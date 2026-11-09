from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from .services.downloader import DownloaderError, VideoDownloader
from .services.jobs import JobManager
from .services.lyrics_arranger import LyricsArranger
from .services.youtube_api import YouTubeMetadata


def get_downloader(request: Request) -> VideoDownloader:
    # Lives on app.state so the background runner (outside request scope)
    # and the routes share one instance; swap it via app.dependency_overrides.
    return request.app.state.downloader


def get_pytubefix_downloader(request: Request) -> VideoDownloader:
    # Engine behind /v2 (temporary bot-check workaround).
    return request.app.state.pytubefix_downloader


def get_metadata(request: Request) -> YouTubeMetadata:
    # Shared by /v1 and /v2; falls back to the route's engine without an API key.
    return request.app.state.metadata


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def get_arranger(request: Request) -> LyricsArranger:
    return request.app.state.arranger


DownloaderGetter = Callable[[Request], VideoDownloader]

DownloaderDep = Annotated[VideoDownloader, Depends(get_downloader)]
MetadataDep = Annotated[YouTubeMetadata, Depends(get_metadata)]
JobManagerDep = Annotated[JobManager, Depends(get_job_manager)]
ArrangerDep = Annotated[LyricsArranger, Depends(get_arranger)]


def downloader_http_error(exc: DownloaderError) -> HTTPException:
    return HTTPException(
        status_code=422 if exc.client_error else 502,
        detail={"code": exc.code, "message": exc.message},
    )
