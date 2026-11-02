import asyncio
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeDownloader

from cloudy_api.config import Settings
from cloudy_api.schemas import Format, ItemStatus, JobStatus
from cloudy_api.services.downloader import DownloaderError
from cloudy_api.services.jobs import (
    Job,
    JobItem,
    JobManager,
    QueueFullError,
    _build_zip,
    sanitize_filename,
)

pytestmark = pytest.mark.anyio

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


class Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def make_manager(
    make_settings: Callable[..., Settings], fake_downloader: FakeDownloader, clock: Clock
) -> Callable[..., JobManager]:
    def factory(**settings: Any) -> JobManager:
        return JobManager(make_settings(**settings), fake_downloader, now=clock)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def manager(make_manager: Callable[..., JobManager]) -> JobManager:
    return make_manager()


async def drain(manager: JobManager) -> None:
    """Wait until every queued job has been processed."""
    await asyncio.wait_for(manager.queue.join(), timeout=5)


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("My Title", "My Title"),
        ('a/b\\c:d*e?f"g<h>i|j', "abcdefghij"),
        ("  lots   of\tspace\n ", "lots of space"),
        ("...dots...", "dots"),
        ("\x00\x1f", "video"),
        ("", "video"),
        ("x" * 300, "x" * 150),
        ("Café ñandú 🎵", "Café ñandú 🎵"),
    ],
)
def test_sanitize_filename(title: str, expected: str) -> None:
    assert sanitize_filename(title) == expected


def test_job_progress() -> None:
    job = Job(id="j", format=Format.MP3, items=[JobItem(A, progress=1.0), JobItem(B)], created_at=0)
    assert job.progress == 0.5
    job.items[1].progress = 1.0
    assert job.progress == 0.99  # capped until DONE
    job.status = JobStatus.DONE
    assert job.progress == 1.0
    assert Job(id="e", format=Format.MP3, items=[], created_at=0).progress == 0.0


def test_build_zip_dedupes_names(tmp_path: Path) -> None:
    items = []
    for i, vid in enumerate([A, A, A]):
        path = tmp_path / f"{i}.mp3"
        path.write_bytes(f"file-{i}".encode())
        items.append(JobItem(vid, title="Same", status=ItemStatus.DONE, path=path))
    target = tmp_path / "out.zip"

    _build_zip(target, items)

    with zipfile.ZipFile(target) as zf:
        assert zf.namelist() == [
            f"Same [{A}].mp3",
            f"Same [{A}] (2).mp3",
            f"Same [{A}] (3).mp3",
        ]
        assert all(info.compress_type == zipfile.ZIP_STORED for info in zf.infolist())
    assert not list(tmp_path.glob("*.mp3"))  # sources removed after zipping
    assert all(item.path is None for item in items)


# ------------------------------------------------------------------- submit
async def test_submit_queues_job(manager: JobManager, clock: Clock) -> None:
    job = manager.submit([A, B], Format.MP3, {A: "Title A"})
    assert job.status is JobStatus.QUEUED
    assert len(job.id) == 32
    assert job.created_at == clock.value
    assert [(i.video_id, i.title, i.status) for i in job.items] == [
        (A, "Title A", ItemStatus.QUEUED),
        (B, None, ItemStatus.QUEUED),
    ]
    assert manager.get(job.id) is job
    assert manager.queue.qsize() == 1


async def test_submit_raises_when_queue_full(make_manager: Callable[..., JobManager]) -> None:
    manager = make_manager(max_queue=2)
    manager.submit([A], Format.MP3)
    manager.submit([B], Format.MP3)
    with pytest.raises(QueueFullError):
        manager.submit([C], Format.MP3)
    assert len(manager.store.snapshot()) == 2  # rejected job isn't stored


