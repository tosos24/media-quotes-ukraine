"""Runtime configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]
if load_dotenv:
    load_dotenv(ROOT / ".env")


def _path(env: str, default: Path) -> Path:
    value = os.getenv(env)
    p = Path(value) if value else default
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: _path("MQCS_DATA_DIR", ROOT / "data"))
    reports_dir: Path = field(default_factory=lambda: _path("MQCS_REPORTS_DIR", ROOT / "reports"))
    logs_dir: Path = field(default_factory=lambda: _path("MQCS_LOGS_DIR", ROOT / "logs"))
    config_dir: Path = ROOT / "config"
    model: str = os.getenv("MQCS_MODEL", "claude-opus-5-5")
    collection_start: date = date.fromisoformat(os.getenv("MQCS_START_DATE", "2026-10-01"))
    report_window_days: int = int(os.getenv("MQCS_REPORT_WINDOW_DAYS", "14"))
    daily_lookback_days: int = int(os.getenv("MQCS_DAILY_LOOKBACK_DAYS", "3"))
    user_agent: str = os.getenv(
        "MQCS_USER_AGENT",
        "MediaQuotesCollector/1.0 (research prototype; respects robots.txt)",
    )
    request_interval_s: float = float(os.getenv("MQCS_REQUEST_INTERVAL", "1.0"))
    http_timeout_s: float = float(os.getenv("MQCS_HTTP_TIMEOUT", "30"))
    llm_concurrency: int = int(os.getenv("MQCS_LLM_CONCURRENCY", "4"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "quotes.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "export"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.export_dir, self.reports_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def load_json(self, name: str):
        with open(self.config_dir / name, encoding="utf-8") as f:
            return json.load(f)


settings = Settings()
