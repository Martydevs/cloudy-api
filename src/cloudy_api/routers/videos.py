import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from ..deps import DownloaderDep, downloader_http_error
from ..schemas import VIDEO_ID_PATTERN, VideoInfo
from ..security import require_api_key
from ..services.downloader import DownloaderError

router = APIRouter(
    prefix="/v1/videos",
    tags=["videos"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/{video_id}")
async def get_video(
    video_id: Annotated[str, Path(pattern=VIDEO_ID_PATTERN)],
    downloader: DownloaderDep,
) -> VideoInfo:
    try:
        return await asyncio.to_thread(downloader.probe, video_id)
    except DownloaderError as exc:
        raise downloader_http_error(exc) from exc
