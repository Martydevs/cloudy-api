import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse

from ..config import Settings
from ..deps import DownloaderDep, JobManagerDep, MetadataDep, downloader_http_error
from ..schemas import (
    JobCreate,
    JobCreated,
    JobOut,
    JobStatus,
    VideoInfo,
    VideosRejectedResponse,
)
from ..security import SettingsDep, require_api_key, sign, verify_signature
from ..services.downloader import DownloaderError, VideoDownloader
from ..services.jobs import Job, QueueFullError
from ..services.youtube_api import YouTubeMetadata
from .responses import ENGINE_FAILED, UNAUTHORIZED, error

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".zip": "application/zip",
}

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
    responses=UNAUTHORIZED,
)

# The signed file route has no API key: the browser calls it directly.
files_router = APIRouter(prefix="/jobs", tags=["files"])

_NOT_FOUND = {404: error("Unknown or expired job (`not_found`). Jobs don't survive restarts.")}


def _not_found() -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "not_found", "message": "Job not found or expired"},
    )


async def _preflight(
    metadata: YouTubeMetadata,
    downloader: VideoDownloader,
    video_ids: list[str],
    settings: Settings,
) -> dict[str, str]:
    """Probe every video before queueing; returns ``{video_id: title}``.

    One YouTube Data API call covers the whole batch; without an API key the
    engine probes each video (``PROBE_CONCURRENCY`` at a time).
    """
    try:
        by_id = await asyncio.to_thread(metadata.probe_many, video_ids, fallback=downloader)
    except DownloaderError as exc:
        raise downloader_http_error(exc) from exc
    results = [by_id[video_id] for video_id in video_ids]

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


def _job_out(job: Job, request: Request, settings: SettingsDep) -> JobOut:
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
        path = request.app.url_path_for("download_file", job_id=job.id)
        out.download_url = f"{base}{path}?exp={exp}&sig={sig}"
        out.filename = job.filename
        out.size_bytes = job.size_bytes
        out.expires_at = job.finished_at + settings.file_ttl_seconds
    return out


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a download job",
    responses={
        422: error(
            "`videos_rejected` (with a `videos` list saying why each one can't be "
            "downloaded) or `batch_too_large`.",
            VideosRejectedResponse,
        ),
        429: error("The queue is full (`queue_full`). Retry later."),
        **ENGINE_FAILED,
    },
)
async def create_job(
    body: JobCreate,
    settings: SettingsDep,
    downloader: DownloaderDep,
    metadata: MetadataDep,
    manager: JobManagerDep,
) -> JobCreated:
    """Queue 1 to `MAX_BATCH` (default 10) videos in one format.

    Duplicate ids are dropped. Every video is checked before queueing; if any
    can't be downloaded the whole request is rejected with `422 videos_rejected`.
    Then poll `GET /api/jobs/{job_id}` until `status` is `done` or `failed`.
    """
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
    titles = await _preflight(metadata, downloader, body.video_ids, settings)
    try:
        job = manager.submit(body.video_ids, body.format, titles)
    except QueueFullError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "queue_full", "message": "Too many jobs queued, retry later"},
        ) from exc
    return JobCreated(job_id=job.id, status=job.status)


@router.get("/{job_id}", summary="Get job status", responses=_NOT_FOUND)
async def get_job(
    job_id: str, request: Request, settings: SettingsDep, manager: JobManagerDep
) -> JobOut:
    """Status, overall and per-video progress, and per-video errors.

    Once `status` is `done` the response also has `download_url` (signed, valid
    for `SIGNED_URL_TTL_SECONDS`, default 10 min; poll again for a fresh one),
    `filename`, `size_bytes` and `expires_at` (Unix time the file is deleted).
    One video gives the file itself; several give a ZIP of the ones that worked.
    """
    job = manager.get(job_id)
    if job is None:
        raise _not_found()
    return _job_out(job, request, settings)


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel or delete a job",
    responses=_NOT_FOUND,
)
async def delete_job(job_id: str, manager: JobManagerDep) -> Response:
    """Stop a queued or running job, or delete a finished one's file early."""
    if not manager.delete(job_id):
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@files_router.get(
    "/{job_id}/file",
    response_class=FileResponse,
    summary="Download a job's file",
    responses={
        200: {
            "description": "The file as an attachment (`Content-Disposition` has its name).",
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}}
                for media_type in _MEDIA_TYPES.values()
            },
        },
        403: error("Bad signature (`invalid_signature`) or `link_expired`: poll the job again."),
        410: error("The file was deleted after `FILE_TTL_MINUTES` (`expired`)."),
        **_NOT_FOUND,
    },
)
async def download_file(
    job_id: str,
    exp: Annotated[int, Query(description="Expiry (Unix time), from `download_url`.")],
    sig: Annotated[str, Query(max_length=128, description="HMAC signature, from `download_url`.")],
    settings: SettingsDep,
    manager: JobManagerDep,
) -> FileResponse:
    """Open `download_url` from a `done` job exactly as given. **No API key**: the
    signature authorizes it, so the browser can download straight from here.
    """
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
