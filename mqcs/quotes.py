"""Quote detection (model judgement) + deterministic reconstruction and verification.

Flow for one article:
  1. The stored article body is split into paragraphs and sentences with stable IDs (p3.s1).
  2. Claude reads the numbered article and proposes quote *spans* (start/end sentence IDs)
     plus a verbatim copy of the text it believes those spans contain.
  3. The system rebuilds the quote text itself from the stored body using the span IDs.
     The model's copy is used only as a cross-check: if the two differ, the quote is rejected.
  4. Further deterministic checks (contiguity, size, boilerplate, metadata completeness,
     speaker attribution present in source) run before anything is stored.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .llm import LLM, ArticleExtraction, ProposedQuote
from .settings import settings
from .textutil import normalise_for_compare, sha256, split_sentences

SPAN_RE = re.compile(r"^p(\d+)\.s(\d+)$")
QUOTABLE_KINDS = {"p", "li", "quote"}
MAX_SPAN_PARAGRAPHS = 3
MAX_SPAN_SENTENCES = 6
MIN_CHARS, MAX_CHARS = 40, 1500
CONTAMINATION = [re.compile(p, re.I) for p in [
    r"\bsign up\b", r"\badvertisement\b", r"\bimage (source|caption)\b", r"\bclick here\b",
    r"\bsubscribe\b", r"https?://", r"\|\s*\|", r"^\s*\d+\s*$", r"\bcookie(s)?\b", r"\bread more\b",
]]
VERIFIER_VERSION = "verify-v1"


@dataclass
class IndexedArticle:
    paragraphs: list[dict]            # [{"kind":..., "text":...}]
    sentences: list[list[str]]        # sentences[p][s]

    @classmethod
    def from_body(cls, paragraphs: list[dict]) -> "IndexedArticle":
        return cls(paragraphs, [split_sentences(p["text"]) for p in paragraphs])

    def render_for_model(self) -> str:
        lines = []
        for pi, (para, sents) in enumerate(zip(self.paragraphs, self.sentences)):
            label = {"h": "SUBHEADING", "li": "LIST ITEM", "quote": "BLOCKQUOTE"}.get(para["kind"], "")
            prefix = f"[p{pi}]" + (f" ({label})" if label else "")
            lines.append(prefix + " " + " ".join(f"[p{pi}.s{si}] {s}" for si, s in enumerate(sents)))
        return "\n".join(lines)

    def order(self) -> list[tuple[int, int]]:
        return [(p, s) for p, sents in enumerate(self.sentences) for s in range(len(sents))]

    def build(self, start: tuple[int, int], end: tuple[int, int]) -> tuple[str, str]:
        """Return (stored_text, plain_text) for an inclusive sentence span.

        stored_text keeps the original structure: paragraphs separated by a blank line, list
        items prefixed with '- '. plain_text is the same words without structure markers.
        """
        parts_stored, parts_plain = [], []
        for p in range(start[0], end[0] + 1):
            s0 = start[1] if p == start[0] else 0
            s1 = end[1] if p == end[0] else len(self.sentences[p]) - 1
            chunk = " ".join(self.sentences[p][s0:s1 + 1])
            parts_plain.append(chunk)
            parts_stored.append(("- " if self.paragraphs[p]["kind"] == "li" else "") + chunk)
        return "\n\n".join(parts_stored), " ".join(parts_plain)


PARA_RE = re.compile(r"^p(\d+)$")


def _parse_id(sid: str, art: "IndexedArticle | None" = None, is_end: bool = False) -> tuple[int, int] | None:
    """Parse 'p4.s1'. A bare paragraph id 'p4' means the whole paragraph: its first sentence as a
    start, its last sentence as an end. The text cross-check still applies either way."""
    sid = sid.strip()
    m = SPAN_RE.match(sid)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = PARA_RE.match(sid)
    if m and art is not None:
        p = int(m.group(1))
        if p < len(art.sentences) and art.sentences[p]:
            return p, (len(art.sentences[p]) - 1 if is_end else 0)
    return None


def verify_proposal(q: ProposedQuote, art: IndexedArticle, meta: dict, section_keys: set[str]) -> dict:
    """Run all deterministic checks. Returns a report with 'ok', 'checks', 'text' and 'locator'."""
    checks: dict[str, bool | str] = {}
    start, end = _parse_id(q.start, art), _parse_id(q.end, art, is_end=True)
    order = art.order()
    checks["span_ids_valid"] = bool(start and end and start in order and end in order)
    if not checks["span_ids_valid"]:
        return {"ok": False, "checks": checks, "reasons": ["invalid span ids"]}
    i0, i1 = order.index(start), order.index(end)
    checks["span_ordered"] = i0 <= i1
    n_paras = end[0] - start[0] + 1
    checks["span_size_ok"] = n_paras <= MAX_SPAN_PARAGRAPHS and (i1 - i0 + 1) <= MAX_SPAN_SENTENCES
    kinds = {art.paragraphs[p]["kind"] for p in range(start[0], end[0] + 1)}
    checks["no_headings_in_span"] = kinds <= QUOTABLE_KINDS
    if not (checks["span_ordered"] and checks["span_size_ok"] and checks["no_headings_in_span"]):
        return {"ok": False, "checks": checks, "reasons": [k for k, v in checks.items() if v is False]}

    stored, plain = art.build(start, end)
    # Cross-check against the model's own copy: any divergence = hallucination/frankenquote risk.
    checks["model_copy_matches_source"] = normalise_for_compare(plain) == normalise_for_compare(q.quote_text)
    body_norm = normalise_for_compare(" ".join(" ".join(s) for s in art.sentences))
    checks["verbatim_in_article_body"] = normalise_for_compare(plain) in body_norm
    checks["length_ok"] = MIN_CHARS <= len(plain) <= MAX_CHARS
    checks["no_contamination"] = not any(p.search(plain) for p in CONTAMINATION)
    for field in ("source_name", "publication_name", "article_title", "article_url", "published_at"):
        checks[f"metadata_{field}"] = bool((meta.get(field) or "").strip())
    checks["topics_valid"] = bool(q.topics) and all(t in section_keys for t in q.topics)
    checks["materiality_in_range"] = 1 <= q.materiality <= 5

    # Speaker attribution must be traceable to the article text near the quote.
    speaker_ok = True
    if q.speaker.strip():
        window = " ".join(" ".join(art.sentences[p]) for p in range(max(0, start[0] - 3), end[0] + 1))
        window_norm = normalise_for_compare(window + " " + meta.get("article_title", ""))
        tokens = [t for t in re.findall(r"[A-Za-zÀ-ž'-]{3,}", q.speaker)
                  if t.lower() not in {"the", "and", "of", "for", "president", "minister", "spokesman",
                                       "spokeswoman", "spokesperson", "official", "officials", "said"}]
        speaker_ok = not tokens or any(t.lower() in window_norm for t in tokens)
    checks["speaker_traceable"] = speaker_ok

    hard = ["model_copy_matches_source", "verbatim_in_article_body", "length_ok", "no_contamination",
            "metadata_source_name", "metadata_publication_name", "metadata_article_title",
            "metadata_article_url", "metadata_published_at"]
    reasons = [k for k in hard if checks[k] is not True]
    return {
        "ok": not reasons,
        "checks": checks,
        "reasons": reasons,
        "text": stored,
        "plain": plain,
        "locator": {"start": q.start, "end": q.end, "paragraphs": [start[0], end[0]]},
        "verifier": VERIFIER_VERSION,
    }


SYSTEM_PROMPT = """You are a meticulous media analyst building an evidence base about Russia's war on Ukraine.
You read one news article at a time and select the passages ("quotes") that help a reader develop a
substantial understanding of the CURRENT TRENDS and EXPECTATIONS of the war.

