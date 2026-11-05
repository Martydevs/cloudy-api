from typing import Annotated

from fastapi import Depends, HTTPException, Request

from .services.downloader import Downloader, DownloaderError
from .services.jobs import JobManager
from .services.lyrics_arranger import LyricsArranger


def get_downloader(request: Request) -> Downloader:
    # Lives on app.state so the background runner (outside request scope)
    # and the routes share one instance; swap it via app.dependency_overrides.
    return request.app.state.downloader


def get_job_manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def get_arranger(request: Request) -> LyricsArranger:
    return request.app.state.arranger


DownloaderDep = Annotated[Downloader, Depends(get_downloader)]
JobManagerDep = Annotated[JobManager, Depends(get_job_manager)]
ArrangerDep = Annotated[LyricsArranger, Depends(get_arranger)]


def downloader_http_error(exc: DownloaderError) -> HTTPException:
    return HTTPException(
        status_code=422 if exc.client_error else 502,
        detail={"code": exc.code, "message": exc.message},
    )