# ------------------------------------------------------------------ run_job
async def test_single_item_job_returns_the_file(
    manager: JobManager, fake_downloader: FakeDownloader, clock: Clock
) -> None:
    job = manager.submit([A], Format.MP4_1080, {A: "Probe title"})
    await manager.run_job(job)

    assert job.status is JobStatus.DONE
    assert job.progress == 1.0
    item = job.items[0]
    assert item.status is ItemStatus.DONE
    assert item.title == f"Downloaded {A}"
    assert item.actual_height == 1080
    assert item.progress == 1.0
    assert job.filename == f"Downloaded {A} [{A}].mp4"
    assert job.file_path == manager.job_dir(job.id) / f"{A}.mp4"
    assert job.size_bytes == job.file_path.stat().st_size
    assert job.finished_at == clock.value
    assert fake_downloader.downloaded == [(A, Format.MP4_1080)]


async def test_batch_job_returns_zip(manager: JobManager) -> None:
    job = manager.submit([A, B], Format.MP3)
    await manager.run_job(job)

    assert job.status is JobStatus.DONE
    assert job.filename == f"cloudy-batch-{job.id[:8]}.zip"
    assert job.file_path is not None
    with zipfile.ZipFile(job.file_path) as zf:
        assert sorted(zf.namelist()) == [
            f"Downloaded {A} [{A}].mp3",
            f"Downloaded {B} [{B}].mp3",
        ]
        assert zf.read(f"Downloaded {A} [{A}].mp3") == f"mp3:{A}".encode()
    assert sorted(p.name for p in manager.job_dir(job.id).iterdir()) == [job.filename]


async def test_partial_batch_failure(manager: JobManager, fake_downloader: FakeDownloader) -> None:
    fake_downloader.download_errors[B] = DownloaderError("private", "Private video")
    job = manager.submit([A, B, C], Format.MP3)
    await manager.run_job(job)

    assert job.status is JobStatus.DONE
    statuses = {i.video_id: (i.status, i.error) for i in job.items}
    assert statuses[A][0] is ItemStatus.DONE
    assert statuses[C][0] is ItemStatus.DONE
    assert statuses[B][0] is ItemStatus.FAILED
    assert statuses[B][1] is not None and statuses[B][1].code == "private"
    assert job.file_path is not None
    with zipfile.ZipFile(job.file_path) as zf:
        assert len(zf.namelist()) == 2


