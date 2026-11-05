"""Group lyrics into slides, using an LLM (via OpenRouter) to pick the breaks.

The model never writes lyrics. It only sees numbered lines and answers with
inclusive line ranges, which this module validates and turns back into the
original text. A plan that skips, repeats or reorders lines, or any LLM
failure, falls back to a deterministic grouping, so a request still produces
a presentation and the lyrics are never altered.
"""

import logging
import math
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ..config import Settings

log = logging.getLogger(__name__)

PARAGRAPHS_PER_SLIDE = 2
# A stanza longer than this is split into shorter paragraphs.
MAX_PARAGRAPH_LINES = 6
MAX_SLIDE_LINES = 12
_SPLIT_TARGET_LINES = 4

Paragraph = list[str]
Slide = list[Paragraph]

SYSTEM_PROMPT = f"""\
You arrange song lyrics into presentation slides for a church multimedia team.
The lyrics arrive as numbered lines; blank lines mark the original stanzas.

Answer with the slides in order. Each slide has 1 to {PARAGRAPHS_PER_SLIDE} \
paragraphs, and each paragraph is an inclusive range of consecutive line numbers \
(start, end).

Hard rules:
- Cover every line exactly once, in the original order: no gaps, overlaps or reordering.
- Never write, edit or translate lyrics. You only choose the ranges.
- A paragraph has at most {MAX_PARAGRAPH_LINES} lines and a slide at most {MAX_SLIDE_LINES}.

Guidelines:
- Aim for {PARAGRAPHS_PER_SLIDE} paragraphs per slide so the deck stays short.
- Keep the original stanza breaks as paragraph breaks. Split a stanza only when it \
is longer than {_SPLIT_TARGET_LINES} lines, at a natural phrase boundary.
- Pair paragraphs that belong together (a verse with its chorus, question and \
answer) and keep a repeated chorus or bridge intact.
- Avoid a slide with a single short paragraph unless it is the last one or the \
lyrics demand it.
"""


class ParagraphRange(BaseModel):
    start: int = Field(description="First line number of the paragraph (1-based)")
    end: int = Field(description="Last line number of the paragraph, inclusive")


class SlideSpec(BaseModel):
    paragraphs: list[ParagraphRange] = Field(min_length=1, max_length=PARAGRAPHS_PER_SLIDE)


class LLMPlan(BaseModel):
    """What the model must return."""

    slides: list[SlideSpec]


class PlanError(ValueError):
    """The model's plan doesn't cover the lyrics exactly once, in order."""


class PlanChain(Protocol):
    """The slice of a LangChain structured-output runnable that we use."""

    async def ainvoke(self, input: list[BaseMessage]) -> Any: ...


ArrangedBy = Literal["llm", "fallback"]


@dataclass(frozen=True, slots=True)
class Arrangement:
    slides: list[Slide]
    arranged_by: ArrangedBy


def number_lines(stanzas: list[Paragraph]) -> tuple[list[str], str]:
    """Return ``(flat lines, numbered text)``; stanzas are separated by blank lines."""
    lines: list[str] = []
    blocks: list[str] = []
    for stanza in stanzas:
        blocks.append("\n".join(f"{len(lines) + i + 1}: {line}" for i, line in enumerate(stanza)))
        lines.extend(stanza)
    return lines, "\n\n".join(blocks)


def resolve_plan(plan: LLMPlan, lines: list[str]) -> list[Slide]:
    """Turn ranges into slides of text, raising ``PlanError`` if the plan is unsound."""
    slides: list[Slide] = []
    expected = 1
    for slide_number, spec in enumerate(plan.slides, start=1):
        slide: Slide = []
        for rng in spec.paragraphs:
            if rng.start != expected or rng.end < rng.start or rng.end > len(lines):
                raise PlanError(
                    f"slide {slide_number}: range {rng.start}-{rng.end} "
                    f"does not continue from line {expected} of {len(lines)}"
                )
            if rng.end - rng.start + 1 > MAX_PARAGRAPH_LINES:
                raise PlanError(f"slide {slide_number}: paragraph longer than {MAX_PARAGRAPH_LINES} lines")
            slide.append(lines[rng.start - 1 : rng.end])
            expected = rng.end + 1
        if sum(len(paragraph) for paragraph in slide) > MAX_SLIDE_LINES:
            raise PlanError(f"slide {slide_number}: more than {MAX_SLIDE_LINES} lines")
        slides.append(slide)
    if expected != len(lines) + 1:
        raise PlanError(f"plan stops at line {expected - 1} of {len(lines)}")
    return slides


def _split_long(stanza: Paragraph) -> list[Paragraph]:
    if len(stanza) <= MAX_PARAGRAPH_LINES:
        return [stanza]
    parts = math.ceil(len(stanza) / _SPLIT_TARGET_LINES)
    size = math.ceil(len(stanza) / parts)
    return [stanza[i : i + size] for i in range(0, len(stanza), size)]


def fallback_plan(stanzas: list[Paragraph]) -> list[Slide]:
    """Keep stanza breaks (splitting very long ones) and pair paragraphs per slide."""
    paragraphs = [part for stanza in stanzas for part in _split_long(stanza)]
    return [
        paragraphs[i : i + PARAGRAPHS_PER_SLIDE]
        for i in range(0, len(paragraphs), PARAGRAPHS_PER_SLIDE)
    ]


def build_chain(settings: Settings) -> PlanChain | None:
    """Structured-output chain on OpenRouter, or ``None`` when no key is configured."""
    if settings.openrouter_api_key is None:
        return None
    # OpenRouter speaks the OpenAI Chat Completions API. langchain-openrouter
    # would be the dedicated client, but it pins a pydantic that has no wheels
    # for the Python version this project targets.
    model = ChatOpenAI(
        model=settings.openrouter_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        temperature=0,
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
        default_headers={"X-Title": "Cloudy API"},
    )
    return model.with_structured_output(LLMPlan, method="function_calling")


class LyricsArranger:
    def __init__(self, settings: Settings, chain: PlanChain | None = None) -> None:
        self._chain = chain if chain is not None else build_chain(settings)
        if self._chain is None:
            log.warning("lyrics.llm_disabled OPENROUTER_API_KEY is not set; using fallback grouping")

    async def arrange(self, stanzas: list[Paragraph], title: str | None = None) -> Arrangement:
        if self._chain is None:
            return Arrangement(fallback_plan(stanzas), "fallback")

        lines, numbered = number_lines(stanzas)
        header = f"Song: {title}\n\n" if title else ""
        messages: list[BaseMessage] = [
            SystemMessage(SYSTEM_PROMPT),
            HumanMessage(f"{header}Lyrics:\n\n{numbered}"),
        ]
        try:
            raw = await self._chain.ainvoke(messages)
            slides = resolve_plan(LLMPlan.model_validate(raw), lines)
        except Exception as exc:  # LLM, network and parsing errors alike must not fail the request
            log.warning("lyrics.llm_failed %s: %s; using fallback grouping", type(exc).__name__, exc)
            return Arrangement(fallback_plan(stanzas), "fallback")
        return Arrangement(slides, "llm")
