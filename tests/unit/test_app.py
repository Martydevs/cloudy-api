import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import ORIGIN, FakeDownloader
from fastapi.testclient import TestClient

from cloudy_api import cli
from cloudy_api import main as main_module
from cloudy_api.config import Settings
from cloudy_api.main import check_dependencies, create_app
from cloudy_api.services.pytubefix_downloader import PytubefixDownloader
from cloudy_api.services.youtube_api import YouTubeMetadata

FFMPEG_TOOLS = {"ffmpeg", "ffprobe"}


def patch_which(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> None:
    """Fake shutil.which: only the bare names in `available` are on PATH."""
    monkeypatch.setattr(
        main_module.shutil, "which", lambda cmd: cmd if cmd in available else None
    )


@pytest.fixture
def node_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the pytubefix bundled-Node check at a file that exists."""
    node = tmp_path / "node"
    node.write_bytes(b"")
    monkeypatch.setattr(main_module, "PYTUBEFIX_NODE_PATH", str(node))
    return node


@pytest.fixture
def node_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    node = tmp_path / "missing" / "node"
    monkeypatch.setattr(main_module, "PYTUBEFIX_NODE_PATH", str(node))
    return node


# ------------------------------------------------------------------ healthz
@pytest.mark.usefixtures("node_present")
def test_healthz_all_dependencies(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_which(monkeypatch, FFMPEG_TOOLS)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "ffmpeg": True,
        "pytubefix_version": main_module.PYTUBEFIX_VERSION,
        "pytubefix_node": True,
        "youtube_api": False,
    }


@pytest.mark.usefixtures("node_present")
def test_healthz_reports_youtube_api_without_changing_status(
    make_settings: Callable[..., Settings],
    fake_downloader: FakeDownloader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(make_settings(youtube_data_api_key="yt-key"), downloader=fake_downloader)  # type: ignore[arg-type]
    with TestClient(app) as client:
        patch_which(monkeypatch, FFMPEG_TOOLS)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["youtube_api"] is True
        patch_which(monkeypatch, set())
        r = client.get("/healthz")
        assert r.status_code == 503
        assert r.json()["youtube_api"] is True


@pytest.mark.usefixtures("node_missing")
def test_healthz_missing_bundled_node_is_informational(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_which(monkeypatch, FFMPEG_TOOLS)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["pytubefix_node"] is False


@pytest.mark.usefixtures("node_present")
@pytest.mark.parametrize(
    "available",
    [set(), {"ffmpeg"}, {"ffprobe"}],  # ffmpeg and ffprobe are both required
)
def test_healthz_missing_ffmpeg(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, available: set[str]
) -> None:
    patch_which(monkeypatch, available)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["ffmpeg"] is False
    assert r.json()["pytubefix_node"] is True


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
        "/api/videos/{video_id}",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/file",
        "/api/lyrics/pptx",
        "/api/lyrics/pptx/batch",
    }
    for path in ("/api/lyrics/pptx", "/api/lyrics/pptx/batch", "/api/jobs"):
        assert spec["paths"][path]["post"]["security"] == [{"APIKeyHeader": []}]
    for path in ("/api/videos/{video_id}", "/api/jobs/{job_id}"):
        assert spec["paths"][path]["get"]["security"] == [{"APIKeyHeader": []}]
    file_op = spec["paths"]["/api/jobs/{job_id}/file"]["get"]
    assert "security" not in file_op  # signed URL, no API key
    operation_ids = [
        op["operationId"] for item in spec["paths"].values() for op in item.values()
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_openapi_describes_operations_for_the_docs(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert [tag["name"] for tag in spec["tags"]] == ["videos", "jobs", "files", "lyrics", "health"]
    assert "X-API-Key" in spec["info"]["description"]
    operations = {
        op["operationId"]: op for item in spec["paths"].values() for op in item.values()
    }
    assert set(operations) == {
        "healthz",
        "get_video",
        "create_job",
        "get_job",
        "delete_job",
        "download_file",
        "create_lyrics_pptx",
        "create_lyrics_pptx_batch",
    }
    for op in operations.values():
        assert op["summary"] and op["description"]
    error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
    for name in ("get_video", "create_job", "get_job", "create_lyrics_pptx"):
        assert operations[name]["responses"]["401"]["content"]["application/json"]["schema"] == error_ref
    assert "401" not in operations["download_file"]["responses"]
    assert set(operations["download_file"]["responses"]) >= {"200", "403", "404", "410"}
    assert operations["create_job"]["responses"]["422"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/VideosRejectedResponse"}
    assert set(operations["create_job"]["responses"]) >= {"202", "429", "502"}


def test_scalar_docs_preselect_the_api_key(client: TestClient) -> None:
    body = client.get("/docs").text
    assert "APIKeyHeader" in body


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


@pytest.mark.usefixtures("node_present")
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
    patch_which(monkeypatch, FFMPEG_TOOLS)
    with caplog.at_level(logging.INFO, logger="cloudy_api"), TestClient(app):
        pass
    messages = [r.message for r in caplog.records]
    assert (
        f"startup pytubefix={main_module.PYTUBEFIX_VERSION} ffmpeg=True youtube_api=False"
        in messages
    )
    assert not any("missing_dependencies" in m or "node_missing" in m for m in messages)


def test_lifespan_warns_without_bundled_node(
    settings: Settings,
    fake_downloader: FakeDownloader,
    node_missing: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(settings, downloader=fake_downloader)  # type: ignore[arg-type]
    monkeypatch.setattr(main_module.log, "propagate", True)  # let caplog see it
    patch_which(monkeypatch, FFMPEG_TOOLS)
    with caplog.at_level(logging.INFO, logger="cloudy_api"), TestClient(app):
        pass
    messages = [r.message for r in caplog.records]
    assert f"startup.pytubefix_node_missing path={node_missing}" in messages
    assert not any("missing_dependencies" in m for m in messages)


def test_create_app_defaults(
    monkeypatch: pytest.MonkeyPatch, make_settings: Callable[..., Settings]
) -> None:
    settings = make_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = create_app()
    assert app.state.settings is settings
    assert isinstance(app.state.downloader, PytubefixDownloader)
    assert app.state.job_manager.downloader is app.state.downloader
    assert not hasattr(app.state, "pytubefix_downloader")
    assert isinstance(app.state.metadata, YouTubeMetadata)
    assert app.state.metadata.enabled is False


@pytest.mark.usefixtures("node_present")
def test_check_dependencies_reports_version(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    patch_which(monkeypatch, FFMPEG_TOOLS)
    assert check_dependencies(settings).model_dump() == {
        "status": "ok",
        "ffmpeg": True,
        "pytubefix_version": main_module.PYTUBEFIX_VERSION,
        "pytubefix_node": True,
        "youtube_api": False,
    }


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


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/videos/dQw4w9WgXcQ"),
        ("post", "/v1/jobs"),
        ("get", "/v1/jobs/abc/file"),
        ("post", "/v1/lyrics/pptx"),
        ("get", "/v2/videos/dQw4w9WgXcQ"),
        ("post", "/v2/jobs"),
    ],
)
def test_versioned_routes_are_gone(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, headers={"X-API-Key": "test-api-key"}).status_code == 404
