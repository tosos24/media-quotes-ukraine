"""Deterministic text utilities: normalisation, sentence splitting, hashing.

Nothing in this module is probabilistic. The same input always gives the same output,
which is what makes quote verification reproducible.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")

# Abbreviations that end with a period but do not end a sentence.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "gen", "lt", "col", "maj", "capt", "sgt", "st", "sr", "jr",
    "rev", "gov", "sen", "rep", "pres", "adm", "cmdr", "jan", "feb", "mar", "apr", "jun", "jul",
    "aug", "sep", "sept", "oct", "nov", "dec", "no", "vs", "etc", "inc", "ltd", "co", "corp",
    "u.s", "u.k", "u.n", "e.u", "a.m", "p.m", "approx", "est", "fig", "dept", "mt",
}

_SENT_BOUNDARY = re.compile(r"""([.!?…]+["'”’)\]]*)(\s+)(?=["'“‘(\[]?[A-Z0-9])""")


def clean_ws(text: str) -> str:
    """Collapse runs of whitespace (incl. NBSP) into single spaces and strip ends.

    This is the only transformation applied to article text before storage: it keeps
    every character the publisher wrote, including punctuation and quote marks.
    """
    text = text.replace(" ", " ").replace("​", "")
    return _WS.sub(" ", text).strip()


def normalise_for_compare(text: str) -> str:
    """Canonical form used only to *compare* strings (never stored as a quote).

    Unifies typographic quotes/dashes and whitespace so a model's copy of a quote can be
    compared with the source text without being fooled by curly-vs-straight quotes.
    """
    text = unicodedata.normalize("NFKC", text)
    table = {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "–": "-", "—": "-", "−": "-", "…": "...",
    }
    for k, v in table.items():
        text = text.replace(k, v)
    return _WS.sub(" ", text).strip().lower()


def split_sentences(paragraph: str) -> list[str]:
    """Split a paragraph into sentences with a deterministic rule-based splitter.

    Concatenating the returned sentences with single spaces reproduces the paragraph
    exactly (given the paragraph was produced by ``clean_ws``).
    """
    paragraph = clean_ws(paragraph)
    if not paragraph:
        return []
    sentences: list[str] = []
    start = 0
    for m in _SENT_BOUNDARY.finditer(paragraph):
        end = m.end(1)
        candidate = paragraph[start:end]
        last_word = re.findall(r"([A-Za-z.]+)\.\W*$", candidate)
        if last_word:
            word = last_word[-1].lower().rstrip(".")
            # Abbreviation or a single initial ("J. Smith") -> not a boundary.
            if word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
                continue
        sentences.append(candidate.strip())
        start = m.end(2)
    tail = paragraph[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
