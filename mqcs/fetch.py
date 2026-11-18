"""Polite HTTP fetching: robots.txt compliance, per-host rate limiting, retries, raw snapshots."""
from __future__ import annotations

import gzip
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import requests

from .settings import settings
from .textutil import sha256_bytes

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
BLOCKED_STATUSES = {401, 403, 451}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    content: bytes
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and self.error is None

    @property
    def blocked(self) -> bool:
        return self.status in BLOCKED_STATUSES

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, user_agent: str | None = None, interval: float | None = None,
                 timeout: float | None = None, retries: int = 3, respect_robots: bool = True):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        })
        self.interval = settings.request_interval_s if interval is None else interval
        self.timeout = timeout or settings.http_timeout_s
        self.retries = retries
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    # -- robots.txt -------------------------------------------------------------------
    def robots(self, url: str) -> robotparser.RobotFileParser | None:
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt (or unreadable) -> no restrictions known
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        return self._robots[base]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self.robots(url)
        return True if rp is None else rp.can_fetch(self.session.headers["User-Agent"], url)

    def sitemaps_from_robots(self, url: str) -> list[str]:
        rp = self.robots(url)
        return list(rp.site_maps() or []) if rp else []

    # -- fetching ---------------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0) + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def get(self, url: str) -> FetchResult:
        if not self.allowed(url):
            return FetchResult(url, url, None, b"", error="disallowed by robots.txt")
        host = urlparse(url).netloc
        last_error = None
        for attempt in range(self.retries + 1):
            self._throttle(host)
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
            else:
                if r.status_code in RETRY_STATUSES and attempt < self.retries:
                    last_error = f"HTTP {r.status_code}"
                else:
                    err = None if r.ok else f"HTTP {r.status_code}"
                    return FetchResult(url, r.url, r.status_code, r.content, err)
            time.sleep(min(2 ** attempt * 2, 30))
        return FetchResult(url, url, None, b"", error=last_error)


def save_snapshot(content: bytes, source_id: str) -> tuple[str, str]:
    """Store raw bytes gzip-compressed under data/raw/<source>/<sha>.html.gz (content-addressed)."""
    digest = sha256_bytes(content)
    folder = settings.raw_dir / source_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.html.gz"
    if not path.exists():
        with gzip.open(path, "wb") as f:
            f.write(content)
    return str(path.relative_to(settings.data_dir)).replace("\\", "/"), digest


def load_snapshot(rel_path: str) -> bytes:
    with gzip.open(Path(settings.data_dir) / rel_path, "rb") as f:
        return f.read()
