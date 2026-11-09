import json
import threading
import time
from collections.abc import Callable
from typing import Any

import httplib2
import pytest
from conftest import FakeDownloader, FakeYouTubeApi, video_info, youtube_item
from googleapiclient.errors import HttpError

from cloudy_api.config import Settings
from cloudy_api.schemas import VideoInfo
from cloudy_api.services import youtube_api
from cloudy_api.services.downloader import PROBE_CACHE_TTL_SECONDS, DownloaderError
from cloudy_api.services.youtube_api import (
    YouTubeMetadata,
    best_thumbnail,
    map_api_error,
    parse_iso_duration,
)

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"
KEY = "yt-secret-key"


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def api_settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(youtube_data_api_key=KEY)


@pytest.fixture
def metadata(api_settings: Settings, fake_youtube: FakeYouTubeApi) -> YouTubeMetadata:
    return YouTubeMetadata(api_settings, build=fake_youtube)


def http_error(status: int, body: Any, reason: str = "Forbidden") -> HttpError:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    resp = httplib2.Response({"status": status})
    resp.reason = reason  # set from the HTTP status line in real responses
    return HttpError(resp, content, uri=f"https://youtube.googleapis.com/youtube/v3/videos?key={KEY}")


def google_error(status: int, reason: str, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"message": message, "domain": "youtube.quota", "reason": reason}],
        }
    }


# ---------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("PT4M", 240),
        ("PT2H", 7200),
        ("PT10M0S", 600),
        ("P1DT1S", 86401),
        ("P0D", None),  # live / upcoming
        ("PT0S", None),
        ("", None),
        (None, None),
        ("1:02:03", None),
    ],
)
def test_parse_iso_duration(value: str | None, seconds: int | None) -> None:
    assert parse_iso_duration(value) == seconds


@pytest.mark.parametrize(
    ("thumbnails", "url"),
    [
        (
            {size: {"url": size} for size in ("default", "medium", "high", "standard", "maxres")},
            "maxres",
        ),
        ({"default": {"url": "default"}, "standard": {"url": "standard"}}, "standard"),
        ({"default": {"url": "default"}, "medium": {"url": "medium"}}, "medium"),
        ({"default": {"url": "default"}, "high": {"url": ""}}, "default"),
        ({}, None),
        (None, None),
    ],
)
def test_best_thumbnail(thumbnails: dict | None, url: str | None) -> None:
    assert best_thumbnail(thumbnails) == url


