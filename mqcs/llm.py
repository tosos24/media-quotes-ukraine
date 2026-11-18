"""Claude API wrapper. The model is used for judgement only (which spans are material quotes,
which section they belong to, which are obsolete, section summaries). It never supplies quote
text or metadata that gets stored: those always come from the publisher's page."""
from __future__ import annotations

from typing import Literal, TypeVar

import anthropic
from pydantic import BaseModel, Field

from .settings import settings

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


# ----------------------------------------------------------------------------------------
# Structured output schemas
# ----------------------------------------------------------------------------------------
class ProposedQuote(BaseModel):
    start: str = Field(description="ID of the first sentence of the quote, e.g. 'p4.s0'")
    end: str = Field(description="ID of the last sentence of the quote (same or later), e.g. 'p4.s2'")
    quote_text: str = Field(description="The exact text of sentences start..end, copied character for character")
    speaker: str = Field(description="Person/organisation whose words or claim these are; '' if the journalist's own reporting")
    kind: Literal["direct_quote", "reported_statement", "reporting"]
    topics: list[str] = Field(description="One or more section keys this quote informs")
    materiality: int = Field(description="1 (marginal) to 5 (essential for understanding trends/expectations)")
    rationale: str = Field(description="One sentence: why this matters for understanding the war's trends or expectations")


class ArticleExtraction(BaseModel):
    article_relevant: bool
    relevance_note: str
    quotes: list[ProposedQuote]


class SectionAssignment(BaseModel):
    section_key: str
    summary: str = Field(description="1-2 sentence inference of what the selected quotes collectively indicate")
    quote_ids: list[str]


class ObsoleteDecision(BaseModel):
    quote_id: str
    superseded_by: list[str] = Field(description="IDs of newer quotes whose facts replace this one")
    reason: str


class ReportPlan(BaseModel):
    sections: list[SectionAssignment]
    obsolete: list[ObsoleteDecision]
    not_material: list[str] = Field(description="IDs of current quotes left out as duplicative or immaterial")


# ----------------------------------------------------------------------------------------
class LLM:
    def __init__(self, model: str | None = None):
        import os
        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_PROFILE")):
            raise LLMError("no Anthropic credentials found (set ANTHROPIC_API_KEY in .env)")
        self.model = model or settings.model
        self.client = anthropic.Anthropic(max_retries=4, timeout=600)

    def parse(self, system: str, user: str, schema: type[T], effort: str = "medium",
              max_tokens: int = 32000) -> tuple[T, dict]:
        """Structured call with server-side refusal fallback. Returns (parsed, call_metadata)."""
        # Server-side refusal fallback exists for the Opus/Sonnet/Fable tiers, not for Haiku.
        fallback = {} if "haiku" in self.model else {
            "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": effort},
                **fallback,
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed - set ANTHROPIC_API_KEY") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Bad request: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Rate limited after retries") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Network error contacting Anthropic API: {e}") from e
        meta = {"model": resp.model, "request_id": resp._request_id, "stop_reason": resp.stop_reason,
                "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        if resp.stop_reason == "refusal":
            category = resp.stop_details.category if resp.stop_details else None
            raise LLMError(f"Model refused (category={category})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Output truncated at max_tokens")
        if resp.parsed_output is None:
            raise LLMError("No structured output returned")
        return resp.parsed_output, meta