async def test_all_items_failing_fails_the_job(
    manager: JobManager, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.download_errors[A] = DownloaderError("unavailable", "Video unavailable")
    fake_downloader.download_errors[B] = RuntimeError("boom")
    job = manager.submit([A, B], Format.MP3)
    await manager.run_job(job)

    assert job.status is JobStatus.FAILED
    assert job.file_path is None
    assert job.finished_at is not None
    assert [i.error.code for i in job.items if i.error] == ["unavailable", "internal"]
    assert not manager.job_dir(job.id).exists()


async def test_status_transitions(manager: JobManager, fake_downloader: FakeDownloader) -> None:
    gate = threading.Event()
    fake_downloader.gate = gate
    job = manager.submit([A, B], Format.MP3)
    seen: list[JobStatus] = [job.status]

    task = asyncio.create_task(manager.run_job(job))
    while not fake_downloader.started.is_set():
        await asyncio.sleep(0)
    seen.append(job.status)
    assert job.items[0].status is ItemStatus.RUNNING
    gate.set()
    await task
    seen.append(job.status)

    assert seen == [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE]


async def test_zipping_status_is_set_while_zipping(
    manager: JobManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cloudy_api.services import jobs as jobs_module

    job = manager.submit([A, B], Format.MP3)
    observed: list[JobStatus] = []
    real_build = jobs_module._build_zip

    def spy(target: Path, items: list[JobItem]) -> None:
        observed.append(job.status)
        real_build(target, items)

    monkeypatch.setattr(jobs_module, "_build_zip", spy)
    await manager.run_job(job)
    assert observed == [JobStatus.ZIPPING]


async def test_progress_hook_updates_item(
    manager: JobManager, fake_downloader: FakeDownloader
) -> None:
    gate = threading.Event()
    fake_downloader.gate = gate
    job = manager.submit([A], Format.MP3)
    task = asyncio.create_task(manager.run_job(job))
    while not fake_downloader.started.is_set():
        await asyncio.sleep(0)
    gate.set()
    await task
    # FakeDownloader reports 0.5 then the runner marks the item done.
    assert job.items[0].progress == 1.0


# ------------------------------------------------------------ cancellation
async def test_delete_queued_job(manager: JobManager, fake_downloader: FakeDownloader) -> None:
    job = manager.submit([A], Format.MP3)
    assert manager.delete(job.id) is True
    assert manager.get(job.id) is None
    assert manager.delete(job.id) is False

    await manager.start()
    try:
        await drain(manager)
    finally:
        await manager.stop()
    assert fake_downloader.downloaded == []  # skipped by the runner


async def test_delete_running_job_cancels_it(
    manager: JobManager, fake_downloader: FakeDownloader
) -> None:
    gate = threading.Event()
    fake_downloader.gate = gate
    job = manager.submit([A, B], Format.MP3)
    task = asyncio.create_task(manager.run_job(job))
    while not fake_downloader.started.is_set():
        await asyncio.sleep(0)

    assert manager.delete(job.id) is True
    assert manager.get(job.id) is None
    gate.set()
    await task

    assert job.cancelled is True
    assert job.items[0].error is not None and job.items[0].error.code == "cancelled"
    assert job.items[1].status is ItemStatus.QUEUED  # never started
    assert fake_downloader.downloaded == [(A, Format.MP3)]
    assert not manager.job_dir(job.id).exists()


async def test_cancel_during_zip_discards_result(
    manager: JobManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cloudy_api.services import jobs as jobs_module

    job = manager.submit([A, B], Format.MP3)
    real_build = jobs_module._build_zip

    def cancel_then_zip(target: Path, items: list[JobItem]) -> None:
        job.cancelled = True
        real_build(target, items)

    monkeypatch.setattr(jobs_module, "_build_zip", cancel_then_zip)
    await manager.run_job(job)
    assert job.status is JobStatus.ZIPPING
    assert job.file_path is None
    assert not manager.job_dir(job.id).exists()


# ------------------------------------------------------------ runner loop
async def test_runner_processes_queue(manager: JobManager) -> None:
    await manager.start()
    try:
        jobs = [manager.submit([vid], Format.MP3) for vid in (A, B, C)]
        await drain(manager)
    finally:
        await manager.stop()
    assert all(j.status is JobStatus.DONE for j in jobs)


async def test_semaphore_limits_concurrency(
    make_manager: Callable[..., JobManager], fake_downloader: FakeDownloader
) -> None:
    manager = make_manager(max_concurrent_jobs=1)
    active = 0
    peak = 0
    real_run = manager.run_job

    async def tracked(job: Job) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        await real_run(job)
        active -= 1

    manager.run_job = tracked  # type: ignore[method-assign]
    await manager.start()
    try:
        for vid in (A, B, C):
            manager.submit([vid], Format.MP3)
        await drain(manager)
    finally:
        await manager.stop()
    assert peak == 1


async def test_semaphore_allows_parallel_jobs(make_manager: Callable[..., JobManager]) -> None:
    manager = make_manager(max_concurrent_jobs=2)
    active = 0
    peak = 0
    both_running = asyncio.Event()

    async def tracked(job: Job) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), timeout=5)
        active -= 1

    manager.run_job = tracked  # type: ignore[method-assign]
    await manager.start()
    try:
        manager.submit([A], Format.MP3)
        manager.submit([B], Format.MP3)
        await drain(manager)
    finally:
        await manager.stop()
    assert peak == 2


async def test_crashing_job_is_marked_failed(manager: JobManager) -> None:
    async def boom(job: Job) -> None:
        raise RuntimeError("unexpected")

    manager.run_job = boom  # type: ignore[method-assign]
    await manager.start()
    try:
        job = manager.submit([A], Format.MP3)
        await drain(manager)
    finally:
        await manager.stop()
    assert job.status is JobStatus.FAILED
    assert job.finished_at is not None


async def test_crash_after_delete_is_ignored(manager: JobManager) -> None:
    async def delete_then_crash(job: Job) -> None:
        manager.delete(job.id)
        raise RuntimeError("unexpected")

    manager.run_job = delete_then_crash  # type: ignore[method-assign]
    await manager.start()
    try:
        job = manager.submit([A], Format.MP3)
        await drain(manager)
    finally:
        await manager.stop()
    assert manager.get(job.id) is None


async def test_start_removes_stale_job_dirs_only(manager: JobManager) -> None:
    work = manager.work_dir
    work.mkdir(parents=True)
    stale_dir = work / ("a" * 32)
    stale_dir.mkdir()
    (stale_dir / "x.mp4").write_bytes(b"x")
    stale_file = work / ("b" * 32)
    stale_file.write_bytes(b"x")
    keep = work / "not-a-job"
    keep.mkdir()

    await manager.start()
    await manager.stop()

    assert not stale_dir.exists()
    assert not stale_file.exists()
    assert keep.exists()


# ------------------------------------------------------------------ cleanup
async def test_sweep_expires_files_then_records(manager: JobManager, clock: Clock) -> None:
    done = manager.submit([A], Format.MP3)
    await manager.run_job(done)
    assert done.file_path is not None and done.file_path.exists()
    pending = manager.submit([B], Format.MP3)  # never finished: untouched
    ttl = manager.settings.file_ttl_seconds

    clock.value += ttl - 1
    manager.sweep()
    assert done.status is JobStatus.DONE

    clock.value += 1
    manager.sweep()
    assert done.status is JobStatus.EXPIRED
    assert done.file_path is None
    assert not manager.job_dir(done.id).exists()
    assert manager.get(done.id) is done  # record kept so polls answer "expired"

    clock.value += ttl
    manager.sweep()
    assert manager.get(done.id) is None
    assert manager.get(pending.id) is pending


async def test_sweep_expires_failed_jobs(manager: JobManager, clock: Clock) -> None:
    job = manager.submit([A], Format.MP3)
    job.status = JobStatus.FAILED
    job.finished_at = clock.value
    clock.value += manager.settings.file_ttl_seconds
    manager.sweep()
    assert job.status is JobStatus.EXPIRED


async def test_sweep_removes_orphan_dirs(manager: JobManager) -> None:
    manager.work_dir.mkdir(parents=True)
    job = manager.submit([A], Format.MP3)
    manager.job_dir(job.id).mkdir()
    orphan = manager.work_dir / ("c" * 32)
    orphan.mkdir()
    other = manager.work_dir / "keep-me"
    other.mkdir()

    manager.sweep()

    assert manager.job_dir(job.id).exists()
    assert not orphan.exists()
    assert other.exists()


async def test_sweep_without_work_dir(manager: JobManager) -> None:
    assert not manager.work_dir.exists()
    manager.sweep()  # no error


async def test_cleanup_loop_sweeps_and_survives_errors(
    make_settings: Callable[..., Settings], fake_downloader: FakeDownloader
) -> None:
    ticks = 0
    swept = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 2:
            swept.set()
            await asyncio.Event().wait()  # park until cancelled

    manager = JobManager(make_settings(), fake_downloader, sleep=fake_sleep)  # type: ignore[arg-type]
    calls = 0

    def flaky_sweep() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("disk on fire")

    manager.sweep = flaky_sweep  # type: ignore[method-assign]
    await manager.start()
    try:
        await asyncio.wait_for(swept.wait(), timeout=5)
    finally:
        await manager.stop()
    assert calls == 2  # first failure was logged, loop kept going
