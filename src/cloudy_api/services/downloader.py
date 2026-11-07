"""Thin wrapper around yt-dlp's Python API.

Everything here is blocking; callers run it through ``asyncio.to_thread``.
yt-dlp drives ffmpeg/ffprobe (merge + MP3 extraction) and a JS runtime
(deno/node/bun/quickjs, for YouTube JS challenges) on its own; this module
never calls them directly.
"""

import contextlib
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadCancelled, YoutubeDLError

from ..config import Settings
from ..schemas import Format, VideoInfo

log = logging.getLogger(__name__)

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
PROBE_CACHE_TTL_SECONDS = 600
_TEMP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")

ProgressCallback = Callable[[float], None]


class DownloaderError(Exception):
    """A yt-dlp failure mapped to a stable API error code."""

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


# (substring in lowercase yt-dlp message, code, client_error). Order matters:
# "sign in to confirm your age" must match age_restricted before bot_check.
_ERROR_PATTERNS: tuple[tuple[str, str, bool], ...] = (
    ("private video", "private", True),
    ("confirm your age", "age_restricted", True),
    ("age-restricted", "age_restricted", True),
    ("inappropriate for some users", "age_restricted", True),
    ("not a bot", "bot_check", False),
    ("ffmpeg not found", "ffmpeg_missing", False),
    ("members-only", "members_only", True),
    ("join this channel", "members_only", True),
    ("available in your country", "geo_restricted", True),
    ("live event will begin", "live", True),
    ("premieres in", "live", True),
    ("requested format is not available", "format_unavailable", True),
    ("video unavailable", "unavailable", True),
    ("has been removed", "unavailable", True),
    ("is not available", "unavailable", True),
    ("does not exist", "unavailable", True),
)


def map_ytdlp_error(exc: BaseException) -> DownloaderError:
    message = str(exc)
    lowered = message.lower()
    for needle, code, client_error in _ERROR_PATTERNS:
        if needle in lowered:
            return DownloaderError(code, _clean_message(message), client_error=client_error)
    return DownloaderError("download_failed", _clean_message(message), client_error=False)


def _clean_message(message: str) -> str:
    # yt-dlp prefixes errors with "ERROR: [youtube] <id>: "; keep the reason.
    message = message.removeprefix("ERROR: ").strip()
    if message.startswith("[") and ": " in message:
        message = message.split(": ", 1)[1]
    return message[:300]