A good quote:
- is material: it states a fact, figure, decision, position, warning, forecast or assessment that matters
  for understanding where the war is heading (battlefield, negotiations, support, sanctions, resources,
  domestic politics, humanitarian situation, escalation risks, outlook);
- is self-explanatory: a reader who has not seen the article understands it. Prefer spans that name
  who is speaking or acting. If a key sentence depends on the previous sentence for context, include
  that sentence too (the span may cover consecutive sentences and up to 3 consecutive paragraphs);
- is verbatim and contiguous: it is exactly the text of the sentences from `start` to `end`, in order,
  with nothing skipped. Never stitch together non-adjacent sentences;
- is not colour, scene-setting, human-interest detail without wider significance, a photo caption,
  a promotional line, or a repetition of another selected quote.

Each sentence in the article is labelled with an ID like [p4.s1] (paragraph 4, sentence 1).
For each quote return the IDs of its first and last sentence, and in `quote_text` copy the text of
those sentences exactly as written, without the ID labels, joined by single spaces. Do not fix typos,
do not change quotation marks, do not add ellipses.

`speaker` is the person or body whose words/claim the passage reports (e.g. "Volodymyr Zelensky",
"Kremlin spokesman Dmitry Peskov", "Institute for the Study of War"); use "" when it is the outlet's own
reporting. `kind`: direct_quote (contains their words in quotation marks), reported_statement
(paraphrase of what someone said), or reporting (the outlet's own factual reporting/analysis).
`topics`: one or more of these section keys:
{sections}

Be complete: select every material quote in the article, typically 0-8 per article, more for long
analytical pieces. If the article is not really about the war (only a passing mention), set
article_relevant=false and return no quotes."""


def build_system_prompt(sections: list[dict]) -> str:
    listing = "\n".join(f"  - {s['key']}: {s['scope']}" for s in sections)
    return SYSTEM_PROMPT.replace("{sections}", listing)


def build_user_prompt(meta: dict, art: IndexedArticle) -> str:
    return (
        f"Source: {meta['source_name']}\nPublication: {meta['publication_name']}\n"
        f"Published: {meta['published_at']}\nTitle: {meta['article_title']}\nURL: {meta['article_url']}\n\n"
        f"ARTICLE (sentence IDs in brackets):\n{art.render_for_model()}"
    )


def extract_quotes(llm: LLM, meta: dict, paragraphs: list[dict]) -> tuple[ArticleExtraction, dict, IndexedArticle]:
    sections = settings.load_json("sections.json")
    art = IndexedArticle.from_body(paragraphs)
    result, call_meta = llm.parse(build_system_prompt(sections), build_user_prompt(meta, art),
                                  ArticleExtraction, effort="medium")
    return result, call_meta, art


def quote_id(article_url: str, text: str) -> str:
    return "Q-" + sha256(article_url + "\n" + text)[:16]


def proposal_json(q: ProposedQuote) -> str:
    return json.dumps(q.model_dump(), ensure_ascii=False)
