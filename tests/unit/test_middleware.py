from collections.abc import Callable, Iterator
from typing import Any

import pytest
from conftest import AUTH, FakeChain, FakeDownloader
from fastapi.testclient import TestClient

from cloudy_api.config import Settings
from cloudy_api.main import create_app
from cloudy_api.middleware import BodySizeLimitMiddleware, _content_length
from cloudy_api.services.lyrics_arranger import LyricsArranger

pytestmark = pytest.mark.anyio

LIMIT = 2048
URL = "/api/lyrics/pptx/batch"
TOO_LARGE = {
    "detail": {
        "code": "request_too_large",
        "message": f"Request body is larger than {LIMIT} bytes",
    }
}


@pytest.fixture
def small_client(
    make_settings: Callable[..., Settings], fake_downloader: FakeDownloader, fake_chain: FakeChain
) -> Iterator[TestClient]:
    settings = make_settings(max_request_bytes=LIMIT)
    app = create_app(
        settings,
        downloader=fake_downloader,  # type: ignore[arg-type]
        arranger=LyricsArranger(settings, chain=fake_chain),
    )
    with TestClient(app) as client:
        yield client


def big_body() -> dict[str, Any]:
    return {"items": [{"html": "<p>" + "la " * LIMIT + "</p>"}]}


def test_declared_oversized_body_is_rejected_unread(
    small_client: TestClient, fake_chain: FakeChain
) -> None:
    r = small_client.post(URL, json=big_body(), headers=AUTH)

    assert r.status_code == 413
    assert r.json() == TOO_LARGE
    assert r.headers["connection"] == "close"
    assert fake_chain.calls == []


def test_oversized_body_is_rejected_before_auth(small_client: TestClient) -> None:
    r = small_client.post(URL, json=big_body())

    assert r.status_code == 413


def test_streamed_oversized_body_is_cut_off(
    small_client: TestClient, fake_chain: FakeChain
) -> None:
    # A generator body is sent chunked, with no Content-Length to check up front.
    chunks = iter([b'{"items": [{"html": "', b"x" * LIMIT, b'"}]}'])

    r = small_client.post(
        URL, content=chunks, headers={**AUTH, "Content-Type": "application/json"}
    )

    assert "content-length" not in r.request.headers
    assert r.status_code == 413
    assert r.json() == TOO_LARGE
    assert fake_chain.calls == []


def test_body_within_the_limit_passes(small_client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = RuntimeError("use the fallback")
    body = {"items": [{"html": "<p>uno<br>dos</p>"}]}

    r = small_client.post(URL, json=body, headers=AUTH)

    assert r.status_code == 200


def test_default_limit_is_4_mib(settings: Settings) -> None:
    assert settings.max_request_bytes == 4 * 1024 * 1024


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([(b"content-length", b"42")], 42),
        ([(b"content-length", b"nope")], None),
        ([(b"host", b"x")], None),
    ],
)
def test_content_length(headers: list[tuple[bytes, bytes]], expected: int | None) -> None:
    assert _content_length({"type": "http", "headers": headers}) == expected


async def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def receive() -> Any:  # pragma: no cover - never called
        raise AssertionError

    async def send(message: Any) -> None:  # pragma: no cover - never called
        raise AssertionError

    await BodySizeLimitMiddleware(app, max_bytes=1)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]


async def test_disconnect_messages_are_passed_on() -> None:
    received: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        received.append(await receive())

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:  # pragma: no cover - never called
        raise AssertionError

    scope = {"type": "http", "headers": []}
    await BodySizeLimitMiddleware(app, max_bytes=1)(scope, receive, send)

    assert received == [{"type": "http.disconnect"}]
