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
    [["Line one", "Line two"], ["Line three", "Line four"]],
    [["Solo stanza"]],
]


def load(data: bytes) -> PresentationType:
    return Presentation(BytesIO(data))


def test_one_slide_per_group_with_original_text() -> None:
    prs = load(build_presentation(SLIDES, title="My Song"))

    assert len(prs.slides) == 2
    first = prs.slides[0].shapes[0].text_frame
    assert [p.text.split("\x0b") for p in first.paragraphs] == [
        ["Line one", "Line two"],
        ["Line three", "Line four"],
    ]
    assert prs.slides[1].shapes[0].text_frame.text == "Solo stanza"
    assert prs.core_properties.title == "My Song"


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
    assert box.top < SLIDE_HEIGHT * 0.1
    assert box.left + box.width / 2 == pytest.approx(SLIDE_WIDTH / 2, abs=2)  # centered box
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


def test_font_color() -> None:
    default = load(build_presentation(SLIDES)).slides[0].shapes[0].text_frame.paragraphs[0].runs[0]
    white = load(build_presentation(SLIDES, font_color="ffffff")).slides[0]
    assert str(default.font.color.rgb) == "000000"
    assert str(white.shapes[0].text_frame.paragraphs[0].runs[0].font.color.rgb) == "FFFFFF"


def test_paragraphs_are_spaced_apart_but_not_the_last() -> None:
    paragraphs = load(build_presentation(SLIDES)).slides[0].shapes[0].text_frame.paragraphs
    assert paragraphs[0].space_after is not None
    assert paragraphs[1].space_after is None


def test_font_size_shrinks_for_long_or_wrapping_slides() -> None:
    short = [["one", "two"]]
    eight_lines = [["x" * 20] * 4, ["y" * 20] * 4]
    twelve_lines = [["x" * 20] * 6, ["y" * 20] * 6]
    very_long_lines = [["z" * 120] * 6, ["z" * 120] * 6]

    assert font_size_for(short) == MAX_FONT_PT
    assert MIN_FONT_PT < font_size_for(twelve_lines) < MAX_FONT_PT
    assert font_size_for(eight_lines) >= font_size_for(twelve_lines)
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
