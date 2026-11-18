"""Orchestration: discover -> filter -> fetch -> extract -> (LLM) select quotes -> verify -> store."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from . import db
from .discovery import SourceBlocked, adapter_for, autoconfigure_source, canonical
from .extract import extract_article
from .fetch import Fetcher, save_snapshot, load_snapshot
from .llm import LLM, LLMError
from .logs import RunLogger
from .quotes import extract_quotes, proposal_json, quote_id, verify_proposal
from .relevance import article_match, candidate_match, rules
from .settings import settings
from .textutil import sha256

_write_lock = threading.Lock()


# ----------------------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------------------
def seed_sources(conn: sqlite3.Connection) -> None:
    for s in settings.load_json("sources.json"):
        with db.tx(conn):
            conn.execute(
                """INSERT INTO sources(source_id, name, homepage, adapter, config_json, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET config_json=excluded.config_json,
                       homepage=excluded.homepage, adapter=excluded.adapter""",
                (s["source_id"], s["name"], s["homepage"], s["adapter"], db.dumps(s["config"]), db.utcnow()),
            )


def get_sources(conn, source_ids: list[str] | None = None) -> list[dict]:
    rows = conn.execute("SELECT * FROM sources WHERE active=1 ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        if source_ids and r["source_id"] not in source_ids:
            continue
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json") or "{}")
        out.append(d)
    return out


def setup(conn) -> None:
    settings.ensure_dirs()
    db.init_db(conn)
    seed_sources(conn)


# ----------------------------------------------------------------------------------------
# Stage 1: discovery
# ----------------------------------------------------------------------------------------
def discover(conn, log: RunLogger, fetcher: Fetcher, sources: list[dict], start: date, end: date,
             mode: str) -> None:
    for src in sources:
        adapter = adapter_for(src, fetcher, log)
        seen, matched, new = 0, 0, 0
        try:
            for cand in adapter.discover(start, end, mode):
                seen += 1
                url = canonical(cand.url)
                # Exhaustive archive items have no title: they are admitted and judged on full text.
                if cand.via == "sitemap:archive":
                    ok, ev = True, {"rule": "exhaustive_archive", "ruleset": rules().version}
                else:
                    ok, ev = candidate_match(url, cand.title, cand.description)
                if not ok:
                    continue
                matched += 1
                with _write_lock, db.tx(conn):
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO articles(source_id, url, title, published_at, discovered_at,
                               discovered_via, candidate_reason, status)
                           VALUES (?,?,?,?,?,?,?, 'discovered')""",
                        (src["source_id"], url, cand.title or None, cand.published_hint, db.utcnow(),
                         cand.via, db.dumps(ev)),
                    )
                    new += cur.rowcount
        except SourceBlocked as e:
            log.error("discover", f"{src['name']}: access blocked by publisher ({e}). "
                      "Use `mqcs import-html` for manually saved pages or a licensed feed.",
                      {"source": src["source_id"]})
            log.bump(f"blocked:{src['source_id']}")
        log.info("discover", f"{src['name']}: {seen} items scanned, {matched} matched rules, {new} new",
                 {"source": src["source_id"], "scanned": seen, "matched": matched, "new": new})
        log.bump("candidates_new", new)


# ----------------------------------------------------------------------------------------
# Stage 2: fetch + extract + deterministic relevance
# ----------------------------------------------------------------------------------------
def _store_fetched(conn, log, art_row, src: dict, html: bytes, final_url: str, status: int,
                   start: date, end: date) -> str:
    raw_path, raw_sha = save_snapshot(html, src["source_id"])
    doc = extract_article(html.decode("utf-8", errors="replace"), final_url, src["adapter"])
    body_json = json.dumps(doc.paragraphs, ensure_ascii=False)
    ok, ev = article_match(doc.title, doc.body_text)
    ev["extractor"] = doc.extractor
    ev["relevant"] = ok
    pub = doc.published_at or art_row["published_at"]
    if not doc.paragraphs:
        new_status = "failed"
        ev["error"] = "no article body extracted"
    elif not pub:
        new_status = "failed"
        ev["error"] = "no publication date found"
    elif not (start <= date.fromisoformat(pub[:10]) <= end):
        new_status = "out_of_range"
    else:
        new_status = "fetched" if ok else "irrelevant"
    with _write_lock, db.tx(conn):
        conn.execute(
            """UPDATE articles SET title=?, publication_name=?, published_at=?, modified_at=?, status=?,
                   http_status=?, error=?, fetched_at=?, raw_path=?, raw_sha256=?, body_json=?, body_sha256=?,
                   relevance_json=? WHERE article_id=?""",
            (doc.title or art_row["title"], doc.publication_name or src["name"], pub, doc.modified_at,
             new_status, status, ev.get("error"), db.utcnow(), raw_path, raw_sha, body_json,
             sha256(body_json), db.dumps(ev), art_row["article_id"]),
        )
    return new_status


