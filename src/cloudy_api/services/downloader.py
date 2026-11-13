"""Engine-neutral download contract: errors, results, the engine protocol and
shared helpers.

The engine (:class:`~.pytubefix_downloader.PytubefixDownloader`) is blocking;
callers run it through ``asyncio.to_thread``.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..schemas import Format, VideoInfo

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
PROBE_CACHE_TTL_SECONDS = 600

ProgressCallback = Callable[[float], None]


class DownloaderError(Exception):
    """A download engine failure mapped to a stable API error code."""

    def __init__(self, code: str, message: str, *, client_error: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # True: the video itself is the problem (422). False: our side (502).
        self.client_error = client_error


class Cancelled(Exception):
    """Raised when a job is cancelled while its download is running."""


@dataclass(slots=True)
class DownloadResult:
    path: Path
    title: str
    actual_height: int | None


class VideoDownloader(Protocol):
    """What routes and the job runner need from a download engine."""

    def probe(self, video_id: str) -> VideoInfo: ...

    def download(
        self,
        video_id: str,
        fmt: Format,
        dest: Path,
        on_progress: ProgressCallback,
        is_cancelled: Callable[[], bool] = ...,
    ) -> DownloadResult: ...


class ProbeCache:
    """Thread-safe ``video_id -> VideoInfo`` cache with a fixed TTL."""

    def __init__(self, now: Callable[[], float], ttl: float = PROBE_CACHE_TTL_SECONDS) -> None:
        self._now = now
        self._ttl = ttl
        self._entries: dict[str, tuple[float, VideoInfo]] = {}
        self._lock = threading.Lock()

    def get(self, video_id: str) -> VideoInfo | None:
        with self._lock:
            now = self._now()
            for key, (stamp, _) in list(self._entries.items()):
                if now - stamp > self._ttl:
                    del self._entries[key]
            entry = self._entries.get(video_id)
        return entry[1] if entry else None

    def put(self, info: VideoInfo) -> None:
        with self._lock:
            self._entries[info.video_id] = (self._now(), info)


def not_downloadable_reason(
    *, live: bool, duration: int | None, max_duration: int
) -> str | None:
    if live:
        return "live"
    if duration is None:
        return "unknown_duration"
    if duration > max_duration:
        return "too_long"
    return None


def _clean_message(message: str) -> str:
    # Drop "ERROR: " and "[extractor] <id>: " prefixes; keep the reason.
    message = message.removeprefix("ERROR: ").strip()
    if message.startswith("[") and ": " in message:
        message = message.split(": ", 1)[1]
    return message[:300]


def watch_url(video_id: str) -> str:
    return YOUTUBE_WATCH_URL.format(video_id=video_id)
