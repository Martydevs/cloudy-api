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

ZIP_MEDIA_TYPE = "application/zip"

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/lyrics",
    tags=["lyrics"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/pptx",
    response_class=Response,
    responses={
        200: {
            "description": "The presentation. `X-Lyrics-Arranged-By` is `llm` or `fallback`.",
            "content": {PPTX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def create_lyrics_pptx(body: LyricsRequest, arranger: ArrangerDep) -> Response:
    """Turn Letras lyrics HTML into a .pptx with two paragraphs per slide."""
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
    responses={
        200: {
            "description": (
                "A ZIP with one `.pptx` per converted song plus `manifest.json` "
                "(`{items: [{index, status, filename, title, arranged_by, slide_count, error}]}`). "
                "Failed items are listed in the manifest only. "
                "`X-Batch-Succeeded` / `X-Batch-Failed` carry the counts."
            ),
            "content": {ZIP_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def create_lyrics_pptx_batch(
    body: LyricsBatchRequest, arranger: ArrangerDep, settings: SettingsDep
) -> Response:
    """Turn several Letras lyrics HTML items into presentations, one at a time, in a ZIP."""
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
