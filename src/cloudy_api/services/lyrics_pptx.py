"""The lyrics → .pptx pipeline shared by the single and batch routes.

``generate_lyrics_pptx`` does parse → arrange → render for one song.
``build_batch_zip`` packs batch results into one ZIP with a ``manifest.json``.
"""

import asyncio
import zipfile
from dataclasses import dataclass
from io import BytesIO

from ..schemas import ErrorInfo, LyricsBatchItemOut, LyricsBatchManifest, LyricsRequest
from .lyrics_arranger import ArrangedBy, LyricsArranger
from .lyrics_parser import parse_lyrics_html
from .pptx_builder import build_presentation, presentation_filename

MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class LyricsPptx:
    content: bytes
    title: str | None
    ascii_name: str
    full_name: str
    arranged_by: ArrangedBy
    slide_count: int


async def generate_lyrics_pptx(
    item: LyricsRequest, arranger: LyricsArranger, *, llm_timeout: float | None = None
) -> LyricsPptx:
    """Build one presentation. Raises ``LyricsError`` when the HTML has no usable lyrics.

    The arranger never raises (it falls back on any LLM failure), so ``LyricsError``
    is the only expected failure. ``llm_timeout`` caps the LLM call; ``<= 0`` skips
    it and uses the fallback grouping.
    """
    parsed = await asyncio.to_thread(parse_lyrics_html, item.html)
    title = item.title or parsed.title
    arrangement = await arranger.arrange(parsed.stanzas, timeout=llm_timeout)
    content = await asyncio.to_thread(
        build_presentation,
        arrangement.slides,
        title=title,
        font_color=item.font_color.removeprefix("#"),
    )
    ascii_name, full_name = presentation_filename(title)
    return LyricsPptx(
        content=content,
        title=title,
        ascii_name=ascii_name,
        full_name=full_name,
        arranged_by=arrangement.arranged_by,
        slide_count=len(arrangement.slides),
    )


def unique_name(name: str, taken: set[str]) -> str:
    """Return ``name``, or ``stem (2).ext``, ``stem (3).ext``… if it's already taken.

    Comparison ignores case so entries don't collide when unzipped on Windows/macOS.
    Adds the chosen name to ``taken``.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate, n = name, 1
    while candidate.casefold() in taken:
        n += 1
        candidate = f"{stem} ({n}){dot}{ext}"
    taken.add(candidate.casefold())
    return candidate


def batch_manifest(
    outcomes: list[LyricsPptx | ErrorInfo],
) -> tuple[LyricsBatchManifest, list[tuple[str, bytes]]]:
    """Describe each outcome and pick a unique ZIP entry name for each success.

    Failures are ``ErrorInfo`` rather than the caught exception, so a batch doesn't
    keep tracebacks (and the parse frames they reference) alive until it ends.
    """
    taken = {MANIFEST_NAME}
    items: list[LyricsBatchItemOut] = []
    files: list[tuple[str, bytes]] = []
    for index, outcome in enumerate(outcomes):
        if isinstance(outcome, ErrorInfo):
            items.append(LyricsBatchItemOut(index=index, status="failed", error=outcome))
            continue
        filename = unique_name(outcome.ascii_name, taken)
        files.append((filename, outcome.content))
        items.append(
            LyricsBatchItemOut(
                index=index,
                status="done",
                filename=filename,
                title=outcome.title,
                arranged_by=outcome.arranged_by,
                slide_count=outcome.slide_count,
            )
        )
    return LyricsBatchManifest(items=items), files


def build_batch_zip(manifest: LyricsBatchManifest, files: list[tuple[str, bytes]]) -> bytes:
    """Pack the presentations plus ``manifest.json`` (stored: .pptx is already compressed)."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, content in files:
            zf.writestr(name, content)
        zf.writestr(MANIFEST_NAME, manifest.model_dump_json(indent=2))
    return buffer.getvalue()
