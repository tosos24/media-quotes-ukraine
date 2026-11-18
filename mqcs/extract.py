"""Article extraction: HTML -> metadata + clean ordered paragraphs (deterministic)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from .textutil import clean_ws

# Lines that are page furniture, not journalism. Matched against whole paragraphs.
BOILERPLATE = [re.compile(p, re.IGNORECASE) for p in [
    r"^this video can ?not be played\.?$",
    r"^(watch|listen|look|video|audio)\s*:",
    r"^media caption", r"^image (source|caption)", r"^(getty images|reuters|epa|afp|ap|pa media)$",
    r"^sign up (for|to)\b", r"^get our .* newsletter", r"^follow .* on (x|twitter|facebook|instagram|bluesky)",
    r"^(related|more on this story|read more|related topics|top stories|more from)\b",
    r"^(reporting by|additional reporting by|writing by|editing by|compiled by)\b",
    r"^our standards:", r"thomson reuters trust principles", r"^suggested topics", r"^advertisement$",
    r"^copyright\b", r"^©", r"^share this", r"^\s*$",
    r"^(bbc )?(verify|news) (live|app)", r"^download the bbc",
]]


@dataclass
class ArticleDoc:
    url: str
    title: str = ""
    description: str = ""
    publication_name: str = ""
    published_at: str | None = None
    modified_at: str | None = None
    paragraphs: list[dict] = field(default_factory=list)  # {"kind": "p|h|li|quote", "text": str}
    extractor: str = ""

    @property
    def body_text(self) -> str:
        return "\n\n".join(p["text"] for p in self.paragraphs)


def is_boilerplate(text: str) -> bool:
    return any(p.search(text) for p in BOILERPLATE)


def to_utc_iso(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                    "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _jsonld_articles(soup: BeautifulSoup) -> list[dict]:
    found = []
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if not isinstance(item, dict):
                continue
            stack.extend(item.get("@graph", []) if isinstance(item.get("@graph"), list) else [])
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(isinstance(t, str) and t.endswith("Article") for t in types):
                found.append(item)
    return found


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return clean_ws(tag["content"])
    return ""


def _metadata(soup: BeautifulSoup, doc: ArticleDoc) -> None:
    ld = _jsonld_articles(soup)
    item = ld[0] if ld else {}
    doc.title = clean_ws(item.get("headline") or "") or _meta(soup, "og:title", "twitter:title") or \
        clean_ws(soup.title.get_text() if soup.title else "")
    doc.description = clean_ws(item.get("description") or "") or _meta(soup, "og:description", "description")
    pub = item.get("publisher")
    if isinstance(pub, list):
        pub = pub[0] if pub else {}
    doc.publication_name = clean_ws((pub or {}).get("name", "") if isinstance(pub, dict) else str(pub or "")) \
        or _meta(soup, "og:site_name")
    doc.published_at = to_utc_iso(item.get("datePublished")) or to_utc_iso(
        _meta(soup, "article:published_time", "og:article:published_time"))
    if not doc.published_at:
        t = soup.find("time", attrs={"datetime": True})
        doc.published_at = to_utc_iso(t["datetime"]) if t else None
    doc.modified_at = to_utc_iso(item.get("dateModified")) or to_utc_iso(_meta(soup, "article:modified_time"))


def _add(doc: ArticleDoc, kind: str, text: str) -> None:
    text = clean_ws(text)
    if len(text) < 2 or is_boilerplate(text):
        return
    if doc.paragraphs and doc.paragraphs[-1]["text"] == text:
        return  # duplicated render
    doc.paragraphs.append({"kind": kind, "text": text})


def _inside(el: Tag, names: tuple[str, ...] = ("figcaption", "figure", "aside", "nav", "footer")) -> bool:
    return any(p.name in names for p in el.parents)


def extract_bbc(soup: BeautifulSoup, doc: ArticleDoc) -> None:
    root = soup.find("article") or soup.find("main") or soup
    for el in root.find_all(["p", "h2", "h3", "li", "blockquote"]):
        if _inside(el):
            continue
        holder = el.find_parent(attrs={"data-component": True})
        holder_kind = holder.get("data-component") if holder else ""
        classes = " ".join(c for p in el.parents if isinstance(p, Tag) for c in (p.get("class") or [])[:2])
        in_text = "RichTextContainer" in classes or holder_kind in ("text-block", "subheadline-block")
        if el.name == "p" and in_text and not el.find_parent("li"):
            _add(doc, "p", el.get_text(" "))
        elif el.name in ("h2", "h3") and ("Subheadline" in classes or holder_kind == "subheadline-block"
                                          or "TextContent" in classes):
            _add(doc, "h", el.get_text(" "))
        elif el.name == "li" and in_text:
            _add(doc, "li", el.get_text(" "))
        elif el.name == "blockquote" and in_text:
            _add(doc, "quote", el.get_text(" "))


def extract_reuters(soup: BeautifulSoup, doc: ArticleDoc) -> None:
    for el in soup.select('[data-testid^="paragraph-"], [data-testid="Heading"], article h2'):
        if _inside(el):
            continue
        kind = "h" if el.name in ("h2", "h3") or el.get("data-testid") == "Heading" else "p"
        _add(doc, kind, el.get_text(" "))


def extract_generic(html: str, soup: BeautifulSoup, doc: ArticleDoc) -> None:
    try:
        import trafilatura
        text = trafilatura.extract(html, output_format="txt", include_comments=False,
                                   include_tables=False, include_links=False, favor_precision=True)
    except Exception:  # pragma: no cover - trafilatura is optional at runtime
        text = None
    if text:
        for line in text.split("\n"):
            line = line.strip()
            kind = "li" if line.startswith(("- ", "• ")) else "p"
            _add(doc, kind, line[2:] if kind == "li" else line)
        return
    root = soup.find("article") or soup.find("main") or soup.body or soup
    for el in root.find_all(["p", "h2", "li", "blockquote"]):
        if not _inside(el):
            _add(doc, {"h2": "h", "blockquote": "quote"}.get(el.name, el.name), el.get_text(" "))


def extract_article(html: str, url: str, adapter: str) -> ArticleDoc:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        if t.name == "script" and t.get("type") == "application/ld+json":
            continue
        t.decompose()
    doc = ArticleDoc(url=url)
    _metadata(soup, doc)
    if adapter == "bbc":
        extract_bbc(soup, doc)
        doc.extractor = "bbc-dom-v1"
    elif adapter == "reuters":
        extract_reuters(soup, doc)
        doc.extractor = "reuters-dom-v1"
    if len(doc.paragraphs) < 3:  # site markup changed or unknown site -> generic fallback
        doc.paragraphs = []
        extract_generic(html, soup, doc)
        doc.extractor = (doc.extractor + "+" if doc.extractor else "") + "generic-trafilatura-v1"
    # Drop the headline if it was repeated as the first paragraph.
    if doc.paragraphs and doc.paragraphs[0]["text"] == doc.title:
        doc.paragraphs.pop(0)
    return doc
