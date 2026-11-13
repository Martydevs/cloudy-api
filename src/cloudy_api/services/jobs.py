"""In-memory job store and background runner.

Render Free runs a single instance with a wiped disk on restart, so jobs live
in a dict and files in a temp dir with a TTL. A lost job (restart) is a 404
that the Nuxt app treats as "expired, retry".
"""

import asyncio
import contextlib
import logging
import re
import shutil
import threading
import time
import unicodedata
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..schemas import ErrorInfo, Format, ItemOut, ItemStatus, JobStatus
from .downloader import Cancelled, DownloaderError, VideoDownloader

log = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r"\s+")
_JOB_DIR_NAME = re.compile(r"^[0-9a-f]{32}$")  # uuid4().hex; never touch anything else
_MAX_TITLE_LEN = 150


class QueueFullError(Exception):
    pass


def sanitize_filename(title: str) -> str:
    title = unicodedata.normalize("NFC", title)
    title = _WHITESPACE.sub(" ", title)  # tabs/newlines become spaces first
    title = _UNSAFE_CHARS.sub("", title)
    title = _WHITESPACE.sub(" ", title).strip().strip(".")
    return title[:_MAX_TITLE_LEN].rstrip() or "video"


@dataclass
class JobItem:
    video_id: str
    title: str | None = None
    status: ItemStatus = ItemStatus.QUEUED
    progress: float = 0.0
    error: ErrorInfo | None = None
    actual_height: int | None = None
    path: Path | None = None

    def to_out(self) -> ItemOut:
        return ItemOut(
            video_id=self.video_id,
            title=self.title,
            status=self.status,
            progress=round(self.progress, 4),
            error=self.error,
            actual_height=self.actual_height,
        )


@dataclass
class Job:
    id: str
    format: Format
    items: list[JobItem]
    created_at: float
    status: JobStatus = JobStatus.QUEUED
    finished_at: float | None = None
    file_path: Path | None = None
    filename: str | None = None
    size_bytes: int | None = None
    cancelled: bool = False
    running: bool = False

    @property
    def progress(self) -> float:
        if self.status is JobStatus.DONE:
            return 1.0
        if not self.items:
            return 0.0
        value = sum(i.progress for i in self.items) / len(self.items)
        return round(min(value, 0.99), 4)


