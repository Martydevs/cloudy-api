import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..deps import ArrangerDep
from ..schemas import ErrorInfo, LyricsBatchRequest, LyricsRequest
from ..security import SettingsDep, require_api_key
from ..services.lyrics_parser import LyricsError
from ..services.lyrics_pptx import (
    LyricsPptx,
    batch_manifest,
    build_batch_zip,
    generate_lyrics_pptx,
)
from ..services.pptx_builder import PPTX_MEDIA_TYPE
from .responses import UNAUTHORIZED, error

ZIP_MEDIA_TYPE = "application/zip"

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/lyrics",
    tags=["lyrics"],
    dependencies=[Depends(require_api_key)],
    responses={
        **UNAUTHORIZED,
        413: error("The body is larger than `MAX_REQUEST_BYTES` (`request_too_large`)."),
    },
)


@router.post(
    "/pptx",
    response_class=Response,
    summary="Convert lyrics to a PowerPoint",
    responses={
        200: {
            "description": (
                "The presentation. `X-Lyrics-Arranged-By` is `llm` or `fallback`; "
                "`X-Slide-Count` is the number of slides."
            ),
            "content": {PPTX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        },
        422: error("No lyrics found (`no_lyrics`) or more than 300 lines (`lyrics_too_long`)."),
    },
)
async def create_lyrics_pptx(body: LyricsRequest, arranger: ArrangerDep) -> Response:
    """Turn lyrics HTML scraped from Letras (or plain text) into a 16:9 `.pptx`,
    two lines per slide, each stanza starting on a new slide. Synchronous: the
    response body is the file.
    """
    try:
        pptx = await generate_lyrics_pptx(body, arranger)
    except LyricsError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    return Response(
        pptx.content,
        media_type=PPTX_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{pptx.ascii_name}"; '
                f"filename*=UTF-8''{quote(pptx.full_name)}"
            ),
            "X-Lyrics-Arranged-By": pptx.arranged_by,
            "X-Slide-Count": str(pptx.slide_count),
        },
    )


@router.post(
    "/pptx/batch",
    response_class=Response,
    summary="Convert several lyrics to PowerPoints (ZIP)",
    responses={
        200: {
            "description": (
                "A ZIP with one `.pptx` per converted song plus `manifest.json` "
                "(`{items: [{index, status, filename, title, arranged_by, slide_count, error}]}`). "
                "Failed items are listed in the manifest only. "
                "`X-Batch-Succeeded` / `X-Batch-Failed` carry the counts."
            ),
            "content": {ZIP_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        },
        422: error(
            "`batch_too_large` (more than `MAX_LYRICS_BATCH` items) or `all_items_failed` "
            "(the detail then has `items`, shaped like the manifest entries)."
        ),
    },
)
async def create_lyrics_pptx_batch(
    body: LyricsBatchRequest, arranger: ArrangerDep, settings: SettingsDep
) -> Response:
    """Convert 1 to `MAX_LYRICS_BATCH` (default 3) songs, one at a time, into a ZIP
    of `.pptx` files plus `manifest.json`. Partial success is normal: failed
    songs only appear in the manifest.
    """
    if len(body.items) > settings.max_lyrics_batch:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "batch_too_large",
                "message": f"At most {settings.max_lyrics_batch} lyrics per request",
            },
        )

    # Strictly sequential: one parse/LLM call/render at a time keeps memory flat
    # on the small Render instance and doesn't burst the shared LLM. The LLM
    # shares one deadline across the batch; songs past it use the fallback
    # grouping, so a slow LLM can't push the response past the caller's timeout.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.lyrics_batch_deadline_seconds
    outcomes: list[LyricsPptx | ErrorInfo] = []
    for index, item in enumerate(body.items):
        try:
            pptx = await generate_lyrics_pptx(item, arranger, llm_timeout=deadline - loop.time())
        except LyricsError as exc:
            outcomes.append(ErrorInfo(code=exc.code, message=exc.message))
        except Exception:
            # One bad song must not throw away the ones already converted.
            log.exception("lyrics.batch_item_failed index=%d", index)
            outcomes.append(
                ErrorInfo(
                    code="internal_error",
                    message="Unexpected error while converting these lyrics",
                )
            )
        else:
            outcomes.append(pptx)

    manifest, files = batch_manifest(outcomes)
    if not files:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "all_items_failed",
                "message": "None of the lyrics could be converted",
                "items": [item.model_dump(mode="json") for item in manifest.items],
            },
        )

    return Response(
        build_batch_zip(manifest, files),
        media_type=ZIP_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="lyrics.zip"',
            "X-Batch-Succeeded": str(len(files)),
            "X-Batch-Failed": str(len(manifest.items) - len(files)),
        },
    )
