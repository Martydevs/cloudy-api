"""Render lyric slides as a .pptx with python-pptx.

Layout: 16:9, no title slide, one compact text box per slide pinned
top-center, with centered text in plain Arial. The box and the slide background have no fill,
so the text can be laid over a video or backdrop.
"""

import math
import re
import unicodedata
from io import BytesIO

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.presentation import Presentation as PresentationType
from pptx.util import Emu, Inches, Pt

from .lyrics_arranger import Slide

PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
FONT_NAME = "Arial"
DEFAULT_FONT_COLOR = "000000"

SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)
# A compact box near the top edge, centered horizontally: room for two lines at
# the largest size, or for wrapped lines at a smaller one.
_BOX_WIDTH = Inches(10.5)
_BOX_HEIGHT = Inches(2.6)
_BOX_LEFT = Emu((SLIDE_WIDTH - _BOX_WIDTH) // 2)
_MARGIN_TOP = Inches(0.4)

MAX_FONT_PT = 40
MIN_FONT_PT = 24
_LINE_SPACING = 1.2  # of the font size
_PARAGRAPH_GAP = 0.5  # of the font size
_AVG_CHAR_WIDTH = 0.52  # of the font size, for Arial
_BLANK_LAYOUT = 6


def font_size_for(slide: Slide) -> int:
    """Largest size in [MIN_FONT_PT, MAX_FONT_PT] at which the slide's text fits the box."""
    box_width_pt = _BOX_WIDTH.pt
    box_height_pt = _BOX_HEIGHT.pt
    for size in range(MAX_FONT_PT, MIN_FONT_PT, -2):
        chars_per_line = box_width_pt / (size * _AVG_CHAR_WIDTH)
        lines = sum(
            max(1, math.ceil(len(line) / chars_per_line)) for paragraph in slide for line in paragraph
        )
        height = size * (_LINE_SPACING * lines + _PARAGRAPH_GAP * (len(slide) - 1))
        if height <= box_height_pt:
            return size
    return MIN_FONT_PT


def _add_slide(prs: PresentationType, slide: Slide, color: RGBColor) -> None:
    page = prs.slides.add_slide(prs.slide_layouts[_BLANK_LAYOUT])
    page.background.fill.background()  # no fill

    box = page.shapes.add_textbox(_BOX_LEFT, _MARGIN_TOP, _BOX_WIDTH, _BOX_HEIGHT)
    box.fill.background()
    box.line.fill.background()

    frame = box.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.vertical_anchor = MSO_ANCHOR.TOP

    size = font_size_for(slide)
    for index, lines in enumerate(slide):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = PP_ALIGN.CENTER
        paragraph.line_spacing = _LINE_SPACING
        if index < len(slide) - 1:
            paragraph.space_after = Pt(size * _PARAGRAPH_GAP)
        for line_index, line in enumerate(lines):
            if line_index:
                paragraph.add_line_break()
            run = paragraph.add_run()
            run.text = line
            run.font.name = FONT_NAME
            run.font.size = Pt(size)
            run.font.bold = False
            run.font.color.rgb = color


def build_presentation(
    slides: list[Slide], *, title: str | None = None, font_color: str = DEFAULT_FONT_COLOR
) -> bytes:
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT
    prs.core_properties.title = title or "Lyrics"
    prs.core_properties.author = "Cloudy"

    color = RGBColor.from_string(font_color.upper())
    for slide in slides:
        _add_slide(prs, slide, color)

    buffer = BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


def presentation_filename(title: str | None) -> tuple[str, str]:
    """Return ``(ascii_name, full_name)`` for a Content-Disposition header."""
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', " ", title or "").strip(" .")
    stem = re.sub(r"\s+", " ", stem)[:100].strip() or "lyrics"
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    ascii_stem = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 _-]+", "", ascii_stem)).strip() or "lyrics"
    return f"{ascii_stem}.pptx", f"{stem}.pptx"