class _YtDlpLogger:
    """Routes yt-dlp output into std logging instead of stdout."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp %s", msg)  # re-raised as DownloadError and logged by caller


def watch_url(video_id: str) -> str:
    return YOUTUBE_WATCH_URL.format(video_id=video_id)


def _mp4_selector(height: int) -> str:
    return (
        f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]"
        f"/bv*[height<={height}]+ba"
        f"/b[height<={height}]"
    )


class Downloader:
    def __init__(
        self,
        settings: Settings,
        *,
        ydl_factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._ydl_factory = ydl_factory
        self._now = now
        self._probe_cache: dict[str, tuple[float, VideoInfo]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ opts
    def build_opts(self, fmt: Format | None = None) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "noplaylist": True,
            "logger": _YtDlpLogger(),
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "overwrites": True,
            "js_runtimes": {
                name: {"path": path} if path else {}
                for name, path in self._settings.ytdlp_js_runtimes.items()
            },
        }
        if self._settings.ytdlp_cookies_file is not None:
            opts["cookiefile"] = str(self._settings.ytdlp_cookies_file)
        if self._settings.ytdlp_proxy is not None:
            opts["proxy"] = self._settings.ytdlp_proxy.get_secret_value()
        if self._settings.ytdlp_extractor_args:
            opts["extractor_args"] = self._settings.ytdlp_extractor_args

        if fmt is None:
            return opts
        if fmt is Format.MP3:
            opts["format"] = "ba/b"
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            height = fmt.max_height
            assert height is not None
            opts["format"] = _mp4_selector(height)
            opts["merge_output_format"] = "mp4"
            # A single-file fallback (e.g. webm) gets remuxed into MP4.
            opts["postprocessors"] = [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
            ]
        return opts

    @contextlib.contextmanager
    def _open(self, opts: dict[str, Any]) -> Iterator[Any]:
        """Create a YoutubeDL instance.

        yt-dlp writes the cookie jar back to ``cookiefile`` on close. Render
        secret files are read-only and probes run concurrently, so each
        instance gets its own writable copy, deleted afterwards.
        """
        cookie_copy: str | None = None
        source = opts.get("cookiefile")
        if source:
            fd, cookie_copy = tempfile.mkstemp(prefix="cloudy-cookies-", suffix=".txt")
            os.close(fd)
            shutil.copyfile(source, cookie_copy)
            opts = {**opts, "cookiefile": cookie_copy}
        try:
            with self._ydl_factory(opts) as ydl:
                yield ydl
        finally:
            if cookie_copy:
                with contextlib.suppress(OSError):
                    os.unlink(cookie_copy)

    # ----------------------------------------------------------------- probe
    def probe(self, video_id: str) -> VideoInfo:
        cached = self._cached_probe(video_id)
        if cached is not None:
            return cached
        try:
            with self._open(self.build_opts()) as ydl:
                info = ydl.extract_info(watch_url(video_id), download=False)
        except YoutubeDLError as exc:  # DownloadError and friends
            raise map_ytdlp_error(exc) from exc
        result = self._to_video_info(video_id, info)
        with self._cache_lock:
            self._probe_cache[video_id] = (self._now(), result)
        return result

    def _cached_probe(self, video_id: str) -> VideoInfo | None:
        with self._cache_lock:
            now = self._now()
            for key, (stamp, _) in list(self._probe_cache.items()):
                if now - stamp > PROBE_CACHE_TTL_SECONDS:
                    del self._probe_cache[key]
            entry = self._probe_cache.get(video_id)
        return entry[1] if entry else None

    def _to_video_info(self, video_id: str, info: dict[str, Any]) -> VideoInfo:
        duration = info.get("duration")
        duration = int(duration) if isinstance(duration, int | float) else None
        live_status = info.get("live_status")

        reason: str | None = None
        if live_status in ("is_live", "is_upcoming", "post_live") or info.get("is_live"):
            reason = "live"
        elif duration is None:
            reason = "unknown_duration"
        elif duration > self._settings.max_duration_seconds:
            reason = "too_long"

        return VideoInfo(
            video_id=video_id,
            title=info.get("title") or video_id,
            duration=duration,
            thumbnail=info.get("thumbnail"),
            channel=info.get("channel") or info.get("uploader"),
            available_heights=available_heights(info.get("formats") or []),
            downloadable=reason is None,
            reason=reason,
        )

    # -------------------------------------------------------------- download
    def download(
        self,
        video_id: str,
        fmt: Format,
        dest: Path,
        on_progress: ProgressCallback,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> DownloadResult:
        dest.mkdir(parents=True, exist_ok=True)
        opts = self.build_opts(fmt)
        opts["outtmpl"] = {"default": str(dest / "%(id)s.%(ext)s")}
        opts["progress_hooks"] = [_ProgressTracker(fmt, on_progress, is_cancelled)]

        try:
            with self._open(opts) as ydl:
                info = ydl.extract_info(watch_url(video_id), download=True)
        except DownloadCancelled as exc:
            raise Cancelled() from exc
        except YoutubeDLError as exc:
            raise map_ytdlp_error(exc) from exc

        path = _final_path(info, dest, video_id, fmt)
        on_progress(1.0)
        height = info.get("height") if fmt is not Format.MP3 else None
        return DownloadResult(
            path=path,
            title=info.get("title") or video_id,
            actual_height=height if isinstance(height, int) else None,
        )


def available_heights(formats: list[dict[str, Any]]) -> list[int]:
    heights = {
        f["height"]
        for f in formats
        if f.get("vcodec") not in (None, "none") and isinstance(f.get("height"), int)
    }
    return sorted(heights)


def _final_path(info: dict[str, Any], dest: Path, video_id: str, fmt: Format) -> Path:
    for entry in info.get("requested_downloads") or []:
        candidate = entry.get("filepath")
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    expected = dest / f"{video_id}.{fmt.ext}"
    if expected.is_file():
        return expected
    for candidate in sorted(dest.glob(f"{video_id}.*")):
        if candidate.is_file() and not candidate.name.endswith(_TEMP_SUFFIXES):
            return candidate
    raise DownloaderError(
        "download_failed", "Output file not found after download", client_error=False
    )


class _ProgressTracker:
    """Turns yt-dlp's per-stream hooks into one monotonic 0..1 value.

    MP4 downloads usually fetch video then audio, so each stream gets half of
    the bar; anything above 0.95 is reserved for post-processing.
    """

    def __init__(
        self,
        fmt: Format,
        on_progress: ProgressCallback,
        is_cancelled: Callable[[], bool],
    ) -> None:
        self._phases = 1 if fmt is Format.MP3 else 2
        self._phase = 0
        self._best = 0.0
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled

    def __call__(self, d: dict[str, Any]) -> None:
        if self._is_cancelled():
            raise DownloadCancelled("Job cancelled")
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            fraction = min(done / total, 1.0) if total else 0.0
        elif status == "finished":
            fraction = 1.0
        else:
            return
        phase = min(self._phase, self._phases - 1)
        value = min((phase + fraction) / self._phases, 1.0) * 0.95
        if value > self._best:
            self._best = value
            self._on_progress(value)
        if status == "finished":
            self._phase += 1
