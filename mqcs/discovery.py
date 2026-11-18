"""Deterministic article discovery: sitemaps, RSS feeds and topic/section pages.

Each adapter yields Candidate objects. Whether a candidate is fetched is decided by the
rule-based filter in relevance.py, never by a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .extract import to_utc_iso
from .fetch import Fetcher
from .textutil import clean_ws


class SourceBlocked(Exception):
    """The publisher refused automated access (HTTP 401/403/451 or robots.txt)."""


@dataclass
class Candidate:
    url: str
    title: str = ""
    description: str = ""
    published_hint: str | None = None  # ISO date/time if the channel provides one
    via: str = ""


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}


def canonical(url: str) -> str:
    p = urlparse(url.strip())
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), p.path, "", "", ""))


def in_window(hint: str | None, start: date, end: date) -> bool:
    """True when the hint is inside [start, end], or when there is no hint (decided after fetch)."""
    if not hint:
        return True
    d = date.fromisoformat(hint[:10])
    return start <= d <= end


def parse_relative_date(text: str, today: date) -> date | None:
    """Parse BBC-style promo dates: '5 hrs ago', '2 days ago', '31 October', '3 Nov 2025'."""
    t = text.lower()
    m = re.search(r"(\d+)\s*(min|mins|minute|minutes|hr|hrs|hour|hours)\s+ago", t)
    if m:
        return today
    m = re.search(r"(\d+)\s*(day|days)\s+ago", t)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r"|" + "|".join(k[:3] for k in MONTHS) +
                  r")\b(?:\s+(\d{4}))?", t)
    if m:
        month = MONTHS.get(m.group(2)) or next(v for k, v in MONTHS.items() if k.startswith(m.group(2)))
        year = int(m.group(3)) if m.group(3) else (today.year if month <= today.month else today.year - 1)
        try:
            return date(year, month, int(m.group(1)))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------------------
# Generic feed parsers
# --------------------------------------------------------------------------------------
def parse_sitemap(xml: str) -> tuple[list[dict], list[dict]]:
    """Return (child_sitemaps, url_entries). Works for sitemap indexes, urlsets and news sitemaps."""
    soup = BeautifulSoup(xml, "xml")
    children = [{"loc": clean_ws(s.loc.get_text()), "lastmod": to_utc_iso(s.lastmod.get_text()) if s.lastmod else None}
                for s in soup.find_all("sitemap") if s.loc]
    entries = []
    for u in soup.find_all("url"):
        if not u.loc:
            continue
        news = u.find("news")
        title = news.find("title").get_text() if news and news.find("title") else ""
        pub = news.find("publication_date").get_text() if news and news.find("publication_date") else None
        entries.append({
            "loc": clean_ws(u.loc.get_text()),
            "lastmod": to_utc_iso(u.lastmod.get_text()) if u.lastmod else None,
            "title": clean_ws(title),
            "published": to_utc_iso(pub) if pub else None,
        })
    return children, entries


def parse_rss(xml: str) -> list[dict]:
    soup = BeautifulSoup(xml, "xml")
    items = []
    for it in soup.find_all(["item", "entry"]):
        link = it.find("link")
        href = (link.get("href") or link.get_text()) if link else ""
        pub = it.find(["pubDate", "published", "updated"])
        desc = it.find(["description", "summary"])
        items.append({
            "url": clean_ws(href),
            "title": clean_ws(it.find("title").get_text()) if it.find("title") else "",
            "description": clean_ws(BeautifulSoup(desc.get_text(), "lxml").get_text(" ")) if desc else "",
            "published": to_utc_iso(pub.get_text()) if pub else None,
        })
    return items


def _get(fetcher: Fetcher, url: str, log) -> str | None:
    res = fetcher.get(url)
    if res.blocked or res.error == "disallowed by robots.txt":
        raise SourceBlocked(f"{url}: {res.error}")
    if not res.ok:
        log.warning("discover", f"fetch failed {url}: {res.error}")
        return None
    return res.text


# --------------------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------------------
class BaseAdapter:
    name = "base"

    def __init__(self, source: dict, fetcher: Fetcher, log):
        self.source = source
        self.cfg = source.get("config", {})
        self.fetcher = fetcher
        self.log = log
        pattern = self.cfg.get("article_url_pattern")
        self.url_re = re.compile(pattern) if pattern else None

    def is_article_url(self, url: str) -> bool:
        if self.url_re is not None:
            return bool(self.url_re.match(url))
        return urlparse(url).netloc.endswith(urlparse(self.source["homepage"]).netloc.replace("www.", ""))

    # Common channels ------------------------------------------------------------------
    def from_sitemaps(self, index_urls: list[str], start: date, end: date, via: str,
                      max_children: int = 50) -> Iterator[Candidate]:
        queue = list(index_urls)
        seen = set()
        while queue and len(seen) < max_children + len(index_urls):
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            xml = _get(self.fetcher, sm, self.log)
            if not xml:
                continue
            children, entries = parse_sitemap(xml)
            for c in children:
                # Skip child sitemaps last modified before the window (they cannot hold new items).
                if c["lastmod"] and c["lastmod"][:10] < start.isoformat():
                    continue
                queue.append(c["loc"])
            for e in entries:
                url = canonical(e["loc"])
                hint = e["published"] or e["lastmod"]
                if e["published"] and not in_window(hint, start, end):
                    continue
                if not e["published"] and e["lastmod"] and e["lastmod"][:10] < start.isoformat():
                    continue  # lastmod >= publication date, so this item predates the window
                if self.is_article_url(url):
                    yield Candidate(url, e["title"], "", e["published"], via)

    def from_rss(self, feeds: list[str], start: date, end: date) -> Iterator[Candidate]:
        for feed in feeds:
            xml = _get(self.fetcher, feed, self.log)
            if not xml:
                continue
            for it in parse_rss(xml):
                url = canonical(it["url"])
                if it["url"] and self.is_article_url(url) and in_window(it["published"], start, end):
                    yield Candidate(url, it["title"], it["description"], it["published"], f"rss:{feed}")

    def discover(self, start: date, end: date, mode: str) -> Iterator[Candidate]:
        raise NotImplementedError


class BBCAdapter(BaseAdapter):
    name = "bbc"

    def topic_pages(self, start: date, end: date) -> Iterator[Candidate]:
        today = datetime.now(timezone.utc).date()
        for topic in self.cfg.get("topic_pages", []):
            for page in range(1, int(self.cfg.get("max_topic_pages", 60)) + 1):
                html = _get(self.fetcher, f"{topic}?page={page}", self.log)
                if not html:
                    break
                soup = BeautifulSoup(html, "lxml")
                promos = soup.select('[data-testid="promo"]') or soup.select("li")
                dates_on_page = []
                found = 0
                for promo in promos:
                    a = promo.find("a", href=True)
                    if not a:
                        continue
                    url = canonical(urljoin(topic, a["href"]))
                    meta = " ".join(x.get_text(" ", strip=True) for x in promo.select('[class*="Metadata"]'))
                    d = parse_relative_date(meta, today) if meta else None
                    if d:
                        dates_on_page.append(d)
                    if not self.is_article_url(url):
                        continue
                    found += 1
                    if d and not (start <= d <= end):
                        continue
                    yield Candidate(url, clean_ws(a.get_text(" ")), "", d.isoformat() if d else None,
                                    f"topic:{topic}")
                if found == 0 or (dates_on_page and max(dates_on_page) < start):
                    break  # pages are reverse-chronological: everything further is older

    def archive_sitemaps(self, start: date, end: date) -> Iterator[Candidate]:
        """Exhaustive mode: every BBC news article modified in the window (no titles available)."""
        idx = self.cfg.get("archive_sitemap_index")
        if idx:
            yield from self.from_sitemaps([idx], start, end, "sitemap:archive", max_children=80)

    def discover(self, start, end, mode):
        yield from self.from_sitemaps(self.cfg.get("news_sitemaps", []), start, end, "sitemap:news")
        yield from self.from_rss(self.cfg.get("rss_feeds", []), start, end)
        yield from self.topic_pages(start, end)
        if mode == "exhaustive":
            yield from self.archive_sitemaps(start, end)


class ReutersAdapter(BaseAdapter):
    name = "reuters"

    def section_pages(self, start: date, end: date) -> Iterator[Candidate]:
        for page_url in self.cfg.get("section_pages", []):
            html = _get(self.fetcher, page_url, self.log)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                url = canonical(urljoin(page_url, a["href"]))
                m = re.search(r"-(\d{4}-\d{2}-\d{2})/?$", url)
                hint = m.group(1) if m else None
                if self.is_article_url(url) and in_window(hint, start, end):
                    yield Candidate(url, clean_ws(a.get_text(" ")), "", hint, f"section:{page_url}")

    def dated_sitemaps(self, start: date, end: date) -> Iterator[Candidate]:
        template = self.cfg.get("dated_sitemap_template")
        if not template:
            return
        for offset in range(0, 20000, 100):
            xml = _get(self.fetcher, template.format(offset=offset), self.log)
            if not xml:
                return
            _, entries = parse_sitemap(xml)
            if not entries:
                return
            oldest = None
            for e in entries:
                hint = e["published"] or e["lastmod"]
                oldest = min(oldest or hint, hint) if hint else oldest
                url = canonical(e["loc"])
                if self.is_article_url(url) and in_window(hint, start, end):
                    yield Candidate(url, e["title"], "", hint, "sitemap:dated")
            if oldest and oldest[:10] < start.isoformat():
                return

    def discover(self, start, end, mode):
        yield from self.from_sitemaps(self.cfg.get("news_sitemaps", []), start, end, "sitemap:news")
        yield from self.section_pages(start, end)
        if mode in ("backfill", "exhaustive"):
            yield from self.dated_sitemaps(start, end)


class GenericAdapter(BaseAdapter):
    """Auto-configured adapter for user-added sources (robots.txt sitemaps + RSS autodiscovery)."""
    name = "generic"

    def discover(self, start, end, mode):
        sitemaps = self.cfg.get("news_sitemaps", []) + self.cfg.get("sitemaps", [])
        yield from self.from_sitemaps(sitemaps, start, end, "sitemap", max_children=int(
            self.cfg.get("max_child_sitemaps", 40)))
        yield from self.from_rss(self.cfg.get("rss_feeds", []), start, end)


ADAPTERS = {"bbc": BBCAdapter, "reuters": ReutersAdapter, "generic": GenericAdapter}


def adapter_for(source: dict, fetcher: Fetcher, log) -> BaseAdapter:
    return ADAPTERS[source["adapter"]](source, fetcher, log)


# --------------------------------------------------------------------------------------
# Auto-configuration of a new source
# --------------------------------------------------------------------------------------
def autoconfigure_source(homepage: str, fetcher: Fetcher) -> dict:
    """Build a discovery config for an arbitrary publisher from robots.txt and its homepage."""
    homepage = homepage if homepage.endswith("/") else homepage + "/"
    sitemaps = fetcher.sitemaps_from_robots(homepage)
    news = [s for s in sitemaps if re.search(r"news|article|post", s, re.I)]
    others = [s for s in sitemaps if s not in news]
    if not sitemaps:
        others = [urljoin(homepage, "sitemap.xml"), urljoin(homepage, "sitemap_index.xml")]
    rss: list[str] = []
    res = fetcher.get(homepage)
    if res.ok:
        soup = BeautifulSoup(res.text, "lxml")
        for link in soup.find_all("link", attrs={"type": re.compile(r"(rss|atom)\+xml")}):
            if link.get("href"):
                rss.append(urljoin(homepage, link["href"]))
    for guess in ("feed", "rss", "rss.xml", "feeds/all.rss"):
        if not rss:
            r = fetcher.get(urljoin(homepage, guess))
            if r.ok and ("<rss" in r.text[:500] or "<feed" in r.text[:500]):
                rss.append(urljoin(homepage, guess))
    host = urlparse(homepage).netloc
    return {
        "news_sitemaps": news[:10],
        "sitemaps": others[:10],
        "rss_feeds": rss[:10],
        "article_url_pattern": r"^https?://(www\.)?" + re.escape(host.replace("www.", "")) + r"/.+",
        "max_child_sitemaps": 40,
        "homepage_status": res.status,
        "autoconfigured_at": datetime.now(timezone.utc).isoformat(),
    }
