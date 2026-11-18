"""Deterministic (rule-based) relevance detection. No model is involved at this stage."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .settings import settings


@dataclass(frozen=True)
class Rules:
    version: str
    strong: tuple[str, ...]
    context: tuple[str, ...]
    war: tuple[str, ...]
    min_strong: int
    min_context: int
    min_war: int
    exclude: tuple[str, ...]


@lru_cache(maxsize=1)
def rules() -> Rules:
    cfg = settings.load_json("relevance.json")
    return Rules(
        version=cfg["version"],
        strong=tuple(cfg["strong_terms"]),
        context=tuple(cfg["context_terms"]),
        war=tuple(cfg["war_terms"]),
        min_strong=cfg["article_min_strong_hits"],
        min_context=cfg["article_min_context_hits"],
        min_war=cfg["article_min_war_hits"],
        exclude=tuple(cfg["exclude_url_patterns"]),
    )


@lru_cache(maxsize=None)
def _pattern(terms: tuple[str, ...]) -> re.Pattern:
    alts = sorted((re.escape(t).replace(r"\ ", r"[\s-]+") for t in terms), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(alts) + r")(?![a-z0-9])", re.IGNORECASE)


def hits(text: str, terms: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in _pattern(terms).finditer(text or ""):
        key = re.sub(r"[\s-]+", " ", m.group(1).lower())
        out[key] = out.get(key, 0) + 1
    return out


def slug_text(url: str) -> str:
    """Turn a URL path into words: /world/europe/ukraine-drone-attack-2026 -> 'world europe ukraine drone attack 2026'."""
    path = re.sub(r"^https?://[^/]+", "", url)
    return re.sub(r"[/_\-.?=&]+", " ", path)


def excluded_url(url: str) -> bool:
    return any(p in url for p in rules().exclude)


def candidate_match(url: str, title: str = "", description: str = "") -> tuple[bool, dict]:
    """Stage 1: decide from URL + title (+description) whether an item is worth fetching."""
    r = rules()
    if excluded_url(url):
        return False, {"rule": "excluded_url"}
    text = " ".join(filter(None, [slug_text(url), title, description]))
    s, c, w = hits(text, r.strong), hits(text, r.context), hits(text, r.war)
    evidence = {"ruleset": r.version, "strong": s, "context": c, "war": w}
    if s:
        return True, {**evidence, "rule": "strong_term"}
    if c and w:
        return True, {**evidence, "rule": "context_and_war_term"}
    return False, {**evidence, "rule": "no_match"}


def article_match(title: str, body: str) -> tuple[bool, dict]:
    """Stage 2: decide from the full article text whether the article is about the war."""
    r = rules()
    ts = hits(title, r.strong)
    s, c, w = hits(body, r.strong), hits(body, r.context), hits(body, r.war)
    n_s, n_c, n_w = sum(s.values()), sum(c.values()), sum(w.values())
    evidence = {"ruleset": r.version, "title_strong": ts, "body_strong": n_s,
                "body_context": n_c, "body_war": n_w, "top_terms": dict(sorted(
                    {**s, **c}.items(), key=lambda kv: -kv[1])[:8])}
    if ts:
        return True, {**evidence, "rule": "title_strong_term"}
    if n_s >= r.min_strong:
        return True, {**evidence, "rule": f"body_strong_hits>={r.min_strong}"}
    if n_c >= r.min_context and n_w >= r.min_war:
        return True, {**evidence, "rule": "body_context_and_war_hits"}
    return False, {**evidence, "rule": "below_threshold"}
