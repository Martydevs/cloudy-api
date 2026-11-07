from io import BytesIO

import pytest
from pptx import Presentation
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.presentation import Presentation as PresentationType

from cloudy_api.services.pptx_builder import (
    FONT_NAME,
    MAX_FONT_PT,
    MIN_FONT_PT,
    SLIDE_HEIGHT,
    SLIDE_WIDTH,
    build_presentation,
    font_size_for,
    presentation_filename,
)

SLIDES = [
    [["Line one"], ["Line two"]],
    [["Solo line"]],
]


def load(data: bytes) -> PresentationType:
    return Presentation(BytesIO(data))


def test_one_slide_per_group_with_original_text() -> None:
    prs = load(build_presentation(SLIDES, title="My Song"))

    assert len(prs.slides) == 2
    first = prs.slides[0].shapes[0].text_frame
    assert [p.text for p in first.paragraphs] == ["Line one", "Line two"]
    assert prs.slides[1].shapes[0].text_frame.text == "Solo line"
    assert prs.core_properties.title == "My Song"


def test_multi_line_paragraphs_still_render_with_line_breaks() -> None:
    frame = load(build_presentation([[["a", "b"], ["c"]]])).slides[0].shapes[0].text_frame
    assert [p.text.split("\x0b") for p in frame.paragraphs] == [["a", "b"], ["c"]]


def test_widescreen_slides() -> None:
    prs = load(build_presentation(SLIDES))
    assert prs.slide_width == SLIDE_WIDTH
    assert prs.slide_height == SLIDE_HEIGHT
    assert prs.core_properties.title == "Lyrics"


def test_text_is_top_centered_basic_and_transparent() -> None:
    prs = load(build_presentation(SLIDES))
    slide = prs.slides[0]
    box = slide.shapes[0]
    frame = box.text_frame

    assert frame.vertical_anchor == MSO_ANCHOR.TOP
    for paragraph in frame.paragraphs:
        assert paragraph.alignment == PP_ALIGN.CENTER
        for run in paragraph.runs:
            assert run.font.name == FONT_NAME
            assert run.font.bold is False
            assert run.font.italic is None  # nothing fancy

    # No fill on the text box or the slide background, no outline.
    xml = box._element.xml
    assert "<a:noFill/>" in xml
    assert "solidFill" not in xml.split("<p:txBody>")[0]
    assert "<p:bg>" in slide._element.xml
    assert "<a:noFill/>" in slide._element.xml.split("</p:bg>")[0]


def test_box_is_compact_and_pinned_top_center() -> None:
    prs = load(build_presentation(SLIDES))
    for slide in prs.slides:
        (box,) = slide.shapes
        assert box.left + box.width / 2 == pytest.approx(SLIDE_WIDTH / 2, abs=2)  # centered
        assert box.left > 0
        assert box.width < SLIDE_WIDTH * 0.85  # not edge to edge
        assert box.top < SLIDE_HEIGHT * 0.1  # near the top edge
        assert box.height < SLIDE_HEIGHT * 0.4  # compact, not a near-full-slide box


def test_font_color() -> None:
    default = load(build_presentation(SLIDES)).slides[0].shapes[0].text_frame.paragraphs[0].runs[0]
    white = load(build_presentation(SLIDES, font_color="ffffff")).slides[0]
    assert str(default.font.color.rgb) == "000000"
    assert str(white.shapes[0].text_frame.paragraphs[0].runs[0].font.color.rgb) == "FFFFFF"


def test_paragraphs_are_spaced_apart_but_not_the_last() -> None:
    paragraphs = load(build_presentation(SLIDES)).slides[0].shapes[0].text_frame.paragraphs
    assert paragraphs[0].space_after is not None
    assert paragraphs[1].space_after is None


def test_font_size_shrinks_only_when_lines_wrap() -> None:
    short = [["Jay Jay"], ["Musiko"]]
    typical = [["A veces despierto con ganas"], ["Y seguir durmiendo, vaya"]]  # < 36 chars
    one_wraps = [["x" * 50], ["y" * 20]]
    both_wrap = [["x" * 50], ["y" * 50]]
    very_long_lines = [["z" * 200], ["z" * 200]]

    assert font_size_for(short) == MAX_FONT_PT
    assert font_size_for(typical) == MAX_FONT_PT
    assert font_size_for(one_wraps) == MAX_FONT_PT  # three lines still fit at full size
    assert MIN_FONT_PT < font_size_for(both_wrap) < MAX_FONT_PT
    assert font_size_for(very_long_lines) == MIN_FONT_PT


def test_all_runs_use_the_computed_size() -> None:
    slide = SLIDES[0]
    runs = load(build_presentation([slide])).slides[0].shapes[0].text_frame.paragraphs[0].runs
    assert {run.font.size.pt for run in runs} == {font_size_for(slide)}


@pytest.mark.parametrize(
    ("title", "ascii_name", "full_name"),
    [
        ("De Buenas Part", "De Buenas Part.pptx", "De Buenas Part.pptx"),
        ("Cómo Tú — Él", "Como Tu El.pptx", "Cómo Tú — Él.pptx"),
        ('a/b\\c:d*e?"f<g>h|i\r\nj', "a b c d e f g h i j.pptx", "a b c d e f g h i j.pptx"),
        ("  ..  ", "lyrics.pptx", "lyrics.pptx"),
        (None, "lyrics.pptx", "lyrics.pptx"),
        ("Ñ" * 3, "N" * 3 + ".pptx", "ÑÑÑ.pptx"),
        ("日本語", "lyrics.pptx", "日本語.pptx"),
        ("x" * 300, "x" * 100 + ".pptx", "x" * 100 + ".pptx"),
    ],
)
def test_presentation_filename(title: str | None, ascii_name: str, full_name: str) -> None:
    assert presentation_filename(title) == (ascii_name, full_name)
