"""Daily Markdown digest of non-obsolete material quotes for a rolling window (default 14 days).

The model decides *which* stored quotes go into *which* section, which are obsolete (superseded by
newer facts in the set), and writes the 1-2 sentence section summaries. The Markdown itself is
rendered deterministically from the database, so quote text and metadata are copied byte for byte.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from . import db
from .llm import LLM, LLMError, ReportPlan
from .logs import RunLogger
from .settings import settings
from .textutil import sha256

SYSTEM_PROMPT = """You are the editor of a daily intelligence digest on Russia's war on Ukraine.
You receive every verified media quote collected for a rolling {days}-day window. Each quote has an ID,
publication date, source, speaker, suggested topics and its verbatim text.

Your tasks:
1. OBSOLESCENCE. Identify quotes whose facts are outdated because NEWER quotes in this set report a
   newer state of the same fact (e.g. talks "scheduled" later held, cancelled or postponed; a casualty
   or damage figure later updated; a position later reversed; a deadline that has passed with a known
   outcome; a city "under threat" later captured). List them under `obsolete` with the IDs of the newer
   quote(s) in `superseded_by` and a short reason. A quote is NOT obsolete just because it is older,
   or because another quote covers a related topic; only when its specific claim has been superseded.
2. MATERIALITY. Put near-duplicate quotes (the same statement reported by several outlets) and quotes
   that add nothing material under `not_material`, keeping the clearest and best-attributed version.
   Be conservative: when in doubt, keep the quote.
3. SECTIONS. Assign every remaining quote to exactly ONE section (the best fit) from:
{sections}
   Within a section, order quote IDs from most to least significant.
4. SUMMARIES. For each section that has quotes, write a 1-2 sentence inference that accurately
   describes the meaning that emerges from the selected quotes taken together, in the style
   "Commentators highlight facts indicating that the war in Ukraine is intensifying." Base it only on the
   quotes; do not add outside facts, numbers or names that the quotes do not contain.

Every quote ID must appear exactly once across sections, obsolete and not_material. Omit empty sections."""


def _window(today: date, days: int) -> tuple[date, date]:
    return today - timedelta(days=days - 1), today


def _fmt_day(iso: str) -> str:
    d = datetime.fromisoformat(iso).date() if "T" in iso else date.fromisoformat(iso)
    return f"{d.day} {d:%B %Y}"


def _period_title(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {end:%B %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%B} – {end.day} {end:%B %Y}"
    return f"{start.day} {start:%B %Y} – {end.day} {end:%B %Y}"


def load_window_quotes(conn, start: date, end: date) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM quotes WHERE substr(published_at,1,10) BETWEEN ? AND ? ORDER BY published_at, quote_id",
        (start.isoformat(), end.isoformat())).fetchall()
    return [dict(r) for r in rows]


def _model_input(quotes: list[dict]) -> str:
    lines = []
    for q in quotes:
        lines.append(json.dumps({
            "id": q["quote_id"], "published": q["published_at"][:16], "source": q["source_name"],
            "speaker": q["speaker"] or None, "topics": json.loads(q["topics_json"]),
            "materiality": q["materiality"], "article": q["article_title"], "text": q["quote_text"],
        }, ensure_ascii=False))
    return "QUOTES (one JSON object per line):\n" + "\n".join(lines)


def validate_plan(plan: ReportPlan, quotes: list[dict], sections: list[dict]) -> tuple[dict, list[str]]:
    """Deterministically enforce the plan's invariants. Returns (clean_plan, validation_notes)."""
    notes: list[str] = []
    by_id = {q["quote_id"]: q for q in quotes}
    keys = [s["key"] for s in sections]
    placed: set[str] = set()

    obsolete = []
    for o in plan.obsolete:
        if o.quote_id not in by_id or o.quote_id in placed:
            notes.append(f"obsolete: ignored unknown/duplicate id {o.quote_id}")
            continue
        newer = [i for i in o.superseded_by if i in by_id and i != o.quote_id
                 and by_id[i]["published_at"] >= by_id[o.quote_id]["published_at"]]
        if not newer:
            notes.append(f"obsolete: {o.quote_id} reinstated - no valid newer superseding quote cited")
            continue
        obsolete.append({"quote_id": o.quote_id, "superseded_by": newer, "reason": o.reason})
        placed.add(o.quote_id)

    not_material = []
    for i in plan.not_material:
        if i in by_id and i not in placed:
            not_material.append(i)
            placed.add(i)
        else:
            notes.append(f"not_material: ignored unknown/duplicate id {i}")

    section_map: dict[str, dict] = {}
    for s in plan.sections:
        if s.section_key not in keys:
            notes.append(f"section: unknown key {s.section_key!r}; its quotes fall back to their topic")
            continue
        entry = section_map.setdefault(s.section_key, {"summary": s.summary.strip(), "quote_ids": []})
        for i in s.quote_ids:
            if i in by_id and i not in placed:
                entry["quote_ids"].append(i)
                placed.add(i)
            elif i not in by_id:
                notes.append(f"section {s.section_key}: dropped unknown id {i}")

    for q in quotes:  # anything the model forgot is shown under its first topic (never silently dropped)
        if q["quote_id"] not in placed:
            topics = [t for t in json.loads(q["topics_json"]) if t in keys] or [keys[-1]]
            entry = section_map.setdefault(topics[0], {"summary": "", "quote_ids": []})
            entry["quote_ids"].append(q["quote_id"])
            placed.add(q["quote_id"])
            notes.append(f"unassigned {q['quote_id']} placed in {topics[0]} by fallback rule")

    for key, entry in section_map.items():
        n_sent = len(re.findall(r"[.!?](\s|$)", entry["summary"]))
        if not entry["summary"]:
            notes.append(f"section {key}: no summary from model")
        elif n_sent > 2:
            notes.append(f"section {key}: summary has {n_sent} sentences (expected 1-2)")

    ordered = [{"section_key": k, **section_map[k]} for k in keys if section_map.get(k, {}).get("quote_ids")]
    return {"sections": ordered, "obsolete": obsolete, "not_material": not_material}, notes


