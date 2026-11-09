"""pytubefix-based downloader behind the /v2 API.

A temporary workaround for YouTube's "confirm you're not a bot" check on
datacenter IPs: pytubefix's WEB client sends a PO token that it generates by
running BotGuard in Node.js (bundled through ``nodejs-wheel-binaries``).

Same contract as :class:`~.downloader.Downloader`: blocking (callers use
``asyncio.to_thread``), returns ``VideoInfo``/``DownloadResult`` and raises
``DownloaderError``/``Cancelled``. Unlike yt-dlp, pytubefix doesn't merge
streams, so adaptive video+audio and MP3 conversion run ffmpeg directly.
"""

import logging
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pytubefix import YouTube
from pytubefix import exceptions as pexc

from ..config import Settings
from ..schemas import Format, VideoInfo
from .downloader import (
    Cancelled,
    DownloaderError,
    DownloadResult,
    ProbeCache,
    ProgressCallback,
    _clean_message,
    not_downloadable_reason,
    watch_url,
)

log = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_RETRIES = 3
MP3_BITRATE = "192k"
AAC_BITRATE = "192k"
_LIVE_ERRORS = (pexc.LiveStreamError, pexc.LiveStreamOffline, pexc.LiveStreamEnded)

FfmpegRunner = Callable[[Sequence[str]], None]

# isinstance checks in order: subclasses before their bases (everything below
# derives from VideoUnavailable).
_ERROR_TYPES: tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str, bool], ...] = (
    (pexc.VideoPrivate, "private", True),
    (
        (pexc.AgeRestrictedError, pexc.AgeCheckRequiredError, pexc.AgeCheckRequiredAccountError),
        "age_restricted",
        True,
    ),
    ((pexc.BotDetection, pexc.PoTokenRequired), "bot_check", False),
    (pexc.MembersOnly, "members_only", True),
    (pexc.VideoRegionBlocked, "geo_restricted", True),
    (_LIVE_ERRORS, "live", True),
    # Raised when no client returns streams or the API call fails: our side.
    ((pexc.UnknownVideoError, pexc.InnerTubeResponseError), "download_failed", False),
    (pexc.VideoUnavailable, "unavailable", True),
)


def map_pytubefix_error(exc: BaseException) -> DownloaderError:
    message = _clean_message(str(exc) or type(exc).__name__)
    if isinstance(exc, pexc.LoginRequired):
        # "Sign in to confirm you're not a bot" variants that pytubefix didn't
        # recognise as BotDetection still land here.
        if "bot" in message.lower():
            return DownloaderError("bot_check", message, client_error=False)
        return DownloaderError("unavailable", message)
    for types, code, client_error in _ERROR_TYPES:
        if isinstance(exc, types):
            return DownloaderError(code, message, client_error=client_error)
    return DownloaderError("download_failed", message, client_error=False)


def run_ffmpeg(args: Sequence[str]) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip().splitlines()
        reason = stderr[-1] if stderr else f"exit code {exc.returncode}"
        raise DownloaderError(
            "download_failed", f"ffmpeg failed: {reason}"[:300], client_error=False
        ) from exc


