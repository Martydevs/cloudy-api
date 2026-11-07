import logging
from collections.abc import Callable
from typing import Any

import pytest
from conftest import FakeChain
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from cloudy_api.config import Settings
from cloudy_api.services import lyrics_arranger
from cloudy_api.services.lyrics_arranger import (
    MAX_PARAGRAPH_LINES,
    MAX_SLIDE_LINES,
    PARAGRAPHS_PER_SLIDE,
    LLMPlan,
    LyricsArranger,
    PlanError,
    build_chain,
    fallback_plan,
    number_lines,
    resolve_plan,
)

pytestmark = pytest.mark.anyio

STANZAS = [["a1", "a2", "a3", "a4"], ["b1", "b2"], ["c1", "c2", "c3"]]
LINES = [line for stanza in STANZAS for line in stanza]  # 9 lines


def plan(*slides: list[tuple[int, int]]) -> dict[str, Any]:
    return {"slides": [{"paragraphs": [{"start": s, "end": e} for s, e in sl]} for sl in slides]}


def test_number_lines() -> None:
    lines, text = number_lines(STANZAS)
    assert lines == LINES
    assert text == "1: a1\n2: a2\n3: a3\n4: a4\n\n5: b1\n6: b2\n\n7: c1\n8: c2\n9: c3"


# A sound plan for STANZAS: two single-line paragraphs per slide, one stanza per slide.
GOOD_PLAN = plan(
    [(1, 1), (2, 2)], [(3, 3), (4, 4)], [(5, 5), (6, 6)], [(7, 7), (8, 8)], [(9, 9)]
)
GOOD_SLIDES = [
    [["a1"], ["a2"]],
    [["a3"], ["a4"]],
    [["b1"], ["b2"]],
    [["c1"], ["c2"]],
    [["c3"]],
]


def test_limits_are_two_single_line_paragraphs() -> None:
    assert MAX_PARAGRAPH_LINES == 1
    assert MAX_SLIDE_LINES == PARAGRAPHS_PER_SLIDE == 2


# --------------------------------------------------------------- resolve_plan
def test_resolve_plan_returns_original_text() -> None:
    assert resolve_plan(LLMPlan.model_validate(GOOD_PLAN), STANZAS) == GOOD_SLIDES


def test_resolve_plan_allows_single_line_slides_inside_a_stanza() -> None:
    # Not the ideal grouping, but sound: one line per slide stays within every limit.
    one_per_slide = plan(*[[(n, n)] for n in range(1, 10)])
    slides = resolve_plan(LLMPlan.model_validate(one_per_slide), STANZAS)
    assert slides == [[[line]] for line in LINES]


@pytest.mark.parametrize(
    "bad",
    [
        plan([(1, 1), (3, 3)]),  # skips line 2
        plan([(1, 1), (1, 1)]),  # repeats line 1
        plan([(2, 2)]),  # doesn't start at 1
        plan([(1, 1), (2, 2)], [(3, 3), (4, 4)]),  # stops early
        plan(*[[(n, n)] for n in range(1, 10)], [(10, 10)]),  # runs past the end
        plan([(1, 1)], [(2, 1)]),  # end before start
        {"slides": []},  # empty plan
        plan([(2, 2)], [(1, 1)]),  # reordered
    ],
)
def test_resolve_plan_rejects_unsound_plans(bad: dict[str, Any]) -> None:
    with pytest.raises(PlanError):
        resolve_plan(LLMPlan.model_validate(bad), STANZAS)


def test_resolve_plan_rejects_multi_line_paragraphs() -> None:
    with pytest.raises(PlanError, match="paragraph longer than 1 lines"):
        resolve_plan(LLMPlan.model_validate(plan([(1, 2)], [(3, 4)], [(5, 9)])), STANZAS)


def test_resolve_plan_rejects_slides_over_the_line_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two single-line paragraphs sit exactly at the limit; below it the slide is rejected.
    monkeypatch.setattr(lyrics_arranger, "MAX_SLIDE_LINES", 1)
    with pytest.raises(PlanError, match="more than 1 lines"):
        resolve_plan(LLMPlan.model_validate(GOOD_PLAN), STANZAS)


