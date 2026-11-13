"""Video metadata through the YouTube Data API v3 (``videos.list``).

Replaces the pytubefix probe for ``GET /videos/{id}`` and the job
preflight: one official API call (1 quota unit, up to 50 ids) instead of a full
extraction, which is slow and triggers YouTube's bot check on datacenter IPs.
The API can't download media, so the engine still handles ``download()``.

Without ``YOUTUBE_DATA_API_KEY`` every call delegates to the engine's own
``probe`` (the previous behaviour).

Blocking, like the engine: callers run it through ``asyncio.to_thread``.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httplib2
from googleapiclient import discovery
from googleapiclient.errors import Error as GoogleApiError
from googleapiclient.errors import HttpError

from ..config import Settings
from ..schemas import VideoInfo
from .downloader import (
    DownloaderError,
    ProbeCache,
    VideoDownloader,
    not_downloadable_reason,
)

log = logging.getLogger(__name__)

# videos.list accepts at most 50 ids per call.
MAX_IDS_PER_CALL = 50
REQUEST_TIMEOUT_SECONDS = 15
NUM_RETRIES = 2
# Best first; maxres/standard only exist for some videos.
THUMBNAIL_SIZES = ("maxres", "standard", "high", "medium", "default")
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_LIVE_CONTENT = frozenset({"live", "upcoming"})
_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

BuildFactory = Callable[..., Any]
ProbeResults = dict[str, VideoInfo | DownloaderError]


def parse_iso_duration(value: str | None) -> int | None:
    """``PT1H2M3S`` → 3723. ``P0D`` (live/upcoming), missing or malformed → None."""
    if not value:
        return None
    match = _ISO_DURATION.match(value)
    if match is None:
        return None
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    seconds = (
        parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    )
    return seconds or None


def best_thumbnail(thumbnails: Mapping[str, Any] | None) -> str | None:
    for size in THUMBNAIL_SIZES:
        url = ((thumbnails or {}).get(size) or {}).get("url")
        if url:
            return url
    return None


def _error_reasons(exc: HttpError) -> set[str]:
    """The ``error.errors[].reason`` values of a Google API error body."""
    try:
        data = json.loads(exc.content.decode("utf-8"))
        return {e["reason"] for e in data["error"]["errors"] if "reason" in e}
    except (ValueError, KeyError, TypeError):
        return set()


def map_api_error(exc: Exception) -> DownloaderError:
    # Never use str(exc): HttpError's repr includes the request URI, and with
    # it the API key.
    if isinstance(exc, HttpError):
        if exc.status_code == 403 and _error_reasons(exc) & _QUOTA_REASONS:
            return DownloaderError(
                "quota_exceeded", "YouTube Data API quota exceeded", client_error=False
            )
        detail = exc.reason or f"HTTP {exc.status_code}"
        message = f"YouTube Data API error: {detail}"
    else:
        message = f"YouTube Data API unreachable: {type(exc).__name__}"
    return DownloaderError("metadata_unavailable", message[:300], client_error=False)


class YouTubeMetadata:
    def __init__(
        self,
        settings: Settings,
        *,
        build: BuildFactory = discovery.build,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._build = build
        self._cache = ProbeCache(now)
        # The httplib2-backed client isn't thread-safe and callers run in a
        # thread pool, so every thread builds its own.
        self._local = threading.local()

    @property
    def enabled(self) -> bool:
        return self._settings.youtube_data_api_key is not None

    def _client(self) -> Any:
        client = getattr(self._local, "client", None)
        if client is None:
            key = self._settings.youtube_data_api_key
            assert key is not None
            client = self._build(
                "youtube",
                "v3",
                developerKey=key.get_secret_value(),
                cache_discovery=False,
                http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS),
            )
            self._local.client = client
        return client

    # ----------------------------------------------------------------- probe
    def probe(self, video_id: str, *, fallback: VideoDownloader) -> VideoInfo:
        """Metadata for one video; raises ``DownloaderError`` like the engine."""
        if not self.enabled:
            return fallback.probe(video_id)
        result = self.probe_many([video_id], fallback=fallback)[video_id]
        if isinstance(result, DownloaderError):
            raise result
        return result

    def probe_many(
        self, video_ids: Iterable[str], *, fallback: VideoDownloader
    ) -> ProbeResults:
        """``{video_id: VideoInfo | DownloaderError}`` for every id.

        Per-video problems (e.g. a deleted video) come back as values; a failure
        of the API itself (quota, network, bad key) raises ``DownloaderError``.
        """
        ids = list(dict.fromkeys(video_ids))
        if not self.enabled:
            return self._fallback_many(ids, fallback)

        results: ProbeResults = {}
        missing: list[str] = []
        for video_id in ids:
            cached = self._cache.get(video_id)
            if cached is not None:
                results[video_id] = cached
            else:
                missing.append(video_id)

        for start in range(0, len(missing), MAX_IDS_PER_CALL):
            chunk = missing[start : start + MAX_IDS_PER_CALL]
            items = self._videos_list(chunk)
            for video_id in chunk:
                item = items.get(video_id)
                if item is None:
                    # The API can't tell private from deleted or nonexistent.
                    results[video_id] = DownloaderError(
                        "unavailable", "Video unavailable (private, deleted or nonexistent)"
                    )
                    continue
                info = self._to_video_info(video_id, item)
                self._cache.put(info)
                results[video_id] = info
        return {video_id: results[video_id] for video_id in ids}

    def _videos_list(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        log.info("youtube_api.videos_list ids=%d", len(ids))
        try:
            response = (
                self._client()
                .videos()
                .list(part="snippet,contentDetails", id=",".join(ids))
                .execute(num_retries=NUM_RETRIES)
            )
        except (GoogleApiError, httplib2.HttpLib2Error, OSError) as exc:
            error = map_api_error(exc)
            log.warning("youtube_api.error code=%s message=%s", error.code, error.message)
            raise error from exc
        return {
            item["id"]: item
            for item in response.get("items") or []
            if isinstance(item, dict) and "id" in item
        }

    def _to_video_info(self, video_id: str, item: dict[str, Any]) -> VideoInfo:
        snippet: dict[str, Any] = item.get("snippet") or {}
        details: dict[str, Any] = item.get("contentDetails") or {}
        duration = parse_iso_duration(details.get("duration"))
        reason = not_downloadable_reason(
            live=snippet.get("liveBroadcastContent") in _LIVE_CONTENT,
            duration=duration,
            max_duration=self._settings.max_duration_seconds,
        )
        return VideoInfo(
            video_id=video_id,
            title=snippet.get("title") or video_id,
            duration=duration,
            thumbnail=best_thumbnail(snippet.get("thumbnails")),
            channel=snippet.get("channelTitle") or None,
            # The API only reports hd/sd, not real heights; the downloaded
            # height comes back as the job item's actual_height.
            available_heights=[],
            downloadable=reason is None,
            reason=reason,
        )

    # -------------------------------------------------------------- fallback
    def _fallback_many(self, ids: list[str], fallback: VideoDownloader) -> ProbeResults:
        def probe_one(video_id: str) -> VideoInfo | DownloaderError:
            try:
                return fallback.probe(video_id)
            except DownloaderError as exc:
                return exc

        if len(ids) <= 1:
            return {video_id: probe_one(video_id) for video_id in ids}
        # Each engine probe runs a pytubefix extraction (+ Node): bounded.
        workers = min(self._settings.probe_concurrency, len(ids))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            return dict(zip(ids, pool.map(probe_one, ids), strict=True))
