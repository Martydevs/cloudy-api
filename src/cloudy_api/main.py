import logging
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pytubefix.botGuard.bot_guard import NODE_PATH as PYTUBEFIX_NODE_PATH
from pytubefix.version import __version__ as PYTUBEFIX_VERSION
from scalar_fastapi import get_scalar_api_reference

from .config import Settings, get_settings
from .middleware import BodySizeLimitMiddleware
from .routers import jobs, lyrics, videos
from .services.downloader import VideoDownloader
from .services.jobs import JobManager
from .services.lyrics_arranger import LyricsArranger
from .services.pytubefix_downloader import PytubefixDownloader
from .services.youtube_api import YouTubeMetadata

log = logging.getLogger("cloudy_api")

# Every API route lives under this prefix; /healthz and the docs stay at the root.
API_PREFIX = "/api"

API_DESCRIPTION = """\
On-demand YouTube downloads (MP4 720p/1080p, MP3) and lyrics-to-PowerPoint
conversion for the Cloudy app.

## Authentication

Every `/api` route needs the `X-API-Key` header, except
`GET /api/jobs/{job_id}/file`, which is authorized by the signature in its
query string. Keep the key on the server (a Nuxt server route), never in the
browser.

## Downloading a video

1. Optional: `GET /api/videos/{video_id}` to show the title and check `downloadable`.
2. `POST /api/jobs` with `video_ids` and a `format` → `202 {job_id}`.
3. Poll `GET /api/jobs/{job_id}` every 2–3 s until `status` is `done` or `failed`.
4. Send the browser to `download_url`. It expires after ~10 minutes; poll again
   for a fresh one. The file itself is deleted after `FILE_TTL_MINUTES` (30).

Jobs live in memory: a `404` on a known job means it expired or the service
restarted; create it again.

## Errors

Errors are `{"detail": {"code": "...", "message": "..."}}`; branch on `code`.
Malformed requests get FastAPI's standard `422` with a `detail` list instead.
"""

OPENAPI_TAGS = [
    {"name": "videos", "description": "YouTube video metadata."},
    {
        "name": "jobs",
        "description": "Asynchronous download jobs: create, poll, cancel.",
    },
    {
        "name": "files",
        "description": "Signed download links for finished jobs (no API key).",
    },
    {
        "name": "lyrics",
        "description": "Lyrics HTML (Letras) to PowerPoint, synchronous.",
    },
    {"name": "health", "description": "Readiness probe (no API key)."},
]


def _operation_id(route: APIRoute) -> str:
    # Stable, readable ids (`create_job`, `get_video`, …) for client generators.
    return route.name


class Health(BaseModel):
    # "ok" iff ffmpeg and ffprobe are on PATH (pytubefix merges streams and
    # converts MP3 through them); otherwise "degraded" and /healthz returns 503.
    status: str
    ffmpeg: bool
    pytubefix_version: str
    # The Node.js binary pytubefix bundles to generate PO tokens.
    # Informational only: it doesn't change status/503.
    pytubefix_node: bool
    # YOUTUBE_DATA_API_KEY is set (metadata via the Data API, not the engine).
    # Informational only.
    youtube_api: bool


def check_dependencies(settings: Settings) -> Health:
    ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    return Health(
        status="ok" if ffmpeg else "degraded",
        ffmpeg=ffmpeg,
        pytubefix_version=PYTUBEFIX_VERSION,
        pytubefix_node=os.path.isfile(PYTUBEFIX_NODE_PATH),
        youtube_api=settings.youtube_data_api_key is not None,
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
    log.info(
        "startup pytubefix=%s ffmpeg=%s youtube_api=%s",
        health.pytubefix_version, health.ffmpeg, health.youtube_api,
    )
    if health.status != "ok":
        log.warning("startup.missing_dependencies ffmpeg=%s", health.ffmpeg)
    if not health.pytubefix_node:
        log.warning("startup.pytubefix_node_missing path=%s", PYTUBEFIX_NODE_PATH)

    manager: JobManager = app.state.job_manager
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


def create_app(
    settings: Settings | None = None,
    downloader: VideoDownloader | None = None,
    arranger: LyricsArranger | None = None,
    metadata: YouTubeMetadata | None = None,
) -> FastAPI:
    _configure_logging()
    settings = settings or get_settings()

    # Scalar replaces Swagger UI / ReDoc as the interactive docs at /docs.
    app = FastAPI(
        title="Cloudy API",
        version="0.2.0",
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        generate_unique_id_function=_operation_id,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.downloader = downloader or PytubefixDownloader(settings)
    # Video metadata: YouTube Data API, with the engine's probe as fallback.
    app.state.metadata = metadata or YouTubeMetadata(settings)
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

    @app.get(
        "/healthz",
        tags=["health"],
        summary="Health check",
        response_model=Health,
        responses={503: {"model": Health, "description": "ffmpeg or ffprobe is missing."}},
    )
    async def healthz() -> JSONResponse:
        """`200` when ffmpeg and ffprobe are installed, else `503` with `status:
        "degraded"`. `pytubefix_node` and `youtube_api` are informational. Also
        handy to wake the service before a user starts a download.
        """
        health = check_dependencies(settings)
        code = 200 if health.status == "ok" else 503
        return JSONResponse(health.model_dump(), status_code=code)

    @app.get("/docs", include_in_schema=False)
    async def scalar_docs() -> HTMLResponse:
        return get_scalar_api_reference(
            openapi_url=app.openapi_url,
            title=f"{app.title} docs",
            # Pre-selects the X-API-Key scheme in "Test Request"; the key itself
            # is typed in the browser and never stored.
            authentication={"preferredSecurityScheme": "APIKeyHeader"},
            default_open_all_tags=True,
            telemetry=False,
        )

    for router in (videos.router, jobs.router, jobs.files_router, lyrics.router):
        app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()