def _blockquote(text: str) -> str:
    return "\n>\n".join("> " + para for para in text.split("\n\n"))


def render_markdown(plan: dict, quotes_by_id: dict, sections: list[dict], start: date, end: date,
                    generated_at: str, model: str) -> str:
    titles = {s["key"]: s["title"] for s in sections}
    shown = sum(len(s["quote_ids"]) for s in plan["sections"])
    sources = sorted({quotes_by_id[i]["source_name"] for s in plan["sections"] for i in s["quote_ids"]})
    out = [
        f"# Russia's War on Ukraine — Media Quotes Digest: {_period_title(start, end)}",
        "",
        f"**Period covered:** {_fmt_day(start.isoformat())} – {_fmt_day(end.isoformat())} "
        f"(rolling {(end - start).days + 1} days)  ",
        f"**Generated:** {generated_at} UTC  ",
        f"**Quotes shown:** {shown} current material quotes from {', '.join(sources) or 'no sources'}  ",
        f"**Excluded as obsolete:** {len(plan['obsolete'])} · **excluded as duplicative/immaterial:** "
        f"{len(plan['not_material'])}",
        "",
        "> Quotes are reproduced verbatim from the linked articles and were verified against the stored page "
        "snapshot. Section summaries in *italics* are AI-generated inferences "
        f"({model}) based only on the quotes beneath them.",
        "",
    ]
    if not plan["sections"]:
        out += ["_No material quotes were collected for this period._", ""]
    if plan["sections"]:
        out += ["## Contents", ""] + [
            f"- [{titles[s['section_key']]}](#{re.sub(r'[^a-z0-9 -]', '', titles[s['section_key']].lower()).replace(' ', '-')}) "
            f"({len(s['quote_ids'])})" for s in plan["sections"]] + [""]
    for s in plan["sections"]:
        out += [f"## {titles[s['section_key']]}", ""]
        if s["summary"]:
            out += [f"*{s['summary']}*", ""]
        for qid in s["quote_ids"]:
            q = quotes_by_id[qid]
            speaker = f"**{q['speaker']}** · " if q["speaker"] else ""
            out += [
                _blockquote(q["quote_text"]),
                "",
                f"— {speaker}Source: {q['source_name']} · Publication: {q['publication_name']} · "
                f"{_fmt_day(q['published_at'])} · [{q['article_title']}]({q['article_url']}) · `{qid}`",
                "",
            ]
    return "\n".join(out).rstrip() + "\n"


