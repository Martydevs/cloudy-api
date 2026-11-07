import json
import zipfile
from io import BytesIO
from typing import Any

from conftest import AUTH, FakeChain, FakeDownloader
from fastapi.testclient import TestClient
from pptx import Presentation

import pytest

from cloudy_api.config import Settings
from cloudy_api.main import create_app
from cloudy_api.services import lyrics_pptx
from cloudy_api.services.lyrics_arranger import LyricsArranger
from cloudy_api.services.lyrics_pptx import unique_name

URL = "/v1/lyrics/pptx/batch"
BAD_HTML = "<div>nothing here</div><script>x</script><p></p>"


def song(title: str, *lines: str) -> str:
    return f"<h1>{title}</h1><div class='lyric-original'><p>{'<br>'.join(lines)}</p></div>"


SONG_A = song("Alpha", "A uno", "A dos")
SONG_B = song("Beta", "B uno", "B dos", "B tres")
SONG_C = song("Gamma", "C uno")


def read_zip(content: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    with zipfile.ZipFile(BytesIO(content)) as zf:
        entries = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(entries.pop("manifest.json"))
    return manifest, entries


def first_line(pptx: bytes) -> str:
    prs = Presentation(BytesIO(pptx))
    return prs.slides[0].shapes[0].text_frame.paragraphs[0].text


def test_returns_a_zip_with_every_presentation(client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = RuntimeError("use the fallback")

    r = client.post(
        URL, json={"items": [{"html": SONG_A}, {"html": SONG_B}, {"html": SONG_C}]}, headers=AUTH
    )

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="lyrics.zip"'
    assert r.headers["x-batch-succeeded"] == "3"
    assert r.headers["x-batch-failed"] == "0"
    manifest, entries = read_zip(r.content)
    assert sorted(entries) == ["Alpha.pptx", "Beta.pptx", "Gamma.pptx"]
    assert first_line(entries["Beta.pptx"]) == "B uno"
    assert manifest == {
        "items": [
            {
                "index": 0,
                "status": "done",
                "filename": "Alpha.pptx",
                "title": "Alpha",
                "arranged_by": "fallback",
                "slide_count": 1,
                "error": None,
            },
            {
                "index": 1,
                "status": "done",
                "filename": "Beta.pptx",
                "title": "Beta",
                "arranged_by": "fallback",
                "slide_count": 2,
                "error": None,
            },
            {
                "index": 2,
                "status": "done",
                "filename": "Gamma.pptx",
                "title": "Gamma",
                "arranged_by": "fallback",
                "slide_count": 1,
                "error": None,
            },
        ]
    }


def test_items_are_processed_in_order(client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = {"slides": [{"paragraphs": [{"start": 1, "end": 1}]}]}

    client.post(
        URL, json={"items": [{"html": SONG_C}, {"html": SONG_A}, {"html": SONG_B}]}, headers=AUTH
    )

    sent = [messages[0].content for messages in fake_chain.calls]
    assert sent == ["1: C uno", "1: A uno\n2: A dos", "1: B uno\n2: B dos\n3: B tres"]


def test_llm_arrangement_is_reported(client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = {"slides": [{"paragraphs": [{"start": 1, "end": 1}]}]}

    r = client.post(URL, json={"items": [{"html": SONG_C}]}, headers=AUTH)

    manifest, _ = read_zip(r.content)
    assert manifest["items"][0]["arranged_by"] == "llm"


def test_partial_failure_returns_the_good_items(
    client: TestClient, fake_chain: FakeChain
) -> None:
    fake_chain.result = RuntimeError("use the fallback")

    r = client.post(
        URL, json={"items": [{"html": SONG_A}, {"html": BAD_HTML}, {"html": SONG_C}]}, headers=AUTH
    )

    assert r.status_code == 200
    assert r.headers["x-batch-succeeded"] == "2"
    assert r.headers["x-batch-failed"] == "1"
    manifest, entries = read_zip(r.content)
    assert sorted(entries) == ["Alpha.pptx", "Gamma.pptx"]
    assert [item["status"] for item in manifest["items"]] == ["done", "failed", "done"]
    assert manifest["items"][1] == {
        "index": 1,
        "status": "failed",
        "filename": None,
        "title": None,
        "arranged_by": None,
        "slide_count": None,
        "error": {"code": "no_lyrics", "message": "No lyrics found in the HTML"},
    }
    # The bad item never reached the LLM.
    assert len(fake_chain.calls) == 2


def test_all_items_failing_is_422(client: TestClient, fake_chain: FakeChain) -> None:
    too_long = "<p>" + "<br>".join(["la"] * 301) + "</p>"

    r = client.post(URL, json={"items": [{"html": BAD_HTML}, {"html": too_long}]}, headers=AUTH)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "all_items_failed"
    assert [(i["index"], i["status"], i["error"]["code"]) for i in detail["items"]] == [
        (0, "failed", "no_lyrics"),
        (1, "failed", "lyrics_too_long"),
    ]
    assert fake_chain.calls == []


def test_too_many_items_is_422(client: TestClient, fake_chain: FakeChain) -> None:
    r = client.post(URL, json={"items": [{"html": SONG_A}] * 4}, headers=AUTH)

    assert r.status_code == 422
    assert r.json() == {
        "detail": {"code": "batch_too_large", "message": "At most 3 lyrics per request"}
    }
    assert fake_chain.calls == []


def test_limit_is_configurable(make_settings: Any, fake_downloader: FakeDownloader) -> None:
    settings: Settings = make_settings(max_lyrics_batch=1)
    app = create_app(settings, downloader=fake_downloader)  # type: ignore[arg-type]
    with TestClient(app) as client:
        r = client.post(URL, json={"items": [{"html": SONG_A}] * 2}, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["detail"]["message"] == "At most 1 lyrics per request"


def test_schema_caps_the_batch_size(client: TestClient, fake_chain: FakeChain) -> None:
    # Past the hard ceiling, validation rejects the list before any item is checked.
    r = client.post(URL, json={"items": [{"html": SONG_A}] * 21}, headers=AUTH)

    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "too_long"
    assert fake_chain.calls == []


def test_configured_limit_cannot_exceed_the_schema_cap(make_settings: Any) -> None:
    with pytest.raises(ValueError):
        make_settings(max_lyrics_batch=21)


def test_unexpected_error_fails_only_that_item(
    client: TestClient,
    fake_chain: FakeChain,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_chain.result = RuntimeError("use the fallback")
    real_build = lyrics_pptx.build_presentation

    def build(slides: Any, *, title: str | None, font_color: str) -> bytes:
        if title == "Beta":
            raise RecursionError("maximum recursion depth exceeded")
        return real_build(slides, title=title, font_color=font_color)

    monkeypatch.setattr(lyrics_pptx, "build_presentation", build)

    r = client.post(
        URL, json={"items": [{"html": SONG_A}, {"html": SONG_B}, {"html": SONG_C}]}, headers=AUTH
    )

    assert r.status_code == 200
    assert r.headers["x-batch-succeeded"] == "2"
    assert r.headers["x-batch-failed"] == "1"
    manifest, entries = read_zip(r.content)
    assert sorted(entries) == ["Alpha.pptx", "Gamma.pptx"]
    assert manifest["items"][1]["status"] == "failed"
    assert manifest["items"][1]["error"] == {
        "code": "internal_error",
        "message": "Unexpected error while converting these lyrics",
    }
    assert "lyrics.batch_item_failed index=1" in caplog.text


def test_items_past_the_deadline_use_the_fallback(
    make_settings: Any, fake_downloader: FakeDownloader, fake_chain: FakeChain
) -> None:
    settings: Settings = make_settings(lyrics_batch_deadline_seconds=0.05)
    fake_chain.result = {"slides": [{"paragraphs": [{"start": 1, "end": 1}]}]}
    fake_chain.delay = 0.5  # the first song's LLM call outlasts the whole budget
    app = create_app(
        settings,
        downloader=fake_downloader,  # type: ignore[arg-type]
        arranger=LyricsArranger(settings, chain=fake_chain),
    )

    with TestClient(app) as client:
        r = client.post(
            URL, json={"items": [{"html": SONG_A}, {"html": SONG_B}, {"html": SONG_C}]}, headers=AUTH
        )

    assert r.status_code == 200
    manifest, entries = read_zip(r.content)
    assert len(entries) == 3
    assert [item["arranged_by"] for item in manifest["items"]] == ["fallback"] * 3
    # Only the first song reached the LLM; the rest skipped it once time ran out.
    assert len(fake_chain.calls) == 1


def test_request_validation(client: TestClient, fake_chain: FakeChain) -> None:
    bad_bodies: list[dict[str, Any]] = [
        {},
        {"items": []},
        {"items": [{"html": ""}]},
        {"items": [{"html": SONG_A}, {"html": SONG_B, "font_color": "red"}]},
    ]
    for body in bad_bodies:
        r = client.post(URL, json=body, headers=AUTH)
        assert r.status_code == 422, body
        assert isinstance(r.json()["detail"], list)  # FastAPI's standard validation errors
    assert fake_chain.calls == []


def test_duplicate_titles_get_distinct_entries(client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = RuntimeError("use the fallback")

    r = client.post(
        URL,
        json={"items": [{"html": SONG_A}, {"html": SONG_B, "title": "alpha"}, {"html": SONG_A}]},
        headers=AUTH,
    )

    manifest, entries = read_zip(r.content)
    assert [item["filename"] for item in manifest["items"]] == [
        "Alpha.pptx",
        "alpha (2).pptx",
        "Alpha (3).pptx",
    ]
    assert first_line(entries["alpha (2).pptx"]) == "B uno"


def test_per_item_title_and_font_color(client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = RuntimeError("use the fallback")

    r = client.post(
        URL,
        json={
            "items": [
                {"html": SONG_A, "title": "Cómo Tú", "font_color": "#FFFFFF"},
                {"html": SONG_B, "font_color": "FF0000"},
            ]
        },
        headers=AUTH,
    )

    manifest, entries = read_zip(r.content)
    assert manifest["items"][0]["title"] == "Cómo Tú"
    assert manifest["items"][0]["filename"] == "Como Tu.pptx"
    colors = {}
    for name in ("Como Tu.pptx", "Beta.pptx"):
        prs = Presentation(BytesIO(entries[name]))
        run = prs.slides[0].shapes[0].text_frame.paragraphs[0].runs[0]
        colors[name] = str(run.font.color.rgb)
    assert colors == {"Como Tu.pptx": "FFFFFF", "Beta.pptx": "FF0000"}
    assert Presentation(BytesIO(entries["Como Tu.pptx"])).core_properties.title == "Cómo Tú"


def test_requires_api_key(client: TestClient, fake_chain: FakeChain) -> None:
    body = {"items": [{"html": SONG_A}]}
    assert client.post(URL, json=body).status_code == 401
    assert client.post(URL, json=body, headers={"X-API-Key": "x"}).status_code == 401
    assert fake_chain.calls == []


def test_is_documented_as_a_zip_download(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    ok = spec["paths"][URL]["post"]["responses"]["200"]
    assert "application/zip" in ok["content"]


def test_unique_name() -> None:
    taken = {"manifest.json"}
    assert unique_name("Song.pptx", taken) == "Song.pptx"
    assert unique_name("SONG.pptx", taken) == "SONG (2).pptx"
    assert unique_name("song.pptx", taken) == "song (3).pptx"
    assert unique_name("noext", taken) == "noext"
    assert unique_name("noext", taken) == "noext (2)"
