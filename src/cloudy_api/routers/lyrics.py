import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..deps import ArrangerDep
from ..schemas import LyricsRequest
from ..security import require_api_key
from ..services.lyrics_parser import LyricsError, parse_lyrics_html
from ..services.pptx_builder import PPTX_MEDIA_TYPE, build_presentation, presentation_filename

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
        parsed = await asyncio.to_thread(parse_lyrics_html, body.html)
    except LyricsError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    title = body.title or parsed.title
    arrangement = await arranger.arrange(parsed.stanzas)
    content = await asyncio.to_thread(
        build_presentation,
        arrangement.slides,
        title=title,
        font_color=body.font_color.removeprefix("#"),
    )

    ascii_name, full_name = presentation_filename(title)
    return Response(
        content,
        media_type=PPTX_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(full_name)}'
            ),
            "X-Lyrics-Arranged-By": arrangement.arranged_by,
            "X-Slide-Count": str(len(arrangement.slides)),
        },
    )
