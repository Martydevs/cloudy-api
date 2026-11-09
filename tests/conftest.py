import os

# cloudy_api.main builds a module-level `app` on import, which needs these.
os.environ.setdefault("API_KEY", "env-api-key")
os.environ.setdefault("SIGNING_SECRET", "env-signing-secret")

import asyncio  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cloudy_api.config import Settings  # noqa: E402
from cloudy_api.main import create_app  # noqa: E402
from cloudy_api.schemas import Format, VideoInfo  # noqa: E402
from cloudy_api.services.downloader import (  # noqa: E402
    Cancelled,
    DownloaderError,
    DownloadResult,
)
from cloudy_api.services.lyrics_arranger import LyricsArranger  # noqa: E402
from cloudy_api.services.youtube_api import YouTubeMetadata  # noqa: E402

API_KEY = "test-api-key"
SIGNING_SECRET = "test-signing-secret"
ORIGIN = "https://cloudy.vercel.app"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "api_key": API_KEY,
            "signing_secret": SIGNING_SECRET,
            "allowed_origins": ORIGIN,
            "work_dir": tmp_path / "work",
            # Engine-probe fallback unless a test opts into the Data API.
            "youtube_data_api_key": None,
            "_env_file": None,
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


def video_info(video_id: str, **overrides: Any) -> VideoInfo:
    values: dict[str, Any] = {
        "video_id": video_id,
        "title": f"Title {video_id}",
        "duration": 60,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hq.jpg",
        "channel": "Channel",
        "available_heights": [360, 720, 1080],
        "downloadable": True,
        "reason": None,
    }
    values.update(overrides)
    return VideoInfo(**values)


class FakeDownloader:
    """Stands in for services.downloader.Downloader: no network, no ffmpeg."""

    def __init__(self) -> None:
        self.probe_results: dict[str, VideoInfo | DownloaderError] = {}
        self.download_errors: dict[str, BaseException] = {}
        self.probed: list[str] = []
        self.downloaded: list[tuple[str, Format]] = []
        # Set to a threading.Event to hold downloads until the test releases them.
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def probe(self, video_id: str) -> VideoInfo:
        self.probed.append(video_id)
        result = self.probe_results.get(video_id) or video_info(video_id)
        if isinstance(result, DownloaderError):
            raise result
        return result

    def download(
        self,
        video_id: str,
        fmt: Format,
        dest: Path,
        on_progress: Callable[[float], None],
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> DownloadResult:
        self.downloaded.append((video_id, fmt))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if is_cancelled():
            raise Cancelled()
        if video_id in self.download_errors:
            raise self.download_errors[video_id]
        on_progress(0.5)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{video_id}.{fmt.ext}"
        path.write_bytes(f"{fmt.value}:{video_id}".encode())
        return DownloadResult(
            path=path,
            title=f"Downloaded {video_id}",
            actual_height=fmt.max_height,
        )


def youtube_item(
    video_id: str,
    *,
    title: str | None = None,
    duration: str | None = "PT1M",
    live: str = "none",
    channel: str | None = "Channel",
    thumbnails: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A ``videos.list`` item (part=snippet,contentDetails) as the Data API returns it."""
    snippet: dict[str, Any] = {
        "title": f"Title {video_id}" if title is None else title,
        "channelTitle": channel,
        "liveBroadcastContent": live,
        "thumbnails": thumbnails
        if thumbnails is not None
        else {
            "default": {"url": f"https://i.ytimg.com/vi/{video_id}/default.jpg"},
            "high": {"url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"},
        },
    }
    details: dict[str, Any] = {"definition": "hd"}
    if duration is not None:
        details["duration"] = duration
    return {"kind": "youtube#video", "id": video_id, "snippet": snippet, "contentDetails": details}


class _FakeVideosList:
    def __init__(self, api: FakeYouTubeApi, part: str, ids: list[str]) -> None:
        self._api = api
        self._part = part
        self._ids = ids

    def execute(self, num_retries: int = 0) -> dict[str, Any]:
        with self._api.lock:
            self._api.calls.append(self._ids)
            self._api.parts.append(self._part)
        if self._api.error is not None:
            raise self._api.error
        return {
            "kind": "youtube#videoListResponse",
            "items": [
                self._api.items.get(i) or youtube_item(i)
                for i in self._ids
                if i not in self._api.missing_ids
            ],
        }


class FakeYouTubeApi:
    """Stands in for ``googleapiclient.discovery.build``: no network.

    Every id is a 1-minute video unless overridden in ``items``; ids in
    ``missing_ids`` are left out of the response, like deleted/private videos.
    """

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.missing_ids: set[str] = set()
        self.error: BaseException | None = None
        self.calls: list[list[str]] = []
        self.parts: list[str] = []
        self.builds: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    # build(...) → client; client.videos().list(...).execute()
    def __call__(self, service: str, version: str, **kwargs: Any) -> FakeYouTubeApi:
        with self.lock:
            self.builds.append({"service": service, "version": version, **kwargs})
        return self

    def videos(self) -> FakeYouTubeApi:
        return self

    def list(self, *, part: str, id: str) -> _FakeVideosList:
        return _FakeVideosList(self, part, id.split(","))


class FakeChain:
    """Stands in for the OpenRouter structured-output chain: no network."""

    def __init__(self) -> None:
        # What ainvoke returns (a plan model or dict), or raises if it's an exception.
        self.result: Any = {"slides": []}
        self.calls: list[list[Any]] = []
        # Seconds each call takes, to simulate a slow LLM.
        self.delay = 0.0

    async def ainvoke(self, input: list[Any]) -> Any:
        self.calls.append(input)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def fake_downloader() -> FakeDownloader:
    return FakeDownloader()


@pytest.fixture
def fake_pytubefix() -> FakeDownloader:
    """The /v2 engine's stand-in, separate from the yt-dlp one."""
    return FakeDownloader()


@pytest.fixture
def fake_chain() -> FakeChain:
    return FakeChain()


@pytest.fixture
def app(
    settings: Settings,
    fake_downloader: FakeDownloader,
    fake_pytubefix: FakeDownloader,
    fake_chain: FakeChain,
) -> FastAPI:
    return create_app(
        settings,
        downloader=fake_downloader,  # type: ignore[arg-type]
        arranger=LyricsArranger(settings, chain=fake_chain),
        pytubefix_downloader=fake_pytubefix,  # type: ignore[arg-type]
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_youtube() -> FakeYouTubeApi:
    return FakeYouTubeApi()


@pytest.fixture
def api_client(
    make_settings: Callable[..., Settings],
    fake_downloader: FakeDownloader,
    fake_pytubefix: FakeDownloader,
    fake_youtube: FakeYouTubeApi,
) -> Iterator[TestClient]:
    """Like ``client``, but metadata comes from a (fake) YouTube Data API."""
    settings = make_settings(youtube_data_api_key="yt-test-key")
    app = create_app(
        settings,
        downloader=fake_downloader,  # type: ignore[arg-type]
        pytubefix_downloader=fake_pytubefix,  # type: ignore[arg-type]
        metadata=YouTubeMetadata(settings, build=fake_youtube),
    )
    with TestClient(app) as test_client:
        yield test_client


def wait_for_job(
    client: TestClient,
    job_id: str,
    statuses: tuple[str, ...] = ("done", "failed"),
    prefix: str = "/v1",
) -> dict[str, Any]:
    """Poll the job endpoint until it reaches one of `statuses` (bounded)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = client.get(f"{prefix}/jobs/{job_id}", headers=AUTH).json()
        if body["status"] in statuses:
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {statuses}")
