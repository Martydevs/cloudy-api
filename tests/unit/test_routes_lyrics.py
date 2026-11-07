from io import BytesIO
from typing import Any
from urllib.parse import quote

from conftest import AUTH, FakeChain, FakeDownloader
from fastapi.testclient import TestClient
from pptx import Presentation

from cloudy_api.config import Settings
from cloudy_api.main import create_app
from cloudy_api.services.pptx_builder import PPTX_MEDIA_TYPE

URL = "/v1/lyrics/pptx"
HTML = """
<h1>De Buenas Part</h1>
<div class="lyric-original">
  <p>Uno<br>Dos<br>Tres<br>Cuatro</p>
  <p>Cinco<br>Seis</p>
  <p>Siete<br>Ocho</p>
</div>
"""


def slide_texts(response_content: bytes) -> list[list[str]]:
    prs = Presentation(BytesIO(response_content))
    return [
        [p.text.replace("\x0b", "|") for p in slide.shapes[0].text_frame.paragraphs]
        for slide in prs.slides
    ]


def test_returns_a_pptx_arranged_by_the_llm(client: TestClient, fake_chain: FakeChain) -> None:
    lines = [[1, 2], [3], [4], [5, 6], [7, 8]]
    fake_chain.result = {
        "slides": [{"paragraphs": [{"start": n, "end": n} for n in slide]} for slide in lines]
    }

    r = client.post(URL, json={"html": HTML}, headers=AUTH)

    assert r.status_code == 200
    assert r.headers["content-type"] == PPTX_MEDIA_TYPE
    assert r.headers["x-lyrics-arranged-by"] == "llm"
    assert r.headers["x-slide-count"] == "5"
    assert r.headers["content-disposition"] == (
        'attachment; filename="De Buenas Part.pptx"; filename*=UTF-8\'\'De%20Buenas%20Part.pptx'
    )
    assert slide_texts(r.content) == [
        ["Uno", "Dos"],
        ["Tres"],
        ["Cuatro"],
        ["Cinco", "Seis"],
        ["Siete", "Ocho"],
    ]
    # The model saw only the numbered lyrics; the preset carries the instructions.
    (messages,) = fake_chain.calls
    assert [m.content for m in messages] == ["1: Uno\n2: Dos\n3: Tres\n4: Cuatro\n\n5: Cinco\n6: Seis\n\n7: Siete\n8: Ocho"]


def test_falls_back_when_the_llm_fails(client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = RuntimeError("OpenRouter is down")

    r = client.post(URL, json={"html": HTML}, headers=AUTH)

    assert r.status_code == 200
    assert r.headers["x-lyrics-arranged-by"] == "fallback"
    assert r.headers["x-slide-count"] == "4"
    # Two single-line paragraphs per slide; stanzas never share a slide.
    assert slide_texts(r.content) == [
        ["Uno", "Dos"],
        ["Tres", "Cuatro"],
        ["Cinco", "Seis"],
        ["Siete", "Ocho"],
    ]


def test_falls_back_when_the_llm_plan_mixes_stanzas(
    client: TestClient, fake_chain: FakeChain
) -> None:
    lines = [[1, 2], [3], [4, 5], [6, 7], [8]]  # 4+5 and 6+7 cross stanza breaks
    fake_chain.result = {
        "slides": [{"paragraphs": [{"start": n, "end": n} for n in slide]} for slide in lines]
    }

    r = client.post(URL, json={"html": HTML}, headers=AUTH)

    assert r.status_code == 200
    assert r.headers["x-lyrics-arranged-by"] == "fallback"
    assert r.headers["x-slide-count"] == "4"


def test_title_and_font_color_overrides(client: TestClient, fake_chain: FakeChain) -> None:
    fake_chain.result = RuntimeError("skip the LLM")
    title = "Cómo Tú"

    r = client.post(URL, json={"html": HTML, "title": title, "font_color": "#FFFFFF"}, headers=AUTH)

    assert r.status_code == 200
    assert f"filename*=UTF-8''{quote('Cómo Tú.pptx')}" in r.headers["content-disposition"]
    assert 'filename="Como Tu.pptx"' in r.headers["content-disposition"]
    prs = Presentation(BytesIO(r.content))
    assert prs.core_properties.title == title
    run = prs.slides[0].shapes[0].text_frame.paragraphs[0].runs[0]
    assert str(run.font.color.rgb) == "FFFFFF"


def test_works_without_a_configured_llm(settings: Settings, fake_downloader: FakeDownloader) -> None:
    # A deploy without OPENROUTER_API_KEY: the default arranger has no chain.
    app = create_app(settings, downloader=fake_downloader)  # type: ignore[arg-type]
    with TestClient(app) as client:
        r = client.post(URL, json={"html": HTML}, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["x-lyrics-arranged-by"] == "fallback"


def test_requires_api_key(client: TestClient, fake_chain: FakeChain) -> None:
    assert client.post(URL, json={"html": HTML}).status_code == 401
    assert client.post(URL, json={"html": HTML}, headers={"X-API-Key": "x"}).status_code == 401
    assert fake_chain.calls == []


def test_html_without_lyrics_is_422(client: TestClient, fake_chain: FakeChain) -> None:
    r = client.post(URL, json={"html": "<div>nothing here</div><script>x</script><p></p>"}, headers=AUTH)

    assert r.status_code == 422
    assert r.json() == {"detail": {"code": "no_lyrics", "message": "No lyrics found in the HTML"}}
    assert fake_chain.calls == []


def test_too_many_lines_is_422(client: TestClient) -> None:
    html = "<p>" + "<br>".join(["la"] * 301) + "</p>"
    r = client.post(URL, json={"html": html}, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "lyrics_too_long"


def test_request_validation(client: TestClient, fake_chain: FakeChain) -> None:
    bad_bodies: list[dict[str, Any]] = [
        {},
        {"html": ""},
        {"html": "x" * 500_001},
        {"html": HTML, "font_color": "red"},
        {"html": HTML, "font_color": "#FFF"},
        {"html": HTML, "title": "t" * 201},
    ]
    for body in bad_bodies:
        r = client.post(URL, json=body, headers=AUTH)
        assert r.status_code == 422, body
        assert isinstance(r.json()["detail"], list)  # FastAPI's standard validation errors
    assert fake_chain.calls == []


def test_is_documented_as_a_binary_download(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    ok = spec["paths"][URL]["post"]["responses"]["200"]
    assert PPTX_MEDIA_TYPE in ok["content"]
