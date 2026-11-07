# Plan: 2 lines per lyrics slide, compact top-center placement

## Context
`POST /v1/lyrics/pptx` builds a PowerPoint from Letras.com lyrics. Today a slide holds up to 2 *stanzas*, and a stanza can have up to 6 lines. A slide can therefore carry up to 12 lines, which looks like 3+ paragraphs and fills the slide. Required (confirmed with the user):
- Each slide shows exactly **2 paragraphs**, where a paragraph is **one verse line**.
- The text sits **top center**: a compact box near the top edge, not a near-full-slide box.

Relevant code (all under `src/cloudy_api/services/`):
- `lyrics_arranger.py` decides the grouping. `Slide = list[Paragraph]` and `Paragraph = list[str]` (lines). Constants are `PARAGRAPHS_PER_SLIDE=2`, `MAX_PARAGRAPH_LINES=6`, `MAX_SLIDE_LINES=12`. It has an LLM path (`LLMPlan`, `resolve_plan`, system prompt) and a deterministic `fallback_plan`.
- `pptx_builder.py` lays the slides out. The box is 12.13in x 6.7in at left 0.6in and top 0.4in, anchored top with centered text. `font_size_for` picks 40 to 24pt so the text fits the box.

## Changes

### 1. Grouping: `lyrics_arranger.py`
- Make a paragraph exactly one line, so a slide has 2 paragraphs of 1 line each.
  - Set `MAX_PARAGRAPH_LINES = 1` and `MAX_SLIDE_LINES = 2`. Keep `PARAGRAPHS_PER_SLIDE = 2`.
  - Remove `_SPLIT_TARGET_LINES` and `_split_long`, which are no longer needed.
- Rewrite `fallback_plan`:
  - Take each stanza's lines in order and chunk them in twos.
  - Each line becomes its own `Paragraph` (`[line]`).
  - Never carry lines across a stanza boundary, so a new stanza starts on a fresh slide.
  - An odd stanza ends with a single-line slide, for example 3 lines become 2+1.
- Update `SYSTEM_PROMPT` to match: 2 single-line paragraphs per slide, stanza breaks start a new slide, and a lone line only at the end of an odd stanza.
- Add a stanza-boundary check to `resolve_plan`. A model slide that spans two stanzas raises `PlanError`, which falls back to `fallback_plan`. This needs stanza sizes passed in, or the plan checked against stanza end indexes. `arrange()` already has `stanzas` and `number_lines` gives the line offsets. If that is too fiddly, drop the check and keep only the 2-line cap.
- The `Slide` and `Paragraph` types stay as they are, so the builder API is unchanged.

### 2. Placement: `pptx_builder.py`
- Replace the full-slide box with a compact, horizontally centered one:
  - `_BOX_WIDTH = Inches(10.5)`.
  - `_BOX_LEFT = Emu((SLIDE_WIDTH - _BOX_WIDTH) // 2)`.
  - `_MARGIN_TOP = Inches(0.4)`.
  - `_BOX_HEIGHT` about `Inches(2.6)`. This fits 2 lines at 40pt, with room for one wrapped line each.
- Use `_BOX_LEFT` in `add_textbox`. Remove `_MARGIN_X`.
- Keep `MSO_ANCHOR.TOP`, `PP_ALIGN.CENTER`, no fill, no outline and Arial. Keep `font_size_for`, which now shrinks only when a line wraps. Its height maths already uses `_BOX_WIDTH` and `_BOX_HEIGHT`.
- Update the module docstring: "one compact text box pinned top-center".

### 3. Tests and docs
- `tests/unit/test_lyrics_arranger.py`:
  - `fallback_plan` yields at most 2 single-line paragraphs per slide.
  - Stanzas don't merge across slides.
  - An odd stanza gives a trailing 1-line slide.
  - `resolve_plan` rejects a paragraph longer than 1 line and a slide longer than 2 lines.
  - Update the old expectations about 6-line paragraphs and 12-line slides.
- `tests/unit/test_pptx_builder.py`:
  - The box is centered: `left + width/2 == slide_width/2`.
  - The box top is near the top edge and its height is under about 40% of the slide height.
  - Update the existing "centered box" and font-size assertions to the new dimensions.
- `README.md` ("Lyrics to PowerPoint", lines ~139-185): change the arrange and render steps to "2 lines per slide, top-center box".
- Check `tests/unit/test_routes_lyrics.py` for `X-Slide-Count` assertions that depend on the old grouping.
- Coverage gate is `fail_under = 90`.

## Verification
1. Run `uv run pytest`. All tests pass and coverage stays at or above 90%.
2. Generate a deck without an LLM key, using the fallback. Start `bun run dev`, then `curl -X POST /v1/lyrics/pptx` with a sample Letras HTML that has stanzas of 3, 4 and 8 lines (API key header as in the README).
3. Open the saved `.pptx` with python-pptx. Check that every slide has 2 paragraphs of one line each, that the box is centered and near the top, and that `X-Slide-Count` equals the slide count.
4. Render the deck to images (LibreOffice to PDF to PNG, if installed) and check that the text sits top-center.

## Note
The LLM only chooses the ranges, and the hard rules now fix almost everything, so its value is small. Dropping it for a pure deterministic grouping (removing langchain, OpenRouter settings and the LLM tests) would be a separate cleanup. It is not part of this plan.