class PytubefixDownloader:
    def __init__(
        self,
        settings: Settings,
        *,
        yt_factory: Callable[..., Any] = YouTube,
        ffmpeg_runner: FfmpegRunner = run_ffmpeg,
        which: Callable[[str], str | None] = shutil.which,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._yt_factory = yt_factory
        self._run_ffmpeg = ffmpeg_runner
        self._which = which
        self._probe_cache = ProbeCache(now)

    def _youtube(self, video_id: str, on_progress: Callable[..., None] | None = None) -> Any:
        kwargs: dict[str, Any] = {"client": self._settings.pytubefix_client}
        if on_progress is not None:
            kwargs["on_progress_callback"] = on_progress
        if self._settings.pytubefix_proxy is not None:
            proxy = self._settings.pytubefix_proxy.get_secret_value()
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        # Never pass use_po_token=True: in pytubefix 11 it prompts on stdin.
        # WEB-family clients generate the PO token on their own.
        return self._yt_factory(watch_url(video_id), **kwargs)

    # ----------------------------------------------------------------- probe
    def probe(self, video_id: str) -> VideoInfo:
        cached = self._probe_cache.get(video_id)
        if cached is not None:
            return cached
        try:
            result = self._probe(video_id)
        except DownloaderError:
            raise
        except Exception as exc:
            raise self._mapped(exc, video_id) from exc
        self._probe_cache.put(result)
        return result

    def _probe(self, video_id: str) -> VideoInfo:
        yt = self._youtube(video_id)
        details: dict[str, Any] = yt.vid_info.get("videoDetails") or {}
        live = bool(details.get("isLive") or details.get("isUpcoming"))
        heights: list[int] = []
        if not live:
            try:
                yt.check_availability()
                heights = sorted(
                    {
                        s.height
                        for s in yt.streams
                        if s.includes_video_track and isinstance(s.height, int)
                    }
                )
            except _LIVE_ERRORS:
                live = True

        length = details.get("lengthSeconds")
        try:
            duration: int | None = int(length) if length not in (None, "") else None
        except (TypeError, ValueError):
            duration = None
        if duration == 0 and live:
            duration = None
        reason = not_downloadable_reason(
            live=live, duration=duration, max_duration=self._settings.max_duration_seconds
        )
        return VideoInfo(
            video_id=video_id,
            title=details.get("title") or video_id,
            duration=duration,
            thumbnail=yt.thumbnail_url,
            channel=details.get("author"),
            available_heights=heights,
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
        if self._which("ffmpeg") is None:
            raise DownloaderError("ffmpeg_missing", "ffmpeg not found", client_error=False)
        dest.mkdir(parents=True, exist_ok=True)
        output = dest / f"{video_id}.{fmt.ext}"
        tracker = _PhaseProgress(on_progress, is_cancelled)
        parts: list[Path] = []
        try:
            yt = self._youtube(video_id, tracker.on_chunk)
            if is_cancelled():
                raise Cancelled()
            streams = list(yt.streams)
            if fmt is Format.MP3:
                height = None
                self._download_mp3(streams, video_id, dest, output, tracker, parts)
            else:
                height = self._download_mp4(fmt, streams, video_id, dest, output, tracker, parts)
            title = (yt.vid_info.get("videoDetails") or {}).get("title") or video_id
        except (Cancelled, DownloaderError):
            output.unlink(missing_ok=True)
            raise
        except Exception as exc:
            output.unlink(missing_ok=True)
            raise self._mapped(exc, video_id) from exc
        finally:
            for part in parts:
                part.unlink(missing_ok=True)

        on_progress(1.0)
        return DownloadResult(path=output, title=title, actual_height=height)

    def _download_mp4(
        self,
        fmt: Format,
        streams: list[Any],
        video_id: str,
        dest: Path,
        output: Path,
        tracker: "_PhaseProgress",
        parts: list[Path],
    ) -> int | None:
        max_height = fmt.max_height
        assert max_height is not None
        video = pick_video(streams, max_height)
        audio = pick_audio(streams)
        if video is not None and audio is not None:
            tracker.phases = 2
            video_path = self._fetch(video, dest, f"{video_id}.video", tracker, parts)
            tracker.next_phase()
            audio_path = self._fetch(audio, dest, f"{video_id}.audio", tracker, parts)
            audio_codec = "copy" if _is_aac(audio) else "aac"
            self._run_ffmpeg(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(video_path), "-i", str(audio_path),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", audio_codec,
                    *(["-b:a", AAC_BITRATE] if audio_codec == "aac" else []),
                    str(output),
                ]
            )
            return video.height

        single = pick_progressive(streams, max_height)
        if single is None:
            raise DownloaderError(
                "format_unavailable", f"No stream up to {max_height}p is available"
            )
        tracker.phases = 1
        path = self._fetch(single, dest, f"{video_id}.single", tracker, parts)
        if single.subtype == "mp4":
            path.replace(output)
        else:
            self._run_ffmpeg(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
                 "-c:v", "copy", "-c:a", "aac", str(output)]
            )
        return single.height

    def _download_mp3(
        self,
        streams: list[Any],
        video_id: str,
        dest: Path,
        output: Path,
        tracker: "_PhaseProgress",
        parts: list[Path],
    ) -> None:
        source = pick_audio(streams, prefer_m4a=False) or pick_progressive(streams, None)
        if source is None:
            raise DownloaderError("format_unavailable", "No audio stream is available")
        tracker.phases = 1
        path = self._fetch(source, dest, f"{video_id}.source", tracker, parts)
        self._run_ffmpeg(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-vn", "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, str(output)]
        )

    def _fetch(
        self, stream: Any, dest: Path, stem: str, tracker: "_PhaseProgress", parts: list[Path]
    ) -> Path:
        filename = f"{stem}.{stream.subtype}"
        path = dest / filename
        parts.append(path)
        stream.download(
            output_path=str(dest),
            filename=filename,
            skip_existing=False,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            max_retries=DOWNLOAD_RETRIES,
        )
        if not path.is_file():
            raise DownloaderError(
                "download_failed", "Stream download produced no file", client_error=False
            )
        tracker.finish_phase()
        return path

    @staticmethod
    def _mapped(exc: BaseException, video_id: str) -> DownloaderError:
        error = map_pytubefix_error(exc)
        if not isinstance(exc, pexc.PytubeFixError):
            # Network errors and pytubefix breaking on a YouTube change.
            log.warning(
                "pytubefix.unexpected_error video_id=%s type=%s",
                video_id, type(exc).__name__, exc_info=exc,
            )
        return error


