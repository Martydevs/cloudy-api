import logging
from collections.abc import Callable
from typing import Any

import pytest
from conftest import FakeChain
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from cloudy_api.config import Settings
from cloudy_api.services import lyrics_arranger
from cloudy_api.services.lyrics_arranger import (
    MAX_PARAGRAPH_LINES,
    MAX_SLIDE_LINES,
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


# --------------------------------------------------------------- resolve_plan
def test_resolve_plan_returns_original_text() -> None:
    slides = resolve_plan(LLMPlan.model_validate(plan([(1, 4), (5, 6)], [(7, 9)])), LINES)
    assert slides == [[["a1", "a2", "a3", "a4"], ["b1", "b2"]], [["c1", "c2", "c3"]]]


@pytest.mark.parametrize(
    "bad",
    [
        plan([(1, 4), (6, 9)]),  # skips line 5
        plan([(1, 4), (4, 9)]),  # overlaps line 4
        plan([(2, 9)]),  # doesn't start at 1
        plan([(1, 4)], [(5, 6)]),  # stops early
        plan([(1, 4)], [(5, 10)]),  # runs past the end
        plan([(1, 4)], [(5, 4)], [(5, 9)]),  # end before start
        {"slides": []},  # empty plan
        plan([(5, 9)], [(1, 4)]),  # reordered
    ],
)
def test_resolve_plan_rejects_unsound_plans(bad: dict[str, Any]) -> None:
    with pytest.raises(PlanError):
        resolve_plan(LLMPlan.model_validate(bad), LINES)


def test_resolve_plan_enforces_size_limits() -> None:
    lines = [f"l{i}" for i in range(1, 30)]
    too_long_paragraph = plan([(1, MAX_PARAGRAPH_LINES + 1)], [(MAX_PARAGRAPH_LINES + 2, 29)])
    with pytest.raises(PlanError, match="paragraph longer"):
        resolve_plan(LLMPlan.model_validate(too_long_paragraph), lines)

    # Two full paragraphs sit exactly at the slide limit and are fine...
    assert MAX_SLIDE_LINES == 2 * MAX_PARAGRAPH_LINES
    full = 2 * MAX_PARAGRAPH_LINES
    ok = resolve_plan(
        LLMPlan.model_validate(plan([(1, MAX_PARAGRAPH_LINES), (MAX_PARAGRAPH_LINES + 1, full)])),
        lines[:full],
    )
    assert [len(p) for p in ok[0]] == [MAX_PARAGRAPH_LINES, MAX_PARAGRAPH_LINES]


def test_resolve_plan_rejects_slides_over_the_line_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only reachable when the slide limit is below paragraphs-per-slide * paragraph limit.
    monkeypatch.setattr(lyrics_arranger, "MAX_SLIDE_LINES", 5)
    with pytest.raises(PlanError, match="more than 5 lines"):
        resolve_plan(LLMPlan.model_validate(plan([(1, 4), (5, 6)], [(7, 9)])), LINES)


def test_plan_schema_allows_one_or_two_paragraphs() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        LLMPlan.model_validate(plan([(1, 2), (3, 4), (5, 6)]))
    with pytest.raises(ValueError):  # noqa: PT011
        LLMPlan.model_validate({"slides": [{"paragraphs": []}]})


# ------------------------------------------------------------------- fallback
def test_fallback_pairs_stanzas() -> None:
    assert fallback_plan(STANZAS) == [[STANZAS[0], STANZAS[1]], [STANZAS[2]]]


def test_fallback_splits_long_stanzas_evenly() -> None:
    long = [f"l{i}" for i in range(1, 11)]  # 10 lines -> 3 parts of 4/4/2
    slides = fallback_plan([long])
    paragraphs = [p for slide in slides for p in slide]
    assert [len(p) for p in paragraphs] == [4, 4, 2]
    assert [line for p in paragraphs for line in p] == long
    assert [len(slide) for slide in slides] == [2, 1]


def test_fallback_keeps_stanzas_up_to_the_limit_whole() -> None:
    stanza = [f"l{i}" for i in range(MAX_PARAGRAPH_LINES)]
    assert fallback_plan([stanza]) == [[stanza]]


def test_fallback_of_nothing() -> None:
    assert fallback_plan([]) == []


# --------------------------------------------------------------- the arranger
async def test_arranger_uses_the_llm_plan(settings: Settings, fake_chain: FakeChain) -> None:
    fake_chain.result = plan([(1, 4)], [(5, 6), (7, 9)])
    arrangement = await LyricsArranger(settings, chain=fake_chain).arrange(STANZAS, "My Song")

    assert arrangement.arranged_by == "llm"
    assert arrangement.slides == [[STANZAS[0]], [STANZAS[1], STANZAS[2]]]


async def test_arranger_sends_numbered_lines_and_title(
    settings: Settings, fake_chain: FakeChain
) -> None:
    fake_chain.result = LLMPlan.model_validate(plan([(1, 4), (5, 6)], [(7, 9)]))  # a model works too
    arranger = LyricsArranger(settings, chain=fake_chain)
    await arranger.arrange(STANZAS, "My Song")
    await arranger.arrange(STANZAS)

    system, human = fake_chain.calls[0]
    assert isinstance(system, SystemMessage)
    assert isinstance(human, HumanMessage)
    assert human.content.startswith("Song: My Song\n\nLyrics:\n\n1: a1\n2: a2")
    assert "9: c3" in human.content
    assert fake_chain.calls[1][1].content.startswith("Lyrics:\n\n1: a1")


@pytest.mark.parametrize(
    "result",
    [
        plan([(1, 4)]),  # doesn't cover everything
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
    assert model.temperature == 0