# ------------------------------------------------------------------ probe
def test_probe_maps_api_item(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.items[A] = youtube_item(A, title="Song", duration="PT3M30S", channel="Choir")
    assert metadata.probe(A, fallback=FakeDownloader()) == VideoInfo(
        video_id=A,
        title="Song",
        duration=210,
        thumbnail=f"https://i.ytimg.com/vi/{A}/hqdefault.jpg",
        channel="Choir",
        available_heights=[],
        downloadable=True,
        reason=None,
    )
    assert fake_youtube.calls == [[A]]
    assert fake_youtube.parts == ["snippet,contentDetails"]


def test_probe_never_uses_the_engine(metadata: YouTubeMetadata) -> None:
    engine = FakeDownloader()
    metadata.probe(A, fallback=engine)
    metadata.probe_many([A, B], fallback=engine)
    assert engine.probed == []


@pytest.mark.parametrize("live", ["live", "upcoming"])
def test_live_and_upcoming_are_not_downloadable(
    metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi, live: str
) -> None:
    fake_youtube.items[A] = youtube_item(A, live=live, duration="P0D")
    info = metadata.probe(A, fallback=FakeDownloader())
    assert (info.downloadable, info.reason, info.duration) == (False, "live", None)


def test_too_long(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.items[A] = youtube_item(A, duration="PT30M1S")  # max is 1800 s
    info = metadata.probe(A, fallback=FakeDownloader())
    assert (info.downloadable, info.reason, info.duration) == (False, "too_long", 1801)


def test_unknown_duration(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.items[A] = youtube_item(A, duration=None)
    info = metadata.probe(A, fallback=FakeDownloader())
    assert (info.downloadable, info.reason, info.duration) == (False, "unknown_duration", None)


def test_missing_snippet_fields(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.items[A] = youtube_item(A, title="", channel=None, thumbnails={})
    info = metadata.probe(A, fallback=FakeDownloader())
    assert (info.title, info.channel, info.thumbnail) == (A, None, None)


def test_item_without_parts(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.items[A] = {"id": A}
    info = metadata.probe(A, fallback=FakeDownloader())
    assert (info.title, info.reason) == (A, "unknown_duration")


def test_missing_video_is_unavailable(
    metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi
) -> None:
    fake_youtube.missing_ids.add(A)
    with pytest.raises(DownloaderError) as exc_info:
        metadata.probe(A, fallback=FakeDownloader())
    assert exc_info.value.code == "unavailable"
    assert exc_info.value.client_error is True


# ------------------------------------------------------------- probe_many
def test_probe_many_returns_per_video_results(
    metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi
) -> None:
    fake_youtube.missing_ids.add(B)
    results = metadata.probe_many([A, B, C, A], fallback=FakeDownloader())
    assert list(results) == [A, B, C]
    assert isinstance(results[A], VideoInfo) and isinstance(results[C], VideoInfo)
    assert isinstance(results[B], DownloaderError) and results[B].code == "unavailable"
    assert fake_youtube.calls == [[A, B, C]]  # one call, de-duplicated


def test_probe_many_chunks_by_fifty(
    metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi
) -> None:
    ids = [f"{i:011d}" for i in range(120)]
    results = metadata.probe_many(ids, fallback=FakeDownloader())
    assert list(results) == ids
    assert [len(call) for call in fake_youtube.calls] == [50, 50, 20]
    assert [video_id for call in fake_youtube.calls for video_id in call] == ids


def test_probe_many_empty(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    assert metadata.probe_many([], fallback=FakeDownloader()) == {}
    assert fake_youtube.calls == []


def test_ignores_malformed_items(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.items[A] = {"snippet": {"title": "no id"}}  # type: ignore[dict-item]
    result = metadata.probe_many([A], fallback=FakeDownloader())[A]
    assert isinstance(result, DownloaderError) and result.code == "unavailable"


# ------------------------------------------------------------------ cache
def test_results_are_cached(api_settings: Settings, fake_youtube: FakeYouTubeApi) -> None:
    clock = Clock()
    metadata = YouTubeMetadata(api_settings, build=fake_youtube, now=clock)
    engine = FakeDownloader()
    first = metadata.probe(A, fallback=engine)
    assert metadata.probe(A, fallback=engine) is first
    metadata.probe_many([A, B], fallback=engine)
    assert fake_youtube.calls == [[A], [B]]  # only the uncached id

    clock.value += PROBE_CACHE_TTL_SECONDS + 1
    metadata.probe(A, fallback=engine)
    assert fake_youtube.calls[-1] == [A]


def test_missing_videos_are_not_cached(
    metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi
) -> None:
    fake_youtube.missing_ids.add(A)
    for _ in range(2):
        with pytest.raises(DownloaderError):
            metadata.probe(A, fallback=FakeDownloader())
    assert fake_youtube.calls == [[A], [A]]


# ----------------------------------------------------------------- errors
def test_quota_exceeded(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.error = http_error(
        403, google_error(403, "quotaExceeded", "The request cannot be completed because you have exceeded your quota.")
    )
    with pytest.raises(DownloaderError) as exc_info:
        metadata.probe_many([A, B], fallback=FakeDownloader())
    error = exc_info.value
    assert (error.code, error.client_error) == ("quota_exceeded", False)
    assert KEY not in error.message


def test_invalid_key(metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.error = http_error(
        400, google_error(400, "badRequest", "API key not valid. Please pass a valid API key."),
        reason="Bad Request",
    )
    with pytest.raises(DownloaderError) as exc_info:
        metadata.probe(A, fallback=FakeDownloader())
    error = exc_info.value
    assert (error.code, error.client_error) == ("metadata_unavailable", False)
    assert error.message == "YouTube Data API error: API key not valid. Please pass a valid API key."
    assert KEY not in error.message


@pytest.mark.parametrize(
    ("error", "message"),
    [
        # A 403 that isn't about quota (e.g. the API is disabled for the key).
        (
            http_error(403, google_error(403, "accessNotConfigured", "API disabled")),
            "YouTube Data API error: API disabled",
        ),
        (http_error(503, b"<html>down</html>", reason="Service Unavailable"),
         "YouTube Data API error: Service Unavailable"),
        (http_error(500, b"", reason=""), "YouTube Data API error: HTTP 500"),
        (TimeoutError("timed out"), "YouTube Data API unreachable: TimeoutError"),
        (httplib2.ServerNotFoundError("dns"), "YouTube Data API unreachable: ServerNotFoundError"),
    ],
)
def test_other_failures_are_502(
    metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi, error: Exception, message: str
) -> None:
    fake_youtube.error = error
    with pytest.raises(DownloaderError) as exc_info:
        metadata.probe(A, fallback=FakeDownloader())
    assert (exc_info.value.code, exc_info.value.client_error) == ("metadata_unavailable", False)
    assert exc_info.value.message == message


def test_map_api_error_ignores_unparsable_bodies() -> None:
    error = map_api_error(http_error(403, {"error": "nope"}))
    assert error.code == "metadata_unavailable"


# ----------------------------------------------------------------- client
def test_client_is_built_once_per_thread(
    metadata: YouTubeMetadata, fake_youtube: FakeYouTubeApi
) -> None:
    metadata.probe(A, fallback=FakeDownloader())
    metadata.probe(B, fallback=FakeDownloader())
    assert len(fake_youtube.builds) == 1
    build = fake_youtube.builds[0]
    assert (build["service"], build["version"]) == ("youtube", "v3")
    assert build["developerKey"] == KEY
    assert build["cache_discovery"] is False
    assert isinstance(build["http"], httplib2.Http)
    assert build["http"].timeout == youtube_api.REQUEST_TIMEOUT_SECONDS

    thread = threading.Thread(target=metadata.probe, args=(C,), kwargs={"fallback": FakeDownloader()})
    thread.start()
    thread.join()
    assert len(fake_youtube.builds) == 2


def test_default_build_creates_a_real_client(api_settings: Settings) -> None:
    # Static discovery document: no network needed to build the client.
    client = YouTubeMetadata(api_settings)._client()
    assert hasattr(client, "videos")


# --------------------------------------------------------------- fallback
def test_without_key_probe_uses_the_engine(
    settings: Settings, fake_youtube: FakeYouTubeApi
) -> None:
    metadata = YouTubeMetadata(settings, build=fake_youtube)
    engine = FakeDownloader()
    assert metadata.enabled is False
    assert metadata.probe(A, fallback=engine) == video_info(A)
    engine.probe_results[B] = DownloaderError("private", "Private video")
    with pytest.raises(DownloaderError):
        metadata.probe(B, fallback=engine)
    assert engine.probed == [A, B]
    assert fake_youtube.builds == [] and fake_youtube.calls == []


def test_without_key_probe_many_uses_the_engine(settings: Settings) -> None:
    metadata = YouTubeMetadata(settings)
    engine = FakeDownloader()
    engine.probe_results[B] = DownloaderError("private", "Private video")
    results = metadata.probe_many([A, B, C], fallback=engine)
    assert list(results) == [A, B, C]
    assert results[A] == video_info(A)
    assert isinstance(results[B], DownloaderError) and results[B].code == "private"
    assert sorted(engine.probed) == [A, B, C]

    engine.probed.clear()
    assert metadata.probe_many([A], fallback=engine) == {A: video_info(A)}
    assert engine.probed == [A]


def test_fallback_respects_probe_concurrency(make_settings: Callable[..., Settings]) -> None:
    metadata = YouTubeMetadata(make_settings(probe_concurrency=2))
    lock = threading.Lock()
    running = peak = 0

    class SlowEngine(FakeDownloader):
        def probe(self, video_id: str) -> VideoInfo:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.02)
            with lock:
                running -= 1
            return super().probe(video_id)

    ids = [f"{i:011d}" for i in range(6)]
    assert list(metadata.probe_many(ids, fallback=SlowEngine())) == ids
    assert peak == 2