# ---------------------------------------------------------- stream selection
def _is_avc(stream: Any) -> bool:
    return (stream.video_codec or "").startswith("avc1")


def _is_aac(stream: Any) -> bool:
    return (stream.audio_codec or "").startswith("mp4a")


def _bitrate(stream: Any) -> int:
    return stream.bitrate or 0


def pick_video(streams: list[Any], max_height: int) -> Any | None:
    """Best video-only stream up to ``max_height``, preferring H.264 (avc1)
    like v1's ``bv*[height<=N][vcodec^=avc1]`` selector."""
    candidates = [
        s for s in streams
        if s.includes_video_track
        and not s.includes_audio_track
        and isinstance(s.height, int)
        and s.height <= max_height
    ]
    preferred = [s for s in candidates if _is_avc(s)] or candidates
    if not preferred:
        return None
    return max(preferred, key=lambda s: (s.height, getattr(s, "fps", 0) or 0, _bitrate(s)))


def pick_audio(streams: list[Any], *, prefer_m4a: bool = True) -> Any | None:
    """Best audio-only stream, original audio track first; m4a (AAC) first
    for MP4 so the merge is a plain copy."""
    candidates = [s for s in streams if s.includes_audio_track and not s.includes_video_track]
    candidates = [s for s in candidates if s.is_default_audio_track] or candidates
    if prefer_m4a:
        candidates = [s for s in candidates if _is_aac(s)] or candidates
    return max(candidates, key=_bitrate, default=None)


def pick_progressive(streams: list[Any], max_height: int | None) -> Any | None:
    """Best stream that carries both video and audio (no merge needed)."""
    candidates = [
        s for s in streams
        if s.includes_video_track
        and s.includes_audio_track
        and (max_height is None or (isinstance(s.height, int) and s.height <= max_height))
    ]
    return max(candidates, key=lambda s: (s.height or 0, _bitrate(s)), default=None)


class _PhaseProgress:
    """Turns pytubefix chunk callbacks into one monotonic 0..1 value.

    Each downloaded stream is one phase (video then audio for MP4); above 0.95
    is reserved for ffmpeg. Raising ``Cancelled`` from the chunk callback
    aborts pytubefix's download loop.
    """

    def __init__(self, on_progress: ProgressCallback, is_cancelled: Callable[[], bool]) -> None:
        self.phases = 1
        self._phase = 0
        self._best = 0.0
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled

    def on_chunk(self, stream: Any, _chunk: bytes, bytes_remaining: int) -> None:
        if self._is_cancelled():
            raise Cancelled()
        total = stream.filesize or 0
        fraction = min(max(1 - bytes_remaining / total, 0.0), 1.0) if total else 0.0
        self._report(fraction)

    def finish_phase(self) -> None:
        if self._is_cancelled():
            raise Cancelled()
        self._report(1.0)

    def next_phase(self) -> None:
        self._phase += 1

    def _report(self, fraction: float) -> None:
        phase = min(self._phase, self.phases - 1)
        value = min((phase + fraction) / self.phases, 1.0) * 0.95
        if value > self._best:
            self._best = value
            self._on_progress(value)
