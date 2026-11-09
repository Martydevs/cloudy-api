"""/v2 routes: same contract as /v1, backed by the pytubefix engine."""

from collections.abc import Callable
from urllib.parse import urlsplit

import pytest
from conftest import AUTH, FakeDownloader, video_info, wait_for_job
from fastapi.testclient import TestClient

from cloudy_api.config import Settings
from cloudy_api.main import create_app
from cloudy_api.schemas import Engine
from cloudy_api.services.downloader import DownloaderError

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def create(client: TestClient, ids: list[str], fmt: str = "mp3", prefix: str = "/v2") -> str:
    r = client.post(f"{prefix}/jobs", headers=AUTH, json={"video_ids": ids, "format": fmt})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def relative(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


# ------------------------------------------------------------------- videos
def test_get_video_uses_pytubefix(
    client: TestClient, fake_downloader: FakeDownloader, fake_pytubefix: FakeDownloader
) -> None:
    r = client.get(f"/v2/videos/{A}", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == video_info(A).model_dump()
    assert fake_pytubefix.probed == [A]
    assert fake_downloader.probed == []


def test_v1_still_uses_ytdlp(
    client: TestClient, fake_downloader: FakeDownloader, fake_pytubefix: FakeDownloader
) -> None:
    assert client.get(f"/v1/videos/{A}", headers=AUTH).status_code == 200
    assert fake_downloader.probed == [A]
    assert fake_pytubefix.probed == []


def test_get_video_requires_api_key(client: TestClient, fake_pytubefix: FakeDownloader) -> None:
    assert client.get(f"/v2/videos/{A}").status_code == 401
    assert client.get(f"/v2/videos/{A}", headers={"X-API-Key": "x"}).status_code == 401
    assert client.get("/v2/videos/not-an-id", headers=AUTH).status_code == 422
    assert fake_pytubefix.probed == []


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (DownloaderError("private", "Private video"), 422),
        (DownloaderError("bot_check", "not a bot", client_error=False), 502),
    ],
)
def test_get_video_errors(
    client: TestClient, fake_pytubefix: FakeDownloader, error: DownloaderError, status: int
) -> None:
    fake_pytubefix.probe_results[A] = error
    r = client.get(f"/v2/videos/{A}", headers=AUTH)
    assert r.status_code == status
    assert r.json() == {"detail": {"code": error.code, "message": error.message}}


# --------------------------------------------------------------------- jobs
def test_job_lifecycle(
    client: TestClient,
    settings: Settings,
    fake_downloader: FakeDownloader,
    fake_pytubefix: FakeDownloader,
) -> None:
    job_id = create(client, [A, A], "mp4_720")
    body = wait_for_job(client, job_id, prefix="/v2")

    assert body["status"] == "done"
    assert body["items"][0]["actual_height"] == 720
    assert body["filename"] == f"Downloaded {A} [{A}].mp4"
    assert body["download_url"].startswith(f"http://testserver/v2/jobs/{job_id}/file?")
    assert fake_pytubefix.probed == [A]  # de-duplicated
    assert fake_pytubefix.downloaded and fake_downloader.downloaded == []
    assert client.app.state.job_manager.get(job_id).engine is Engine.PYTUBEFIX  # type: ignore[attr-defined]

    r = client.get(relative(body["download_url"]))  # signed, no API key
    assert r.status_code == 200
    assert r.content == f"mp4_720:{A}".encode()
    assert r.headers["content-type"] == "video/mp4"

    r = client.get(f"/v2/jobs/{job_id}/file?exp=1&sig=bad")
    assert r.status_code in (401, 403)

    assert client.delete(f"/v2/jobs/{job_id}", headers=AUTH).status_code == 204
    assert client.get(f"/v2/jobs/{job_id}", headers=AUTH).status_code == 404
    assert client.delete(f"/v2/jobs/{job_id}", headers=AUTH).status_code == 404


def test_v1_and_v2_jobs_use_their_own_engine(
    client: TestClient, fake_downloader: FakeDownloader, fake_pytubefix: FakeDownloader
) -> None:
    v1 = wait_for_job(client, create(client, [A], prefix="/v1"))
    v2 = wait_for_job(client, create(client, [B]), prefix="/v2")
    assert fake_downloader.downloaded == [(A, v1["format"])]
    assert fake_pytubefix.downloaded == [(B, v2["format"])]
    assert "/v1/jobs/" in v1["download_url"]
    assert "/v2/jobs/" in v2["download_url"]


def test_create_job_requires_api_key(client: TestClient) -> None:
    r = client.post("/v2/jobs", json={"video_ids": [A], "format": "mp3"})
    assert r.status_code == 401
    assert client.get("/v2/jobs/x").status_code == 401
    assert client.delete("/v2/jobs/x").status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"video_ids": ["bad"], "format": "mp3"},
        {"video_ids": [], "format": "mp3"},
        {"video_ids": [A], "format": "flac"},
    ],
)
def test_create_job_validation(client: TestClient, payload: dict) -> None:
    assert client.post("/v2/jobs", headers=AUTH, json=payload).status_code == 422


def test_create_job_batch_limit(client: TestClient, fake_pytubefix: FakeDownloader) -> None:
    ids = [f"{i:011d}" for i in range(11)]
    r = client.post("/v2/jobs", headers=AUTH, json={"video_ids": ids, "format": "mp3"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "batch_too_large"
    assert fake_pytubefix.probed == []


def test_create_job_rejects_unfit_videos(
    client: TestClient, fake_pytubefix: FakeDownloader
) -> None:
    fake_pytubefix.probe_results[A] = video_info(A, downloadable=False, reason="live")
    fake_pytubefix.probe_results[B] = DownloaderError("private", "Private video")
    r = client.post("/v2/jobs", headers=AUTH, json={"video_ids": [A, B, C], "format": "mp3"})
    assert r.status_code == 422
    assert [v["code"] for v in r.json()["detail"]["videos"]] == ["live", "private"]


def test_create_job_server_side_probe_error_is_502(
    client: TestClient, fake_pytubefix: FakeDownloader
) -> None:
    fake_pytubefix.probe_results[A] = DownloaderError("bot_check", "bot", client_error=False)
    r = client.post("/v2/jobs", headers=AUTH, json={"video_ids": [A], "format": "mp3"})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "bot_check"


def test_queue_is_shared_with_v1(
    make_settings: Callable[..., Settings],
    fake_downloader: FakeDownloader,
    fake_pytubefix: FakeDownloader,
) -> None:
    app = create_app(
        make_settings(max_queue=1),
        downloader=fake_downloader,  # type: ignore[arg-type]
        pytubefix_downloader=fake_pytubefix,  # type: ignore[arg-type]
    )
    client = TestClient(app)  # no lifespan: the runner never drains the queue
    assert create(client, [A], prefix="/v1")
    for prefix in ("/v1", "/v2"):
        r = client.post(f"{prefix}/jobs", headers=AUTH, json={"video_ids": [B], "format": "mp3"})
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "queue_full"
