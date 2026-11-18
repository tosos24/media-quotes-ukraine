"""SQLite storage. The quotes table is append-only (enforced by triggers) for auditability."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .settings import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    source_id      TEXT PRIMARY KEY,           -- short stable id, e.g. 'bbc'
    name           TEXT NOT NULL,              -- display name, e.g. 'BBC'
    homepage       TEXT NOT NULL,
    adapter        TEXT NOT NULL,              -- 'bbc' | 'reuters' | 'generic'
    config_json    TEXT NOT NULL DEFAULT '{}', -- discovery endpoints etc.
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    article_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id         TEXT NOT NULL REFERENCES sources(source_id),
    url               TEXT NOT NULL UNIQUE,
    title             TEXT,
    publication_name  TEXT,                    -- e.g. 'BBC News' (from page metadata)
    published_at      TEXT,                    -- ISO-8601 UTC
    modified_at       TEXT,
    discovered_at     TEXT NOT NULL,
    discovered_via    TEXT NOT NULL,           -- channel that found it, e.g. 'sitemap:news'
    candidate_reason  TEXT,                    -- which deterministic rule matched
    status            TEXT NOT NULL DEFAULT 'discovered',
        -- discovered | fetched | irrelevant | out_of_range | blocked | failed | processed
    http_status       INTEGER,
    error             TEXT,
    fetched_at        TEXT,
    raw_path          TEXT,                    -- gzip snapshot of the HTML as fetched
    raw_sha256        TEXT,
    body_json         TEXT,                    -- extracted paragraphs [{"kind":"p","text":...}]
    body_sha256       TEXT,
    relevance_json    TEXT,                    -- deterministic relevance evidence
    quotes_found      INTEGER,
    llm_model         TEXT,
    processed_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS ix_articles_pub ON articles(published_at);

CREATE TABLE IF NOT EXISTS quotes (
    quote_id          TEXT PRIMARY KEY,        -- 'Q-' + first 16 hex of sha256(url|text)
    article_id        INTEGER NOT NULL REFERENCES articles(article_id),
    source_id         TEXT NOT NULL,
    source_name       TEXT NOT NULL,
    publication_name  TEXT NOT NULL,
    article_title     TEXT NOT NULL,
    article_url       TEXT NOT NULL,
    published_at      TEXT NOT NULL,
    extracted_at      TEXT NOT NULL,
    quote_text        TEXT NOT NULL,           -- verbatim; paragraphs separated by blank line
    quote_sha256      TEXT NOT NULL,
    locator_json      TEXT NOT NULL,           -- paragraph/sentence span in the stored body
    speaker           TEXT,
    quote_kind        TEXT,                    -- direct_quote | reported_statement | reporting
    topics_json       TEXT NOT NULL DEFAULT '[]',
    materiality       INTEGER,
    rationale         TEXT,
    extractor_model   TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    run_id            TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_pub ON quotes(published_at);
CREATE INDEX IF NOT EXISTS ix_quotes_source ON quotes(source_id);

-- Append-only guarantees: verbatim quotes and their provenance can never be edited or removed.
CREATE TRIGGER IF NOT EXISTS quotes_no_update BEFORE UPDATE ON quotes
BEGIN SELECT RAISE(ABORT, 'quotes table is append-only'); END;
CREATE TRIGGER IF NOT EXISTS quotes_no_delete BEFORE DELETE ON quotes
BEGIN SELECT RAISE(ABORT, 'quotes table is append-only'); END;

-- Quotes the verifier rejected (kept for audit: shows what the model proposed and why it failed).
CREATE TABLE IF NOT EXISTS rejected_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL,
    proposed_json   TEXT NOT NULL,
    reasons_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    run_id          TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,                -- daily | backfill | add_source | report | ...
    params_json  TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,                -- running | ok | partial | failed
    stats_json   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    run_id    TEXT,
    level     TEXT NOT NULL,
    stage     TEXT NOT NULL,
    message   TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS reports (
    report_id      TEXT PRIMARY KEY,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    md_path        TEXT NOT NULL,
    md_sha256      TEXT NOT NULL,
    decisions_path TEXT NOT NULL,
    model          TEXT,
    quotes_shown   INTEGER,
    quotes_obsolete INTEGER,
    run_id         TEXT
);
"""

_lock = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
