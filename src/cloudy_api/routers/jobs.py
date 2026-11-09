import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse

from ..config import Settings
from ..deps import (
    DownloaderGetter,
    JobManagerDep,
    downloader_http_error,
    get_downloader,
    get_pytubefix_downloader,
)
from ..schemas import Engine, JobCreate, JobCreated, JobOut, JobStatus, VideoInfo
from ..security import SettingsDep, require_api_key, sign, verify_signature
from ..services.downloader import DownloaderError, VideoDownloader
from ..services.jobs import Job, QueueFullError

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".zip": "application/zip",
}


def _not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "not_found", "message": "Job not found or expired"},
    )


async def _preflight(
    downloader: VideoDownloader, video_ids: list[str], settings: Settings
) -> dict[str, str]:
    """Probe every video before queueing; returns ``{video_id: title}``."""
    limiter = asyncio.Semaphore(settings.probe_concurrency)

    async def probe(video_id: str) -> VideoInfo | DownloaderError:
        async with limiter:
            try:
                return await asyncio.to_thread(downloader.probe, video_id)
            except DownloaderError as exc:
                return exc

    results = await asyncio.gather(*(probe(v) for v in video_ids))

    problems: list[dict[str, str]] = []
    for video_id, result in zip(video_ids, results, strict=True):
        if isinstance(result, DownloaderError):
            if not result.client_error:
                raise downloader_http_error(result)
            problems.append({"video_id": video_id, "code": result.code, "message": result.message})
        elif not result.downloadable:
            reason = result.reason or "not_downloadable"
            message = (
                f"Longer than {settings.max_duration_seconds // 60} minutes"
                if reason == "too_long"
                else "Video can't be downloaded"
            )
            problems.append({"video_id": video_id, "code": reason, "message": message})

    if problems:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "videos_rejected",
                "message": "Some videos can't be downloaded",
                "videos": problems,
            },
        )
    return {
        info.video_id: info.title
        for info in results
        if isinstance(info, VideoInfo)
    }


def _job_out(job: Job, request: Request, settings: SettingsDep, prefix: str) -> JobOut:
    out = JobOut(
        job_id=job.id,
        status=job.status,
        format=job.format,
        progress=job.progress,
        items=[item.to_out() for item in job.items],
    )
    if job.status is JobStatus.DONE and job.file_path is not None and job.finished_at:
        exp, sig = sign(settings, job.id)
        base = settings.public_base_url or str(request.base_url).rstrip("/")
        out.download_url = f"{base}{prefix}/jobs/{job.id}/file?exp={exp}&sig={sig}"
        out.filename = job.filename
        out.size_bytes = job.size_bytes
        out.expires_at = job.finished_at + settings.file_ttl_seconds
    return out


def build_routers(
    prefix: str,
    engine: Engine,
    get_engine: DownloaderGetter,
    *,
    tag: str = "jobs",
    files_tag: str = "files",
) -> tuple[APIRouter, APIRouter]:
    """Job routes under ``{prefix}/jobs`` whose jobs run on ``engine``.

    Returns ``(router, files_router)``; the signed file route has no API key
    because the browser calls it directly. Every prefix serves any job id
    (one shared JobManager), but download links point at the job's own prefix.
    """
    router = APIRouter(
        prefix=f"{prefix}/jobs",
        tags=[tag],
        dependencies=[Depends(require_api_key)],
    )
    files_router = APIRouter(prefix=f"{prefix}/jobs", tags=[files_tag])

    @router.post("", status_code=status.HTTP_202_ACCEPTED)
    async def create_job(
        body: JobCreate,
        settings: SettingsDep,
        downloader: Annotated[VideoDownloader, Depends(get_engine)],
        manager: JobManagerDep,
    ) -> JobCreated:
        if len(body.video_ids) > settings.max_batch:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "batch_too_large", "message": f"At most {settings.max_batch} videos per job"},
            )
        if manager.queue.full():
            # Fail fast before spending time on probes.
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "queue_full", "message": "Too many jobs queued, retry later"},
            )
        titles = await _preflight(downloader, body.video_ids, settings)
        try:
            job = manager.submit(body.video_ids, body.format, titles, engine=engine)
        except QueueFullError as exc:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"code": "queue_full", "message": "Too many jobs queued, retry later"},
            ) from exc
        return JobCreated(job_id=job.id, status=job.status)

    @router.get("/{job_id}")
    async def get_job(
        job_id: str, request: Request, settings: SettingsDep, manager: JobManagerDep
    ) -> JobOut:
        job = manager.get(job_id)
        if job is None:
            raise _not_found()
        return _job_out(job, request, settings, prefix)

    @router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_job(job_id: str, manager: JobManagerDep) -> Response:
        if not manager.delete(job_id):
            raise _not_found()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @files_router.get("/{job_id}/file", response_class=FileResponse)
    async def download_file(
        job_id: str,
        exp: Annotated[int, Query()],
        sig: Annotated[str, Query(max_length=128)],
        settings: SettingsDep,
        manager: JobManagerDep,
    ) -> FileResponse:
        verify_signature(settings, job_id, exp, sig)
        job = manager.get(job_id)
        if job is None:
            raise _not_found()
        if job.status is JobStatus.EXPIRED:
            raise HTTPException(
                status.HTTP_410_GONE,
                detail={"code": "expired", "message": "File expired, create a new job"},
            )
        if job.status is not JobStatus.DONE or job.file_path is None or not job.file_path.is_file():
            raise _not_found()
        return FileResponse(
            job.file_path,
            filename=job.filename,
            media_type=_MEDIA_TYPES.get(job.file_path.suffix, "application/octet-stream"),
        )

    return router, files_router


router, files_router = build_routers("/v1", Engine.YTDLP, get_downloader)
# /v2: same contract, pytubefix instead of yt-dlp (temporary bot-check workaround).
v2_router, v2_files_router = build_routers(
    "/v2", Engine.PYTUBEFIX, get_pytubefix_downloader, tag="jobs v2", files_tag="files v2"
)