@dataclass
class JobStore:
    """Dict of jobs guarded by a *threading* lock: download progress callbacks
    write into it from worker threads."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    jobs: dict[str, Job] = field(default_factory=dict)

    def add(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def remove(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.pop(job_id, None)

    def snapshot(self) -> list[Job]:
        with self.lock:
            return list(self.jobs.values())


class JobManager:
    def __init__(
        self,
        settings: Settings,
        downloader: VideoDownloader,
        *,
        store: JobStore | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self.downloader = downloader
        self.store = store or JobStore()
        self.now = now
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.max_queue)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._tasks: set[asyncio.Task[None]] = set()
        self._background: list[asyncio.Task[None]] = []

    @property
    def work_dir(self) -> Path:
        return self.settings.work_dir

    def job_dir(self, job_id: str) -> Path:
        return self.work_dir / job_id

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        # The store starts empty, so every job dir on disk is left from a crash.
        for child in self.work_dir.iterdir():
            if not _JOB_DIR_NAME.match(child.name):
                continue
            _remove_path(child)
        self._background = [
            asyncio.create_task(self._dispatch(), name="jobs-dispatcher"),
            asyncio.create_task(self._cleanup_loop(), name="jobs-cleanup"),
        ]

    async def stop(self) -> None:
        tasks = [*self._background, *self._tasks]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background.clear()
        self._tasks.clear()

    # ------------------------------------------------------------ public API
    def submit(
        self,
        video_ids: list[str],
        fmt: Format,
        titles: dict[str, str] | None = None,
    ) -> Job:
        titles = titles or {}
        job = Job(
            id=uuid.uuid4().hex,
            format=fmt,
            items=[JobItem(video_id=v, title=titles.get(v)) for v in video_ids],
            created_at=self.now(),
        )
        try:
            self.queue.put_nowait(job.id)
        except asyncio.QueueFull as exc:
            raise QueueFullError() from exc
        self.store.add(job)
        log.info(
            "job.created job_id=%s format=%s items=%d queued=%d",
            job.id, fmt.value, len(job.items), self.queue.qsize(),
        )
        return job

    def get(self, job_id: str) -> Job | None:
        return self.store.get(job_id)

    def delete(self, job_id: str) -> bool:
        job = self.store.remove(job_id)
        if job is None:
            return False
        job.cancelled = True
        # A running job removes its own dir once its download thread stops.
        if not job.running:
            _remove_path(self.job_dir(job_id))
        log.info("job.deleted job_id=%s was_running=%s", job_id, job.running)
        return True

    # --------------------------------------------------------------- runner
    async def _dispatch(self) -> None:
        while True:
            # Wait for a free slot first so waiting jobs stay in the queue and
            # MAX_QUEUE is exact.
            await self._semaphore.acquire()
            job_id = await self.queue.get()
            task = asyncio.create_task(self._run_guarded(job_id), name=f"job-{job_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_guarded(self, job_id: str) -> None:
        try:
            job = self.store.get(job_id)
            if job is not None and not job.cancelled:
                await self.run_job(job)
        except Exception:
            log.exception("job.crashed job_id=%s", job_id)
            job = self.store.get(job_id)
            if job is not None:
                job.status = JobStatus.FAILED
                job.finished_at = self.now()
        finally:
            self._semaphore.release()
            self.queue.task_done()

    async def run_job(self, job: Job) -> None:
        job.running = True
        job.status = JobStatus.RUNNING
        dest = self.job_dir(job.id)
        log.info("job.started job_id=%s", job.id)
        try:
            for item in job.items:
                if job.cancelled:
                    break
                await self._run_item(job, item, dest)

            if job.cancelled:
                return

            successes = [i for i in job.items if i.status is ItemStatus.DONE and i.path]
            if not successes:
                job.status = JobStatus.FAILED
                job.finished_at = self.now()
                _remove_path(dest)
                log.info("job.failed job_id=%s", job.id)
                return

            if len(job.items) == 1:
                item = successes[0]
                assert item.path is not None
                file_path = item.path
                filename = (
                    f"{sanitize_filename(item.title or item.video_id)}"
                    f" [{item.video_id}]{item.path.suffix}"
                )
            else:
                job.status = JobStatus.ZIPPING
                filename = f"cloudy-batch-{job.id[:8]}.zip"
                file_path = dest / filename
                await asyncio.to_thread(_build_zip, file_path, successes)

            if job.cancelled:
                return
            job.file_path = file_path
            job.filename = filename
            job.size_bytes = file_path.stat().st_size
            job.finished_at = self.now()
            job.status = JobStatus.DONE
            log.info(
                "job.done job_id=%s ok=%d failed=%d size_bytes=%d",
                job.id, len(successes), len(job.items) - len(successes), job.size_bytes,
            )
        finally:
            job.running = False
            if job.cancelled:
                _remove_path(dest)
                log.info("job.cancelled job_id=%s", job.id)

    async def _run_item(self, job: Job, item: JobItem, dest: Path) -> None:
        item.status = ItemStatus.RUNNING
        lock = self.store.lock

        def on_progress(value: float) -> None:
            with lock:
                item.progress = max(item.progress, min(value, 1.0))

        log.info("item.started job_id=%s video_id=%s", job.id, item.video_id)
        try:
            result = await asyncio.to_thread(
                self.downloader.download,
                item.video_id,
                job.format,
                dest,
                on_progress,
                lambda: job.cancelled,
            )
        except Cancelled:
            item.status = ItemStatus.FAILED
            item.error = ErrorInfo(code="cancelled", message="Job cancelled")
            return
        except DownloaderError as exc:
            item.status = ItemStatus.FAILED
            item.error = ErrorInfo(code=exc.code, message=exc.message)
            log.warning(
                "item.failed job_id=%s video_id=%s code=%s",
                job.id, item.video_id, exc.code,
            )
            return
        except Exception:
            item.status = ItemStatus.FAILED
            item.error = ErrorInfo(code="internal", message="Unexpected error")
            log.exception("item.crashed job_id=%s video_id=%s", job.id, item.video_id)
            return

        item.path = result.path
        item.title = result.title
        item.actual_height = result.actual_height
        item.progress = 1.0
        item.status = ItemStatus.DONE
        log.info(
            "item.done job_id=%s video_id=%s height=%s",
            job.id, item.video_id, result.actual_height,
        )

    # -------------------------------------------------------------- cleanup
    async def _cleanup_loop(self) -> None:
        while True:
            await self._sleep(self.settings.cleanup_interval_seconds)
            try:
                self.sweep()
            except Exception:
                log.exception("cleanup.failed")

    def sweep(self) -> None:
        """Expire finished jobs after FILE_TTL, drop their records one TTL
        later, and remove dirs that belong to no known job."""
        now = self.now()
        ttl = self.settings.file_ttl_seconds
        for job in self.store.snapshot():
            if job.finished_at is None:
                continue
            age = now - job.finished_at
            if job.status in (JobStatus.DONE, JobStatus.FAILED) and age >= ttl:
                job.status = JobStatus.EXPIRED
                job.file_path = None
                _remove_path(self.job_dir(job.id))
                log.info("job.expired job_id=%s", job.id)
            elif job.status is JobStatus.EXPIRED and age >= 2 * ttl:
                self.store.remove(job.id)

        if self.work_dir.is_dir():
            known = {job.id for job in self.store.snapshot()}
            for child in self.work_dir.iterdir():
                if _JOB_DIR_NAME.match(child.name) and child.name not in known:
                    _remove_path(child)


def _build_zip(target: Path, items: list[JobItem]) -> None:
    used: set[str] = set()
    tmp = target.with_suffix(".zip.part")
    # Media is already compressed; deflating it only burns CPU.
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for item in items:
            assert item.path is not None
            base = f"{sanitize_filename(item.title or item.video_id)} [{item.video_id}]"
            name = f"{base}{item.path.suffix}"
            counter = 2
            while name in used:
                name = f"{base} ({counter}){item.path.suffix}"
                counter += 1
            used.add(name)
            zf.write(item.path, arcname=name)
    tmp.replace(target)
    for item in items:
        assert item.path is not None
        item.path.unlink(missing_ok=True)
        item.path = None


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)
