import logging
from collections.abc import Callable

import pytest
from conftest import ORIGIN, FakeDownloader
from fastapi.testclient import TestClient

from cloudy_api import cli
from cloudy_api import main as main_module
from cloudy_api.config import Settings
from cloudy_api.main import check_dependencies, create_app
from cloudy_api.schemas import Engine
from cloudy_api.services.downloader import Downloader
from cloudy_api.services.pytubefix_downloader import PytubefixDownloader
from cloudy_api.services.youtube_api import YouTubeMetadata

ALL_TOOLS = {"ffmpeg", "ffprobe", "deno", "node"}


def patch_which(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> list[tuple]:
    """Fake shutil.which: `available` holds bare names or full paths."""
    calls: list[tuple] = []

    def which(cmd: str, path: str | None = None) -> str | None:
        calls.append((cmd, path))
        if path is not None:
            cmd = f"{path.rstrip('/')}/{cmd}"
        return cmd if cmd in available or cmd.rsplit("/", 1)[-1] in available else None

    monkeypatch.setattr(main_module.shutil, "which", which)
    return calls


# ------------------------------------------------------------------ healthz
def test_healthz_all_dependencies(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_which(monkeypatch, ALL_TOOLS)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "yt_dlp_version": main_module.YT_DLP_VERSION,
        "yt_dlp_ejs_version": main_module.ejs_version(),
        "ffmpeg": True,
        "js_runtime": True,
        "js_runtimes": {"deno": True, "node": True},
        "pytubefix_version": main_module.PYTUBEFIX_VERSION,
        "pytubefix_node": True,
        "youtube_api": False,
    }


def test_healthz_reports_youtube_api_without_changing_status(
    make_settings: Callable[..., Settings],
    fake_downloader: FakeDownloader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(make_settings(youtube_data_api_key="yt-key"), downloader=fake_downloader)  # type: ignore[arg-type]
    with TestClient(app) as client:
        patch_which(monkeypatch, ALL_TOOLS)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["youtube_api"] is True
        patch_which(monkeypatch, set())
        r = client.get("/healthz")
        assert r.status_code == 503
        assert r.json()["youtube_api"] is True


@pytest.mark.parametrize(
    ("available", "runtimes"),
    [
        ({"ffmpeg", "ffprobe", "node"}, {"deno": False, "node": True}),  # Deno gone
        ({"ffmpeg", "ffprobe", "deno"}, {"deno": True, "node": False}),
    ],
)
def test_healthz_ok_with_any_runtime(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    available: set[str],
    runtimes: dict[str, bool],
) -> None:
    patch_which(monkeypatch, available)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["js_runtime"] is True
    assert r.json()["js_runtimes"] == runtimes


@pytest.mark.parametrize(
    ("available", "ffmpeg", "js_runtime"),
    [
        (set(), False, False),
        ({"ffmpeg", "node"}, False, True),  # ffprobe missing
        ({"ffmpeg", "ffprobe"}, True, False),  # no JS runtime at all
    ],
)
def test_healthz_missing_dependencies(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    available: set[str],
    ffmpeg: bool,
    js_runtime: bool,
) -> None:
    patch_which(monkeypatch, available)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["ffmpeg"] is ffmpeg
    assert r.json()["js_runtime"] is js_runtime


def test_healthz_reports_configured_runtimes(
    make_settings: Callable[..., Settings],
    fake_downloader: FakeDownloader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_which(monkeypatch, {"ffmpeg", "ffprobe", "bun"})
    app = create_app(make_settings(ytdlp_js_runtimes="node,bun"), downloader=fake_downloader)  # type: ignore[arg-type]
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["js_runtimes"] == {"node": False, "bun": True}


def test_js_runtime_lookup_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_which(monkeypatch, {"qjs"})
    assert main_module.js_runtime_available("quickjs", None) is True
    assert calls == [("qjs", None)]  # quickjs's executable is `qjs`


def test_js_runtime_lookup_by_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_which(monkeypatch, {"/opt/node/bin/node"})
    assert main_module.js_runtime_available("node", "/opt/node/bin/node") is True
    assert main_module.js_runtime_available("node", "/missing/node") is False
    assert calls[0] == ("/opt/node/bin/node", None)


def test_js_runtime_lookup_by_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    calls = patch_which(monkeypatch, {f"{tmp_path}/node"})
    assert main_module.js_runtime_available("node", str(tmp_path)) is True
    assert calls == [("node", str(tmp_path))]


def test_healthz_needs_no_auth(client: TestClient) -> None:
    assert client.get("/healthz").status_code in (200, 503)


# --------------------------------------------------------------------- CORS
def test_cors_allows_configured_origin(client: TestClient) -> None:
    r = client.get("/healthz", headers={"Origin": ORIGIN})
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert "Content-Disposition" in r.headers["access-control-expose-headers"]


def test_cors_rejects_other_origins(client: TestClient) -> None:
    r = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight(client: TestClient) -> None:
    r = client.options(
        "/healthz",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ORIGIN


# --------------------------------------------------------------------- docs
def test_scalar_docs(client: TestClient) -> None:
    r = client.get("/docs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "@scalar/api-reference" in r.text
    assert "/openapi.json" in r.text


def test_swagger_and_redoc_are_disabled(client: TestClient) -> None:
    assert client.get("/redoc").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert "/docs" not in paths


def test_openapi_documents_api_key(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    assert set(spec["paths"]) == {
        "/healthz",
        "/v1/videos/{video_id}",
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/file",
        "/v1/lyrics/pptx",
        "/v1/lyrics/pptx/batch",
        "/v2/videos/{video_id}",
        "/v2/jobs",
        "/v2/jobs/{job_id}",
        "/v2/jobs/{job_id}/file",
    }
    for path in ("/v1/lyrics/pptx", "/v1/lyrics/pptx/batch", "/v1/jobs", "/v2/jobs"):
        assert spec["paths"][path]["post"]["security"] == [{"APIKeyHeader": []}]
    for version in ("v1", "v2"):
        assert spec["paths"][f"/{version}/videos/{{video_id}}"]["get"]["security"] == [
            {"APIKeyHeader": []}
        ]
        file_op = spec["paths"][f"/{version}/jobs/{{job_id}}/file"]["get"]
        assert "security" not in file_op  # signed URL, no API key
    operation_ids = [
        op["operationId"] for item in spec["paths"].values() for op in item.values()
    ]
    assert len(operation_ids) == len(set(operation_ids))


# ---------------------------------------------------------------- lifespan
def test_lifespan_starts_and_stops_runner(
    settings: Settings, fake_downloader: FakeDownloader
) -> None:
    app = create_app(settings, downloader=fake_downloader)  # type: ignore[arg-type]
    manager = app.state.job_manager
    assert manager._background == []
    with TestClient(app):
        assert settings.work_dir.is_dir()
        assert len(manager._background) == 2
        tasks = list(manager._background)
    assert manager._background == []
    assert all(task.done() for task in tasks)


def test_lifespan_logs_dependency_report(
    settings: Settings,
    fake_downloader: FakeDownloader,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(settings, downloader=fake_downloader)  # type: ignore[arg-type]
    monkeypatch.setattr(main_module.log, "propagate", True)  # let caplog see it

    patch_which(monkeypatch, set())
    with caplog.at_level(logging.INFO, logger="cloudy_api"), TestClient(app):
        pass
    assert any("startup.missing_dependencies" in r.message for r in caplog.records)

    caplog.clear()
    patch_which(monkeypatch, {"ffmpeg", "ffprobe", "node"})
    with caplog.at_level(logging.INFO, logger="cloudy_api"), TestClient(app):
        pass
    messages = [r.message for r in caplog.records]
    assert any(
        m.startswith("startup yt_dlp=") and "deno=False node=True" in m for m in messages
    )
    assert not any("missing_dependencies" in m for m in messages)


def test_create_app_defaults(
    monkeypatch: pytest.MonkeyPatch, make_settings: Callable[..., Settings]
) -> None:
    settings = make_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = create_app()
    assert app.state.settings is settings
    assert isinstance(app.state.downloader, Downloader)
    assert app.state.job_manager.downloader is app.state.downloader
    assert isinstance(app.state.pytubefix_downloader, PytubefixDownloader)
    assert app.state.job_manager.downloaders == {
        Engine.YTDLP: app.state.downloader,
        Engine.PYTUBEFIX: app.state.pytubefix_downloader,
    }
    assert isinstance(app.state.metadata, YouTubeMetadata)
    assert app.state.metadata.enabled is False


def test_check_dependencies_reports_version(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    patch_which(monkeypatch, {"ffmpeg", "ffprobe", "node"})
    assert check_dependencies(settings).model_dump() == {
        "status": "ok",
        "yt_dlp_version": main_module.YT_DLP_VERSION,
        "yt_dlp_ejs_version": main_module.ejs_version(),
        "ffmpeg": True,
        "js_runtime": True,
        "js_runtimes": {"deno": False, "node": True},
        "pytubefix_version": main_module.PYTUBEFIX_VERSION,
        "pytubefix_node": True,
        "youtube_api": False,
    }


def test_ejs_version_is_reported() -> None:
    from importlib.metadata import version

    assert main_module.ejs_version() == version("yt-dlp-ejs")


def test_missing_ejs_degrades_health(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    patch_which(monkeypatch, ALL_TOOLS)
    monkeypatch.setattr(main_module, "package_version", missing)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["yt_dlp_ejs_version"] is None


def test_logging_is_configured_once() -> None:
    handlers = list(main_module.log.handlers)
    main_module._configure_logging()
    assert main_module.log.handlers == handlers


def test_logging_setup_adds_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.log, "handlers", [])
    monkeypatch.setattr(main_module.log, "propagate", True)  # let caplog see it
    main_module._configure_logging()
    assert len(main_module.log.handlers) == 1
    assert main_module.log.propagate is False


# ------------------------------------------------------------ entry point
def test_main_runs_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.delenv("HOST", raising=False)

    cli.main()

    assert calls == [
        (("cloudy_api.main:app",), {"host": "0.0.0.0", "port": 9123, "workers": 1})
    ]