@pytest.mark.parametrize(
    "crossing",
    [
        # a4 + b1: the first stanza's last line shares a slide with the next stanza.
        plan([(1, 1), (2, 2)], [(3, 3)], [(4, 4), (5, 5)], [(6, 6)], [(7, 7), (8, 8)], [(9, 9)]),
        # b2 + c1
        plan([(1, 1), (2, 2)], [(3, 3), (4, 4)], [(5, 5)], [(6, 6), (7, 7)], [(8, 8), (9, 9)]),
    ],
)
def test_resolve_plan_rejects_slides_that_span_two_stanzas(crossing: dict[str, Any]) -> None:
    with pytest.raises(PlanError, match="spans two stanzas"):
        resolve_plan(LLMPlan.model_validate(crossing), STANZAS)


def test_plan_schema_allows_one_or_two_paragraphs() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        LLMPlan.model_validate(plan([(1, 1), (2, 2), (3, 3)]))
    with pytest.raises(ValueError):  # noqa: PT011
        LLMPlan.model_validate({"slides": [{"paragraphs": []}]})


# ------------------------------------------------------------------- fallback
def test_fallback_gives_two_single_line_paragraphs_per_slide() -> None:
    assert fallback_plan(STANZAS) == GOOD_SLIDES


def test_fallback_never_merges_stanzas() -> None:
    stanzas = [["x1"], ["y1", "y2", "y3"], ["z1"]]
    assert fallback_plan(stanzas) == [[["x1"]], [["y1"], ["y2"]], [["y3"]], [["z1"]]]


@pytest.mark.parametrize(("size", "shape"), [(3, [2, 1]), (4, [2, 2]), (8, [2, 2, 2, 2]), (1, [1])])
def test_fallback_chunks_each_stanza_in_twos(size: int, shape: list[int]) -> None:
    stanza = [f"l{i}" for i in range(size)]
    slides = fallback_plan([stanza])

    assert [len(slide) for slide in slides] == shape  # odd stanzas end with a 1-line slide
    assert all(len(paragraph) == 1 for slide in slides for paragraph in slide)
    assert [line for slide in slides for (line,) in slide] == stanza


def test_fallback_output_is_a_sound_plan() -> None:
    stanzas = [[f"s{s}l{i}" for i in range(size)] for s, size in enumerate([3, 4, 8, 1, 5])]
    slides = fallback_plan(stanzas)
    as_plan: list[list[tuple[int, int]]] = []
    line = 1
    for slide in slides:
        as_plan.append([(line + i, line + i) for i in range(len(slide))])
        line += len(slide)
    assert resolve_plan(LLMPlan.model_validate(plan(*as_plan)), stanzas) == slides


def test_fallback_of_nothing() -> None:
    assert fallback_plan([]) == []


# --------------------------------------------------------------- the arranger
async def test_arranger_uses_the_llm_plan(settings: Settings, fake_chain: FakeChain) -> None:
    # The model may keep a line alone on its own slide; it is still a sound plan.
    fake_chain.result = plan(
        [(1, 1)], [(2, 2), (3, 3)], [(4, 4)], [(5, 5), (6, 6)], [(7, 7), (8, 8)], [(9, 9)]
    )
    arrangement = await LyricsArranger(settings, chain=fake_chain).arrange(STANZAS)

    assert arrangement.arranged_by == "llm"
    assert arrangement.slides == [
        [["a1"]],
        [["a2"], ["a3"]],
        [["a4"]],
        [["b1"], ["b2"]],
        [["c1"], ["c2"]],
        [["c3"]],
    ]


