import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from ..deps import DownloaderGetter, downloader_http_error, get_downloader, get_pytubefix_downloader
from ..schemas import VIDEO_ID_PATTERN, VideoInfo
from ..security import require_api_key
from ..services.downloader import DownloaderError, VideoDownloader


def build_router(prefix: str, get_engine: DownloaderGetter, tag: str = "videos") -> APIRouter:
    """Video metadata routes under ``{prefix}/videos``, backed by ``get_engine``."""
    router = APIRouter(
        prefix=f"{prefix}/videos",
        tags=[tag],
        dependencies=[Depends(require_api_key)],
    )

    @router.get("/{video_id}")
    async def get_video(
        video_id: Annotated[str, Path(pattern=VIDEO_ID_PATTERN)],
        downloader: Annotated[VideoDownloader, Depends(get_engine)],
    ) -> VideoInfo:
        try:
            return await asyncio.to_thread(downloader.probe, video_id)
        except DownloaderError as exc:
            raise downloader_http_error(exc) from exc

    return router


router = build_router("/v1", get_downloader)
# /v2: same contract, pytubefix instead of yt-dlp (temporary bot-check workaround).
v2_router = build_router("/v2", get_pytubefix_downloader, tag="videos v2")
