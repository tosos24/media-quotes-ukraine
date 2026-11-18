"""Structured run logging: every event goes to the DB `events` table, a daily JSONL file and stderr."""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from . import db
from .settings import settings

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class RunLogger:
    def __init__(self, conn: sqlite3.Connection, kind: str, params: dict | None = None,
                 console_level: str = "INFO"):
        self.conn = conn
        self.kind = kind
        self.run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{kind}-{uuid.uuid4().hex[:6]}"
        self.console_level = LEVELS[console_level]
        self.stats: dict[str, int] = {}
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        with db.tx(conn):
            conn.execute(
                "INSERT INTO runs(run_id, kind, params_json, started_at, status) VALUES (?,?,?,?,?)",
                (self.run_id, kind, db.dumps(params or {}), db.utcnow(), "running"),
            )
        self.info("run", f"{kind} run started", params or {})

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def log(self, level: str, stage: str, message: str, data: dict | None = None) -> None:
        ts = db.utcnow()
        record = {"ts": ts, "run_id": self.run_id, "level": level, "stage": stage,
                  "message": message, "data": data or {}}
        with db.tx(self.conn):
            self.conn.execute(
                "INSERT INTO events(ts, run_id, level, stage, message, data_json) VALUES (?,?,?,?,?,?)",
                (ts, self.run_id, level, stage, message, db.dumps(data or {})),
            )
        logfile = settings.logs_dir / f"{ts[:10]}.jsonl"
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if LEVELS[level] >= self.console_level:
            print(f"[{ts}] {level:<7} {stage:<10} {message}", file=sys.stderr, flush=True)

    def debug(self, stage, message, data=None):
        self.log("DEBUG", stage, message, data)

    def info(self, stage, message, data=None):
        self.log("INFO", stage, message, data)

    def warning(self, stage, message, data=None):
        self.log("WARNING", stage, message, data)

    def error(self, stage, message, data=None):
        self.log("ERROR", stage, message, data)

    def finish(self, status: str = "ok") -> None:
        self.info("run", f"{self.kind} run finished: {status}", self.stats)
        with db.tx(self.conn):
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE run_id=?",
                (db.utcnow(), status, db.dumps(self.stats), self.run_id),
            )