async def test_arranger_sends_only_the_numbered_lines(
    settings: Settings, fake_chain: FakeChain
) -> None:
    # The OpenRouter preset holds the instructions: no system prompt, no extra context.
    fake_chain.result = LLMPlan.model_validate(GOOD_PLAN)  # a model works too
    await LyricsArranger(settings, chain=fake_chain).arrange(STANZAS)

    (messages,) = fake_chain.calls
    (human,) = messages
    assert isinstance(human, HumanMessage)
    assert human.content == number_lines(STANZAS)[1]


@pytest.mark.parametrize(
    "result",
    [
        plan([(1, 1), (2, 2)]),  # doesn't cover everything
        plan([(1, 4)], [(5, 6)], [(7, 9)]),  # the old multi-line paragraphs
        plan(  # crosses from stanza a into stanza b
            [(1, 1), (2, 2)], [(3, 3)], [(4, 4), (5, 5)], [(6, 6)], [(7, 7), (8, 8)], [(9, 9)]
        ),
        {"unexpected": "shape"},
        "not even a plan",
        RuntimeError("OpenRouter is down"),
        TimeoutError(),
    ],
)
async def test_arranger_falls_back_on_bad_llm_output_or_errors(
    settings: Settings,
    fake_chain: FakeChain,
    result: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_chain.result = result
    with caplog.at_level(logging.WARNING, logger="cloudy_api.services.lyrics_arranger"):
        arrangement = await LyricsArranger(settings, chain=fake_chain).arrange(STANZAS)

    assert arrangement.arranged_by == "fallback"
    assert arrangement.slides == fallback_plan(STANZAS)
    assert "lyrics.llm_failed" in caplog.text


async def test_arranger_without_api_key_uses_fallback(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    assert settings.openrouter_api_key is None
    with caplog.at_level(logging.WARNING, logger="cloudy_api.services.lyrics_arranger"):
        arranger = LyricsArranger(settings)
    arrangement = await arranger.arrange(STANZAS)

    assert arrangement.arranged_by == "fallback"
    assert "lyrics.llm_disabled" in caplog.text


async def test_arranger_falls_back_when_the_llm_exceeds_the_timeout(
    settings: Settings, fake_chain: FakeChain
) -> None:
    fake_chain.result = GOOD_PLAN
    fake_chain.delay = 1

    arrangement = await LyricsArranger(settings, chain=fake_chain).arrange(STANZAS, timeout=0.01)

    assert arrangement.arranged_by == "fallback"
    assert arrangement.slides == fallback_plan(STANZAS)


@pytest.mark.parametrize("timeout", [0, -5])
async def test_arranger_skips_the_llm_without_time_left(
    settings: Settings, fake_chain: FakeChain, timeout: float
) -> None:
    fake_chain.result = GOOD_PLAN

    arrangement = await LyricsArranger(settings, chain=fake_chain).arrange(STANZAS, timeout=timeout)

    assert arrangement.arranged_by == "fallback"
    assert fake_chain.calls == []


async def test_arranger_uses_the_llm_within_the_timeout(
    settings: Settings, fake_chain: FakeChain
) -> None:
    fake_chain.result = GOOD_PLAN

    arrangement = await LyricsArranger(settings, chain=fake_chain).arrange(STANZAS, timeout=5)

    assert arrangement.arranged_by == "llm"


# ---------------------------------------------------------------- build_chain
def test_build_chain_needs_a_key(settings: Settings) -> None:
    assert build_chain(settings) is None


def test_build_chain_targets_openrouter(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[ChatOpenAI] = []
    real_init = ChatOpenAI.__init__

    def spy(self: ChatOpenAI, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(ChatOpenAI, "__init__", spy)
    settings = make_settings(
        openrouter_api_key="sk-or-test",
        openrouter_model="vendor/model",
        llm_timeout_seconds=12,
    )

    chain = build_chain(settings)

    assert chain is not None
    (model,) = created
    assert model.model_name == "vendor/model"
    assert model.openai_api_base == "https://openrouter.ai/api/v1"
    assert model.openai_api_key is not None
    assert model.openai_api_key.get_secret_value() == "sk-or-test"
    assert model.request_timeout == 12
    assert model.temperature is None  # sampling settings come from the OpenRouter preset
