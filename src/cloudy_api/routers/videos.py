import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from ..deps import DownloaderDep, MetadataDep, downloader_http_error
from ..schemas import VIDEO_ID_PATTERN, VideoInfo
from ..security import require_api_key
from ..services.downloader import DownloaderError
from .responses import ENGINE_FAILED, UNAUTHORIZED, error

router = APIRouter(
    prefix="/videos",
    tags=["videos"],
    dependencies=[Depends(require_api_key)],
    responses=UNAUTHORIZED,
)


@router.get(
    "/{video_id}",
    summary="Get video metadata",
    responses={
        422: error(
            "The video can't be used: `private`, `unavailable`, `age_restricted`, "
            "`members_only`, `geo_restricted`, `live`, … (or a malformed id)."
        ),
        **ENGINE_FAILED,
    },
)
async def get_video(
    video_id: Annotated[
        str,
        Path(pattern=VIDEO_ID_PATTERN, description="11-character YouTube video id."),
    ],
    downloader: DownloaderDep,
    metadata: MetadataDep,
) -> VideoInfo:
    """Title, duration, thumbnail and channel of a YouTube video, and whether it
    can be downloaded (`downloadable` / `reason`: `too_long`, `live`,
    `unknown_duration`).

    Comes from the YouTube Data API when `YOUTUBE_DATA_API_KEY` is set; then
    `available_heights` is always `[]`. Without the key, pytubefix probes the
    video itself. Results are cached for 10 minutes.
    """
    try:
        return await asyncio.to_thread(metadata.probe, video_id, fallback=downloader)
    except DownloaderError as exc:
        raise downloader_http_error(exc) from exc