def verify_markdown(md: str, plan: dict, quotes_by_id: dict) -> list[str]:
    """Check every shown quote and its metadata appear verbatim in the rendered Markdown."""
    problems = []
    for s in plan["sections"]:
        for qid in s["quote_ids"]:
            q = quotes_by_id[qid]
            if _blockquote(q["quote_text"]) not in md:
                problems.append(f"{qid}: quote text not verbatim in report")
            if f"[{q['article_title']}]({q['article_url']})" not in md:
                problems.append(f"{qid}: article link missing")
            if sha256(q["quote_text"]) != q["quote_sha256"]:
                problems.append(f"{qid}: stored quote hash mismatch")
    return problems


def generate_report(conn, today: date | None = None, days: int | None = None, llm: LLM | None = None) -> str | None:
    today = today or datetime.now(timezone.utc).date()
    days = days or settings.report_window_days
    start, end = _window(today, days)
    log = RunLogger(conn, "report", {"start": start.isoformat(), "end": end.isoformat()})
    sections = settings.load_json("sections.json")
    quotes = load_window_quotes(conn, start, end)
    quotes_by_id = {q["quote_id"]: q for q in quotes}
    log.info("report", f"{len(quotes)} stored quotes in window {start}..{end}")
    call_meta: dict = {}
    notes: list[str] = []
    if quotes:
        try:
            llm = llm or LLM()
            listing = "\n".join(f"   - {s['key']}: {s['title']} — {s['scope']}" for s in sections)
            system = SYSTEM_PROMPT.replace("{days}", str(days)).replace("{sections}", listing)
            raw_plan, call_meta = llm.parse(system, _model_input(quotes), ReportPlan, effort="high")
        except LLMError as e:
            log.error("report", f"report not generated: {e}")
            log.finish("failed")
            return None
        plan, notes = validate_plan(raw_plan, quotes, sections)
        for n in notes:
            log.warning("report", n)
    else:
        plan = {"sections": [], "obsolete": [], "not_material": []}

    generated_at = db.utcnow().replace("+00:00", "")
    model = call_meta.get("model", settings.model)
    md = render_markdown(plan, quotes_by_id, sections, start, end, generated_at, model)
    problems = verify_markdown(md, plan, quotes_by_id)
    for p in problems:
        log.error("report", p)

    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"quotes-digest_{start.isoformat()}_to_{end.isoformat()}"
    md_path = settings.reports_dir / f"{stem}.md"
    dec_path = settings.reports_dir / f"{stem}.decisions.json"
    md_path.write_text(md, encoding="utf-8")
    decisions = {
        "period": [start.isoformat(), end.isoformat()], "generated_at": generated_at, "llm": call_meta,
        "plan": plan, "validation_notes": notes, "render_verification_problems": problems,
        "quotes_in_window": len(quotes),
        "quote_hashes": {q["quote_id"]: q["quote_sha256"] for q in quotes},
    }
    dec_path.write_text(json.dumps(decisions, indent=2, ensure_ascii=False), encoding="utf-8")
    shown = sum(len(s["quote_ids"]) for s in plan["sections"])
    with db.tx(conn):
        conn.execute(
            """INSERT OR REPLACE INTO reports(report_id, period_start, period_end, generated_at, md_path, md_sha256,
                   decisions_path, model, quotes_shown, quotes_obsolete, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (stem, start.isoformat(), end.isoformat(), generated_at, md_path.name, sha256(md), dec_path.name,
             model, shown, len(plan["obsolete"]), log.run_id))
    log.stats.update({"quotes_in_window": len(quotes), "shown": shown, "obsolete": len(plan["obsolete"]),
                      "not_material": len(plan["not_material"]), "render_problems": len(problems)})
    log.info("report", f"wrote {md_path.name}")
    log.finish("ok" if not problems else "partial")
    return str(md_path)
