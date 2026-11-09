import io
import threading
import zipfile
from collections.abc import Callable, Iterator
from urllib.parse import parse_qs, urlsplit

import httplib2
import pytest
from conftest import (
    AUTH,
    FakeDownloader,
    FakeYouTubeApi,
    video_info,
    wait_for_job,
    youtube_item,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from cloudy_api.config import Settings
from cloudy_api.main import create_app
from cloudy_api.schemas import JobStatus
from cloudy_api.security import sign
from cloudy_api.services.downloader import DownloaderError

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def create(client: TestClient, ids: list[str], fmt: str = "mp3") -> dict:
    r = client.post("/v1/jobs", headers=AUTH, json={"video_ids": ids, "format": fmt})
    assert r.status_code == 202, r.text
    return r.json()


def relative(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


# ------------------------------------------------------------------- create
def test_create_job(client: TestClient, fake_downloader: FakeDownloader) -> None:
    body = create(client, [A, B, A])
    assert body["status"] in ("queued", "running", "done")
    assert len(body["job_id"]) == 32
    assert sorted(fake_downloader.probed) == [A, B]  # de-duplicated


def test_create_job_requires_api_key(client: TestClient) -> None:
    r = client.post("/v1/jobs", json={"video_ids": [A], "format": "mp3"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"video_ids": ["bad"], "format": "mp3"},
        {"video_ids": [], "format": "mp3"},
        {"video_ids": [A], "format": "flac"},
        {"format": "mp3"},
    ],
)
def test_create_job_validation(client: TestClient, payload: dict) -> None:
    r = client.post("/v1/jobs", headers=AUTH, json=payload)
    assert r.status_code == 422


def test_create_job_batch_limit(client: TestClient, fake_downloader: FakeDownloader) -> None:
    ids = [f"{i:011d}" for i in range(11)]
    r = client.post("/v1/jobs", headers=AUTH, json={"video_ids": ids, "format": "mp3"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "batch_too_large"
    assert fake_downloader.probed == []


def test_create_job_rejects_unfit_videos(
    client: TestClient, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.probe_results[A] = video_info(
        A, duration=4000, downloadable=False, reason="too_long"
    )
    fake_downloader.probe_results[B] = DownloaderError("private", "Private video")
    fake_downloader.probe_results[C] = video_info(C, downloadable=False, reason="live")

    r = client.post("/v1/jobs", headers=AUTH, json={"video_ids": [A, B, C], "format": "mp3"})

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "videos_rejected"
    assert detail["videos"] == [
        {"video_id": A, "code": "too_long", "message": "Longer than 30 minutes"},
        {"video_id": B, "code": "private", "message": "Private video"},
        {"video_id": C, "code": "live", "message": "Video can't be downloaded"},
    ]


def test_create_job_reports_unknown_reason(
    client: TestClient, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.probe_results[A] = video_info(A, downloadable=False, reason=None)
    r = client.post("/v1/jobs", headers=AUTH, json={"video_ids": [A], "format": "mp3"})
    assert r.json()["detail"]["videos"][0]["code"] == "not_downloadable"


def test_create_job_server_side_probe_error_is_502(
    client: TestClient, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.probe_results[B] = DownloaderError("bot_check", "bot", client_error=False)
    r = client.post("/v1/jobs", headers=AUTH, json={"video_ids": [A, B], "format": "mp3"})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "bot_check"


@pytest.fixture
def busy_client(
    make_settings: Callable[..., Settings], fake_downloader: FakeDownloader
) -> Iterator[tuple[TestClient, threading.Event]]:
    """max_queue=1 with downloads held open, so the queue fills up."""
    gate = threading.Event()
    fake_downloader.gate = gate
    app = create_app(make_settings(max_queue=1), downloader=fake_downloader)  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c, gate
        gate.set()


def test_create_job_queue_full_is_429(
    busy_client: tuple[TestClient, threading.Event], fake_downloader: FakeDownloader
) -> None:
    client, _ = busy_client
    create(client, [A])  # picked up by the runner, blocks on the gate
    assert fake_downloader.started.wait(timeout=5)
    create(client, [B])  # fills the single queue slot
    probes_before = len(fake_downloader.probed)

    r = client.post("/v1/jobs", headers=AUTH, json={"video_ids": [C], "format": "mp3"})

    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "queue_full"
    assert len(fake_downloader.probed) == probes_before  # failed fast, no probe


def test_create_job_queue_fills_during_probe(
    busy_client: tuple[TestClient, threading.Event], fake_downloader: FakeDownloader
) -> None:
    client, _ = busy_client
    create(client, [A])
    assert fake_downloader.started.wait(timeout=5)
    manager = client.app.state.job_manager  # type: ignore[attr-defined]
    real_probe = fake_downloader.probe

    def probe_then_fill(video_id: str):  # type: ignore[no-untyped-def]
        result = real_probe(video_id)
        if video_id == C:
            manager.queue.put_nowait("someone-else")
        return result

    fake_downloader.probe = probe_then_fill  # type: ignore[method-assign]
    r = client.post("/v1/jobs", headers=AUTH, json={"video_ids": [C], "format": "mp3"})
    assert r.status_code == 429


# --------------------------------------------------------------------- poll
def test_single_job_lifecycle(client: TestClient, settings: Settings) -> None:
    job_id = create(client, [A], "mp4_720")["job_id"]
    body = wait_for_job(client, job_id)

    assert body["status"] == "done"
    assert body["format"] == "mp4_720"
    assert body["progress"] == 1.0
    assert body["filename"] == f"Downloaded {A} [{A}].mp4"
    assert body["size_bytes"] == len(f"mp4_720:{A}")
    assert body["items"] == [
        {
            "video_id": A,
            "title": f"Downloaded {A}",
            "status": "done",
            "progress": 1.0,
            "error": None,
            "actual_height": 720,
        }
    ]
    job = client.app.state.job_manager.get(job_id)  # type: ignore[attr-defined]
    assert body["expires_at"] == job.finished_at + settings.file_ttl_seconds

    url = body["download_url"]
    assert url.startswith(f"http://testserver/v1/jobs/{job_id}/file?")
    query = parse_qs(urlsplit(url).query)
    assert set(query) == {"exp", "sig"}

    r = client.get(relative(url))  # no API key needed
    assert r.status_code == 200
    assert r.content == f"mp4_720:{A}".encode()
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "Downloaded%20" in r.headers["content-disposition"]


def test_batch_job_downloads_zip(client: TestClient, fake_downloader: FakeDownloader) -> None:
    fake_downloader.download_errors[B] = DownloaderError("unavailable", "Video unavailable")
    job_id = create(client, [A, B, C])["job_id"]
    body = wait_for_job(client, job_id)

    assert body["status"] == "done"
    assert body["filename"] == f"cloudy-batch-{job_id[:8]}.zip"
    failed = [i for i in body["items"] if i["status"] == "failed"]
    assert failed == [
        {
            "video_id": B,
            "title": f"Title {B}",
            "status": "failed",
            "progress": 0.0,
            "error": {"code": "unavailable", "message": "Video unavailable"},
            "actual_height": None,
        }
    ]

    r = client.get(relative(body["download_url"]))
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == [
            f"Downloaded {A} [{A}].mp3",
            f"Downloaded {C} [{C}].mp3",
        ]


def test_failed_job_has_no_download_url(
    client: TestClient, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.download_errors[A] = DownloaderError("private", "Private video")
    body = wait_for_job(client, create(client, [A])["job_id"])
    assert body["status"] == "failed"
    assert body["download_url"] is None
    assert body["filename"] is None


def test_running_job_reports_progress(
    busy_client: tuple[TestClient, threading.Event], fake_downloader: FakeDownloader
) -> None:
    client, gate = busy_client
    job_id = create(client, [A])["job_id"]
    assert fake_downloader.started.wait(timeout=5)
    body = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "running"
    assert body["items"][0]["status"] == "running"
    assert body["download_url"] is None
    gate.set()
    assert wait_for_job(client, job_id)["status"] == "done"


def test_get_job_not_found(client: TestClient) -> None:
    r = client.get("/v1/jobs/missing", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_get_job_requires_api_key(client: TestClient) -> None:
    assert client.get("/v1/jobs/missing").status_code == 401


def test_download_url_uses_public_base_url(
    make_settings: Callable[..., Settings], fake_downloader: FakeDownloader
) -> None:
    app = create_app(
        make_settings(PUBLIC_BASE_URL="https://cloudy.onrender.com"),
        downloader=fake_downloader,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        body = wait_for_job(client, create(client, [A])["job_id"])
    assert body["download_url"].startswith(
        f"https://cloudy.onrender.com/v1/jobs/{body['job_id']}/file?exp="
    )


# ------------------------------------------------------------- signed file
@pytest.fixture
def done_job(client: TestClient) -> dict:
    return wait_for_job(client, create(client, [A])["job_id"])


def test_file_rejects_tampered_signature(client: TestClient, done_job: dict) -> None:
    url = relative(done_job["download_url"]).replace("sig=", "sig=0")
    r = client.get(url)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "invalid_signature"


def test_file_rejects_expired_link(
    client: TestClient, done_job: dict, settings: Settings
) -> None:
    exp, sig = sign(settings, done_job["job_id"], now=lambda: 0)
    r = client.get(f"/v1/jobs/{done_job['job_id']}/file?exp={exp}&sig={sig}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "link_expired"


def test_file_requires_signature_params(client: TestClient, done_job: dict) -> None:
    assert client.get(f"/v1/jobs/{done_job['job_id']}/file").status_code == 422


def test_file_for_unknown_job(client: TestClient, settings: Settings) -> None:
    exp, sig = sign(settings, "f" * 32)
    assert client.get(f"/v1/jobs/{'f' * 32}/file?exp={exp}&sig={sig}").status_code == 404


def test_file_for_expired_job_is_410(
    client: TestClient, app: FastAPI, done_job: dict
) -> None:
    manager = app.state.job_manager
    real_now = manager.now
    manager.now = lambda: real_now() + manager.settings.file_ttl_seconds + 1
    manager.sweep()

    r = client.get(relative(done_job["download_url"]))
    assert r.status_code == 410
    assert r.json()["detail"]["code"] == "expired"
    body = client.get(f"/v1/jobs/{done_job['job_id']}", headers=AUTH).json()
    assert body["status"] == "expired"
    assert body["download_url"] is None


def test_file_not_ready(
    busy_client: tuple[TestClient, threading.Event],
    fake_downloader: FakeDownloader,
    make_settings: Callable[..., Settings],
) -> None:
    client, _ = busy_client
    job_id = create(client, [A])["job_id"]
    assert fake_downloader.started.wait(timeout=5)
    exp, sig = sign(make_settings(), job_id)
    assert client.get(f"/v1/jobs/{job_id}/file?exp={exp}&sig={sig}").status_code == 404


def test_file_missing_on_disk(client: TestClient, done_job: dict, app: FastAPI) -> None:
    job = app.state.job_manager.get(done_job["job_id"])
    job.file_path.unlink()
    assert client.get(relative(done_job["download_url"])).status_code == 404


def test_file_unknown_extension_is_octet_stream(
    client: TestClient, done_job: dict, app: FastAPI
) -> None:
    job = app.state.job_manager.get(done_job["job_id"])
    renamed = job.file_path.with_suffix(".bin")
    job.file_path.rename(renamed)
    job.file_path = renamed
    r = client.get(relative(done_job["download_url"]))
    assert r.headers["content-type"] == "application/octet-stream"


# ------------------------------------------------------------------- delete
def test_delete_job(client: TestClient, done_job: dict, app: FastAPI) -> None:
    job_id = done_job["job_id"]
    job_dir = app.state.job_manager.job_dir(job_id)
    assert job_dir.exists()

    r = client.delete(f"/v1/jobs/{job_id}", headers=AUTH)

    assert r.status_code == 204
    assert not job_dir.exists()
    assert client.get(f"/v1/jobs/{job_id}", headers=AUTH).status_code == 404
    assert client.delete(f"/v1/jobs/{job_id}", headers=AUTH).status_code == 404


def test_delete_requires_api_key(client: TestClient) -> None:
    assert client.delete("/v1/jobs/whatever").status_code == 401


def test_delete_running_job(
    busy_client: tuple[TestClient, threading.Event], fake_downloader: FakeDownloader
) -> None:
    client, gate = busy_client
    job_id = create(client, [A])["job_id"]
    assert fake_downloader.started.wait(timeout=5)
    manager = client.app.state.job_manager  # type: ignore[attr-defined]
    job = manager.get(job_id)

    assert client.delete(f"/v1/jobs/{job_id}", headers=AUTH).status_code == 204
    gate.set()
    client.portal.call(manager.queue.join)  # type: ignore[union-attr]

    assert job.cancelled is True
    assert job.status is not JobStatus.DONE
    assert not manager.job_dir(job_id).exists()


# ------------------------------------------------ preflight via the Data API
@pytest.mark.parametrize("prefix", ["/v1", "/v2"])
def test_api_preflight_uses_one_call(
    api_client: TestClient,
    fake_downloader: FakeDownloader,
    fake_pytubefix: FakeDownloader,
    fake_youtube: FakeYouTubeApi,
    prefix: str,
) -> None:
    r = api_client.post(f"{prefix}/jobs", headers=AUTH, json={"video_ids": [A, B, C, A], "format": "mp3"})
    assert r.status_code == 202, r.text
    body = wait_for_job(api_client, r.json()["job_id"], prefix=prefix)
    assert body["status"] == "done"
    assert fake_youtube.calls == [[A, B, C]]
    assert fake_downloader.probed == fake_pytubefix.probed == []
    engine = fake_downloader if prefix == "/v1" else fake_pytubefix
    assert [video_id for video_id, _ in engine.downloaded] == [A, B, C]


def test_api_preflight_rejects_unfit_videos(
    api_client: TestClient, fake_youtube: FakeYouTubeApi
) -> None:
    fake_youtube.items[A] = youtube_item(A, duration="PT2H")
    fake_youtube.missing_ids.add(B)
    fake_youtube.items[C] = youtube_item(C, live="upcoming", duration="P0D")
    r = api_client.post("/v1/jobs", headers=AUTH, json={"video_ids": [A, B, C], "format": "mp3"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "videos_rejected"
    assert detail["videos"] == [
        {"video_id": A, "code": "too_long", "message": "Longer than 30 minutes"},
        {
            "video_id": B,
            "code": "unavailable",
            "message": "Video unavailable (private, deleted or nonexistent)",
        },
        {"video_id": C, "code": "live", "message": "Video can't be downloaded"},
    ]


def test_api_preflight_quota_exceeded_is_502(
    api_client: TestClient, fake_youtube: FakeYouTubeApi
) -> None:
    fake_youtube.error = HttpError(
        httplib2.Response({"status": 403, "reason": "Forbidden"}),
        b'{"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}',
    )
    r = api_client.post("/v2/jobs", headers=AUTH, json={"video_ids": [A], "format": "mp3"})
    assert r.status_code == 502
    assert r.json()["detail"] == {
        "code": "quota_exceeded",
        "message": "YouTube Data API quota exceeded",
    }
