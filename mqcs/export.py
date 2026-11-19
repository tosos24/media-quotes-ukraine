"""Exports for LLM contextualisation and audit: JSONL (one quote per line), JSON, CSV + manifest with hashes."""
from __future__ import annotations

import csv
import json

from . import db
from .settings import settings
from .textutil import sha256_bytes

FIELDS = ["quote_id", "quote_text", "speaker", "quote_kind", "topics", "materiality", "source_id",
          "source_name", "publication_name", "article_title", "article_url", "published_at",
          "extracted_at", "extractor_model", "quote_sha256", "locator", "rationale", "verification"]


def quote_records(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM quotes ORDER BY published_at, quote_id"):
        d = dict(r)
        out.append({
            "quote_id": d["quote_id"], "quote_text": d["quote_text"], "speaker": d["speaker"] or None,
            "quote_kind": d["quote_kind"], "topics": json.loads(d["topics_json"]),
            "materiality": d["materiality"], "source_id": d["source_id"], "source_name": d["source_name"],
            "publication_name": d["publication_name"], "article_title": d["article_title"],
            "article_url": d["article_url"], "published_at": d["published_at"],
            "extracted_at": d["extracted_at"], "extractor_model": d["extractor_model"],
            "quote_sha256": d["quote_sha256"], "locator": json.loads(d["locator_json"]),
            "rationale": d["rationale"], "verification": json.loads(d["verification_json"]),
        })
    return out


def _previous_records() -> list[dict]:
    path = settings.export_dir / "quotes.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def export_all(conn) -> dict:
    """The export is append-only, like the quotes table. Quotes already in the previous export are kept
    even when the local DB lacks them (e.g. a fresh CI runner that only re-scanned the recent window),
    so the committed export is always the complete history. Records from the DB win on conflict."""
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    merged = {r["quote_id"]: r for r in _previous_records()}
    merged.update({r["quote_id"]: r for r in quote_records(conn)})
    records = sorted(merged.values(), key=lambda r: (r["published_at"] or "", r["quote_id"]))
    files = {}
    jsonl = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    files["quotes.jsonl"] = jsonl
    files["quotes.json"] = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
    csv_path = settings.export_dir / "quotes.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = [c for c in FIELDS if c not in ("verification", "locator")]
        w.writerow(cols)
        for r in records:
            w.writerow([json.dumps(r[c]) if isinstance(r[c], list) else r[c] for c in cols])
    for name, data in files.items():
        (settings.export_dir / name).write_bytes(data)
    manifest = {
        "generated_at": db.utcnow(),
        "quote_count": len(records),
        "files": {name: {"sha256": sha256_bytes((settings.export_dir / name).read_bytes())}
                  for name in ["quotes.jsonl", "quotes.json", "quotes.csv"]},
        "schema": {f: "see README §Data model" for f in FIELDS},
    }
    (settings.export_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
