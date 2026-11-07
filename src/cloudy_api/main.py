import logging
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from scalar_fastapi import get_scalar_api_reference
from yt_dlp.version import __version__ as YT_DLP_VERSION

from .config import JS_RUNTIME_EXECUTABLES, Settings, get_settings
from .middleware import BodySizeLimitMiddleware
from .routers import jobs, lyrics, videos
from .services.downloader import Downloader
from .services.jobs import JobManager
from .services.lyrics_arranger import LyricsArranger

log = logging.getLogger("cloudy_api")


class Health(BaseModel):
    status: str
    yt_dlp_version: str
    # yt-dlp-ejs: challenge-solver scripts the JS runtime executes. Required for
    # full YouTube support; must be the exact version yt-dlp pins (uv.lock).
    yt_dlp_ejs_version: str | None
    ffmpeg: bool
    # True when at least one enabled JS runtime is installed.
    js_runtime: bool
    # Every enabled runtime (YTDLP_JS_RUNTIMES) and whether it was found.
    js_runtimes: dict[str, bool]


def js_runtime_available(name: str, path: str | None) -> bool:
    """Mirror yt-dlp's lookup: PATH by default, or an explicit file/directory."""
    executable = JS_RUNTIME_EXECUTABLES[name]
    if path is None:
        return shutil.which(executable) is not None
    if os.path.isdir(path):
        return shutil.which(executable, path=path) is not None
    return shutil.which(path) is not None


def ejs_version() -> str | None:
    try:
        return package_version("yt-dlp-ejs")
    except PackageNotFoundError:
        return None


def check_dependencies(settings: Settings) -> Health:
    ejs = ejs_version()
    ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    runtimes = {
        name: js_runtime_available(name, path)
        for name, path in settings.ytdlp_js_runtimes.items()
    }
    js_runtime = any(runtimes.values())
    return Health(
        status="ok" if ffmpeg and js_runtime and ejs else "degraded",
        yt_dlp_version=YT_DLP_VERSION,
        yt_dlp_ejs_version=ejs,
        ffmpeg=ffmpeg,
        js_runtime=js_runtime,
        js_runtimes=runtimes,
    )


def _configure_logging() -> None:
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    health = check_dependencies(app.state.settings)
    runtimes = " ".join(f"{name}={found}" for name, found in health.js_runtimes.items())
    log.info(
        "startup yt_dlp=%s yt_dlp_ejs=%s ffmpeg=%s %s",
        health.yt_dlp_version, health.yt_dlp_ejs_version, health.ffmpeg, runtimes,
    )
    if health.status != "ok":
        log.warning(
            "startup.missing_dependencies ffmpeg=%s js_runtime=%s yt_dlp_ejs=%s",
            health.ffmpeg, health.js_runtime, health.yt_dlp_ejs_version,
        )

    manager: JobManager = app.state.job_manager
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


def create_app(
    settings: Settings | None = None,
    downloader: Downloader | None = None,
    arranger: LyricsArranger | None = None,
) -> FastAPI:
    _configure_logging()
    settings = settings or get_settings()

    # Scalar replaces Swagger UI / ReDoc as the interactive docs at /docs.
    app = FastAPI(
        title="Cloudy API",
        version="0.1.0",
        description=(
            "On-demand YouTube downloads (MP4 720p/1080p, MP3) and "
            "lyrics-to-PowerPoint conversion for the Cloudy app."
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.downloader = downloader or Downloader(settings)
    app.state.job_manager = JobManager(settings, app.state.downloader)
    app.state.arranger = arranger or LyricsArranger(settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET"],
        allow_headers=[],
        expose_headers=["Content-Disposition", "Content-Length"],
    )
    # Added last so it runs first: oversized bodies are refused before anything else.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> JSONResponse:
        health = check_dependencies(settings)
        code = 200 if health.status == "ok" else 503
        return JSONResponse(health.model_dump(), status_code=code)

    @app.get("/docs", include_in_schema=False)
    async def scalar_docs() -> HTMLResponse:
        return get_scalar_api_reference(
            openapi_url=app.openapi_url,
            title=f"{app.title} docs",
            telemetry=False,
        )

    app.include_router(videos.router)
    app.include_router(jobs.router)
    app.include_router(jobs.files_router)
    app.include_router(lyrics.router)
    return app


app = create_app()
