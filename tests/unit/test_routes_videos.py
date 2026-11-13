from conftest import AUTH, FakeDownloader, FakeYouTubeApi, video_info, youtube_item
from fastapi.testclient import TestClient

from cloudy_api.services.downloader import DownloaderError

VIDEO_ID = "dQw4w9WgXcQ"


def test_get_video_metadata(client: TestClient, fake_downloader: FakeDownloader) -> None:
    r = client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "video_id": VIDEO_ID,
        "title": f"Title {VIDEO_ID}",
        "duration": 60,
        "thumbnail": f"https://i.ytimg.com/vi/{VIDEO_ID}/hq.jpg",
        "channel": "Channel",
        "available_heights": [360, 720, 1080],
        "downloadable": True,
        "reason": None,
    }
    assert fake_downloader.probed == [VIDEO_ID]


def test_get_video_too_long_is_not_downloadable(
    client: TestClient, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.probe_results[VIDEO_ID] = video_info(
        VIDEO_ID, duration=4000, downloadable=False, reason="too_long"
    )
    body = client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH).json()
    assert body["downloadable"] is False
    assert body["reason"] == "too_long"


def test_get_video_requires_api_key(client: TestClient, fake_downloader: FakeDownloader) -> None:
    assert client.get(f"/api/videos/{VIDEO_ID}").status_code == 401
    assert client.get(f"/api/videos/{VIDEO_ID}", headers={"X-API-Key": "x"}).status_code == 401
    assert fake_downloader.probed == []


def test_get_video_rejects_bad_id(client: TestClient, fake_downloader: FakeDownloader) -> None:
    r = client.get("/api/videos/not-an-id", headers=AUTH)
    assert r.status_code == 422
    assert fake_downloader.probed == []


def test_get_video_client_error_is_422(client: TestClient, fake_downloader: FakeDownloader) -> None:
    fake_downloader.probe_results[VIDEO_ID] = DownloaderError("private", "Private video")
    r = client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH)
    assert r.status_code == 422
    assert r.json() == {"detail": {"code": "private", "message": "Private video"}}


def test_get_video_server_error_is_502(client: TestClient, fake_downloader: FakeDownloader) -> None:
    fake_downloader.probe_results[VIDEO_ID] = DownloaderError(
        "bot_check", "Sign in to confirm you're not a bot", client_error=False
    )
    r = client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH)
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "bot_check"


# ------------------------------------------------------- YouTube Data API
def test_api_metadata_keeps_the_response_shape(
    api_client: TestClient,
    fake_downloader: FakeDownloader,
    fake_youtube: FakeYouTubeApi,
) -> None:
    fake_youtube.items[VIDEO_ID] = youtube_item(
        VIDEO_ID, title="Never Gonna Give You Up", duration="PT3M33S", channel="Rick Astley"
    )
    first = api_client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH)
    second = api_client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {
        "video_id": VIDEO_ID,
        "title": "Never Gonna Give You Up",
        "duration": 213,
        "thumbnail": f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg",
        "channel": "Rick Astley",
        "available_heights": [],
        "downloadable": True,
        "reason": None,
    }
    # Same keys, in the same order, as the engine-backed response.
    assert list(first.json()) == list(video_info(VIDEO_ID).model_dump())
    assert fake_downloader.probed == []
    assert fake_youtube.calls == [[VIDEO_ID]]  # the second request hit the cache


def test_api_live_video_is_not_downloadable(
    api_client: TestClient, fake_youtube: FakeYouTubeApi
) -> None:
    fake_youtube.items[VIDEO_ID] = youtube_item(VIDEO_ID, live="live", duration="P0D")
    body = api_client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH).json()
    assert (body["downloadable"], body["reason"], body["duration"]) == (False, "live", None)


def test_api_missing_video_is_422(api_client: TestClient, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.missing_ids.add(VIDEO_ID)
    r = api_client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unavailable"


def test_api_unreachable_is_502(api_client: TestClient, fake_youtube: FakeYouTubeApi) -> None:
    fake_youtube.error = TimeoutError()
    r = api_client.get(f"/api/videos/{VIDEO_ID}", headers=AUTH)
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "metadata_unavailable"