def fetch_articles(conn, log: RunLogger, fetcher: Fetcher, sources: list[dict], start: date, end: date) -> None:
    by_id = {s["source_id"]: s for s in sources}
    # Articles fetched earlier but outside an earlier window may fall inside this one.
    with db.tx(conn):
        conn.execute(
            f"""UPDATE articles SET status=CASE WHEN json_extract(relevance_json, '$.relevant')=1
                    THEN 'fetched' ELSE 'irrelevant' END
                WHERE status='out_of_range' AND substr(published_at,1,10) BETWEEN ? AND ?
                AND source_id IN ({','.join('?' * len(by_id))})""",
            (start.isoformat(), end.isoformat(), *by_id),
        )
    rows = conn.execute(
        f"""SELECT * FROM articles WHERE status IN ('discovered','failed') AND source_id IN
            ({','.join('?' * len(by_id))}) AND (published_at IS NULL OR substr(published_at,1,10) BETWEEN ? AND ?)
            ORDER BY published_at DESC""",
        (*by_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    log.info("fetch", f"{len(rows)} articles to fetch")
    blocked_hosts: set[str] = set()
    for row in rows:
        src = by_id[row["source_id"]]
        host = urlparse(row["url"]).netloc
        if host in blocked_hosts:
            continue
        res = fetcher.get(row["url"])
        if res.blocked or res.error == "disallowed by robots.txt":
            blocked_hosts.add(host)
            with db.tx(conn):
                conn.execute("UPDATE articles SET status='blocked', http_status=?, error=? WHERE article_id=?",
                             (res.status, res.error, row["article_id"]))
            log.error("fetch", f"{src['name']} blocked automated access ({res.error}); remaining "
                      f"{src['name']} articles skipped this run", {"url": row["url"]})
            log.bump("blocked")
            continue
        if not res.ok:
            with db.tx(conn):
                conn.execute("UPDATE articles SET status='failed', http_status=?, error=? WHERE article_id=?",
                             (res.status, res.error, row["article_id"]))
            log.warning("fetch", f"failed {row['url']}: {res.error}")
            log.bump("fetch_failed")
            continue
        try:
            status = _store_fetched(conn, log, row, src, res.content, row["url"], res.status, start, end)
        except Exception as e:  # malformed page etc. - never abort the run
            with db.tx(conn):
                conn.execute("UPDATE articles SET status='failed', error=? WHERE article_id=?",
                             (f"extract error: {type(e).__name__}: {e}", row["article_id"]))
            log.error("extract", f"extraction error {row['url']}: {e}")
            log.bump("extract_failed")
            continue
        log.bump(f"article_{status}")
        log.debug("fetch", f"{status}: {row['url']}")


# ----------------------------------------------------------------------------------------
# Stage 3: model-assisted quote selection + deterministic verification
# ----------------------------------------------------------------------------------------
def _article_meta(row, src_name: str) -> dict:
    return {"source_id": row["source_id"], "source_name": src_name,
            "publication_name": row["publication_name"] or src_name, "article_title": row["title"] or "",
            "article_url": row["url"], "published_at": row["published_at"] or ""}


def process_article(conn, log: RunLogger, llm: LLM, row, src_name: str, section_keys: set[str]) -> int:
    meta = _article_meta(row, src_name)
    paragraphs = json.loads(row["body_json"])
    result, call_meta, art = extract_quotes(llm, meta, paragraphs)
    stored = 0
    with _write_lock, db.tx(conn):
        for q in result.quotes if result.article_relevant else []:
            rep = verify_proposal(q, art, meta, section_keys)
            if not rep["ok"]:
                conn.execute(
                    "INSERT INTO rejected_quotes(article_id, proposed_json, reasons_json, created_at, run_id) "
                    "VALUES (?,?,?,?,?)",
                    (row["article_id"], proposal_json(q), db.dumps(rep), db.utcnow(), log.run_id))
                log.bump("quotes_rejected")
                continue
            qid = quote_id(meta["article_url"], rep["text"])
            verification = {**rep["checks"], "verifier": rep["verifier"], "body_sha256": row["body_sha256"],
                            "raw_sha256": row["raw_sha256"], "ruleset": rules().version,
                            "verified_at": db.utcnow()}
            speaker = q.speaker.strip() if rep["checks"].get("speaker_traceable") else ""
            topics = [t for t in q.topics if t in section_keys]
            cur = conn.execute(
                """INSERT OR IGNORE INTO quotes(quote_id, article_id, source_id, source_name, publication_name,
                       article_title, article_url, published_at, extracted_at, quote_text, quote_sha256,
                       locator_json, speaker, quote_kind, topics_json, materiality, rationale,
                       extractor_model, verification_json, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (qid, row["article_id"], meta["source_id"], meta["source_name"], meta["publication_name"],
                 meta["article_title"], meta["article_url"], meta["published_at"], db.utcnow(), rep["text"],
                 sha256(rep["text"]), db.dumps(rep["locator"]), speaker, q.kind, db.dumps(topics),
                 max(1, min(5, q.materiality)), q.rationale, call_meta["model"], db.dumps(verification),
                 log.run_id))
            stored += cur.rowcount
        conn.execute(
            "UPDATE articles SET status='processed', quotes_found=?, llm_model=?, processed_at=? WHERE article_id=?",
            (stored, call_meta["model"], db.utcnow(), row["article_id"]))
    log.debug("quotes", f"{stored} quotes from {row['url']}",
              {"proposed": len(result.quotes), "relevant": result.article_relevant, **call_meta})
    return stored


def select_quotes(conn, log: RunLogger, llm: LLM | None, sources: list[dict], start: date, end: date) -> None:
    names = {s["source_id"]: s["name"] for s in sources}
    rows = conn.execute(
        f"""SELECT * FROM articles WHERE status='fetched' AND source_id IN ({','.join('?' * len(names))})
            AND substr(published_at,1,10) BETWEEN ? AND ? ORDER BY published_at""",
        (*names, start.isoformat(), end.isoformat())).fetchall()
    if not rows:
        log.info("quotes", "no relevant articles awaiting quote selection")
        return
    if llm is None:
        log.warning("quotes", f"{len(rows)} articles await quote selection but no LLM is configured "
                    "(set ANTHROPIC_API_KEY); they will be processed on the next run")
        return
    section_keys = {s["key"] for s in settings.load_json("sections.json")}
    log.info("quotes", f"selecting quotes from {len(rows)} articles with {llm.model}")
    stop = threading.Event()

    def guarded(r):
        if stop.is_set():
            return None  # account-level failure: leave the article queued for the next run
        return process_article(conn, log, llm, r, names[r["source_id"]], section_keys)

    with ThreadPoolExecutor(max_workers=settings.llm_concurrency) as pool:
        futures = {pool.submit(guarded, r): r for r in rows}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                n = fut.result()
                if n is None:
                    log.bump("llm_deferred")
                    continue
                log.bump("quotes_stored", n)
                log.bump("articles_processed")
            except LLMError as e:
                msg = str(e).lower()
                if any(k in msg for k in ("credit balance", "authentication", "credentials", "permission")):
                    if not stop.is_set():
                        log.error("quotes", f"Anthropic account problem - stopping quote selection; remaining "
                                  f"articles stay queued for the next run: {e}")
                    stop.set()
                    log.bump("llm_deferred")
                    continue
                log.error("quotes", f"LLM failure on {r['url']}: {e}")
                log.bump("llm_failed")
            except Exception as e:
                log.error("quotes", f"processing error on {r['url']}: {type(e).__name__}: {e}")
                log.bump("process_failed")


# ----------------------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------------------
def make_llm(log: RunLogger) -> LLM | None:
    try:
        return LLM()
    except Exception as e:  # missing credentials etc.
        log.warning("quotes", f"LLM unavailable: {e}")
        return None


def collect(conn, kind: str, start: date, end: date, source_ids: list[str] | None = None,
            mode: str = "daily", use_llm: bool = True) -> RunLogger:
    from .export import export_all

    log = RunLogger(conn, kind, {"start": start.isoformat(), "end": end.isoformat(),
                                 "sources": source_ids, "mode": mode})
    status = "ok"
    try:
        sources = get_sources(conn, source_ids)
        if not sources:
            raise ValueError(f"no active sources match {source_ids}")
        fetcher = Fetcher()
        discover(conn, log, fetcher, sources, start, end, mode)
        fetch_articles(conn, log, fetcher, sources, start, end)
        select_quotes(conn, log, make_llm(log) if use_llm else None, sources, start, end)
        export_all(conn)
        if any(k.startswith("blocked") or k.endswith("failed") for k in log.stats):
            status = "partial"
    except Exception as e:
        log.error("run", f"run aborted: {type(e).__name__}: {e}")
        status = "failed"
    log.finish(status)
    return log


def daily(conn, today: date | None = None, use_llm: bool = True, with_report: bool = True) -> None:
    today = today or datetime.now(timezone.utc).date()
    start = max(settings.collection_start, today - timedelta(days=settings.daily_lookback_days))
    collect(conn, "daily", start, today, mode="daily", use_llm=use_llm)
    if with_report and use_llm:
        from .report import generate_report
        generate_report(conn, today)


def backfill(conn, start: date, end: date, source_ids: list[str] | None = None, exhaustive: bool = False,
             use_llm: bool = True) -> RunLogger:
    return collect(conn, "backfill", start, end, source_ids, mode="exhaustive" if exhaustive else "backfill",
                   use_llm=use_llm)


def add_source(conn, name: str, homepage: str, backfill_from: date | None = None,
               use_llm: bool = True) -> str:
    """Auto-build a pipeline for a new outlet and backfill it from the collection start date."""
    fetcher = Fetcher()
    cfg = autoconfigure_source(homepage, fetcher)
    source_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    with db.tx(conn):
        conn.execute(
            """INSERT INTO sources(source_id, name, homepage, adapter, config_json, created_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET config_json=excluded.config_json,
               homepage=excluded.homepage, active=1""",
            (source_id, name, homepage, "generic", db.dumps(cfg), db.utcnow()))
    start = backfill_from or settings.collection_start
    collect(conn, "add_source", start, datetime.now(timezone.utc).date(), [source_id], mode="backfill",
            use_llm=use_llm)
    return source_id


def import_html(conn, source_id: str, url: str, html: bytes, use_llm: bool = True) -> RunLogger:
    """Ingest a page saved manually (e.g. from a browser) for sources that block automated access."""
    log = RunLogger(conn, "import", {"source": source_id, "url": url})
    src = get_sources(conn, [source_id])[0]
    url = canonical(url)
    with db.tx(conn):
        conn.execute(
            """INSERT OR IGNORE INTO articles(source_id, url, discovered_at, discovered_via, candidate_reason, status)
               VALUES (?,?,?,?,?, 'discovered')""",
            (source_id, url, db.utcnow(), "manual_import", db.dumps({"rule": "manual_import"})))
    row = conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
    status = _store_fetched(conn, log, row, src, html, url, 200, date(2000, 1, 1), date(2100, 1, 1))
    log.info("import", f"imported {url}: {status}")
    if status == "fetched":
        row = conn.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
        pub = date.fromisoformat(row["published_at"][:10])
        select_quotes(conn, log, make_llm(log) if use_llm else None, [src], pub, pub)
    from .export import export_all
    export_all(conn)
    log.finish("ok")
    return log


def reverify(conn) -> dict:
    """Audit: re-extract every quoted article from its raw snapshot and confirm each quote still verifies."""
    log = RunLogger(conn, "audit", {})
    ok = bad = 0
    from .quotes import IndexedArticle
    from .textutil import normalise_for_compare
    for q in conn.execute("SELECT q.*, a.raw_path, a.body_sha256 AS cur_body_sha, s.adapter FROM quotes q "
                          "JOIN articles a USING(article_id) JOIN sources s ON s.source_id=q.source_id"):
        problems = []
        if sha256(q["quote_text"]) != q["quote_sha256"]:
            problems.append("quote hash mismatch")
        try:
            html = load_snapshot(q["raw_path"]).decode("utf-8", errors="replace")
            doc = extract_article(html, q["article_url"], q["adapter"])
            art = IndexedArticle.from_body(doc.paragraphs)
            body = normalise_for_compare(" ".join(" ".join(s) for s in art.sentences))
            plain = " ".join(re.sub(r"^- ", "", p) for p in q["quote_text"].split("\n\n"))
            if normalise_for_compare(plain) not in body:
                problems.append("quote not found in raw snapshot")
            if doc.title and doc.title != q["article_title"]:
                problems.append("article title differs from snapshot")
        except FileNotFoundError:
            problems.append("raw snapshot missing")
        if problems:
            bad += 1
            log.error("audit", f"{q['quote_id']}: {', '.join(problems)}")
        else:
            ok += 1
    log.stats.update({"verified": ok, "problems": bad})
    log.finish("ok" if bad == 0 else "partial")
    return {"verified": ok, "problems": bad}
