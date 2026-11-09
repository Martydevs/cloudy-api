import optparse
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yt_dlp
from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pytubefix.innertube import _default_clients as PYTUBEFIX_CLIENTS

from .schemas import MAX_LYRICS_BATCH_ITEMS

# JavaScript runtimes yt-dlp can use to solve YouTube's JS challenges, mapped to
# the executable it looks for. When several are enabled and installed, yt-dlp
# picks by its own fixed preference: deno > node > quickjs > bun.
JS_RUNTIME_EXECUTABLES: dict[str, str] = {
    "deno": "deno",
    "node": "node",
    "bun": "bun",  # deprecated by yt-dlp (supported up to 1.3.14)
    "quickjs": "qjs",
}

ExtractorArgs = dict[str, dict[str, list[str]]]


def parse_extractor_args(value: str) -> ExtractorArgs:
    """Parse ``IE_KEY:ARGS`` entries exactly like yt-dlp's ``--extractor-args``.

    Entries are whitespace-separated (shell quoting allowed), e.g.
    ``youtube:player_client=default,mweb;po_token=mweb.gvs+TOKEN``.
    """
    argv = ["--ignore-config"]
    for entry in shlex.split(value):
        argv += ["--extractor-args", entry]
    try:
        parsed = yt_dlp.parse_options(argv)
    except optparse.OptParseError as exc:
        message = str(exc).strip().splitlines()[-1]
        raise ValueError(message.split("error: ", 1)[-1]) from None
    return parsed.ydl_opts["extractor_args"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: SecretStr
    signing_secret: SecretStr
    allowed_origins: Annotated[list[str], NoDecode] = []

    max_concurrent_jobs: int = Field(default=1, ge=1)
    max_queue: int = Field(default=20, ge=1)
    max_batch: int = Field(default=10, ge=1)
    # Songs per POST /v1/lyrics/pptx/batch request (processed one at a time).
    max_lyrics_batch: int = Field(default=3, ge=1, le=MAX_LYRICS_BATCH_ITEMS)
    # Time budget for the LLM across one lyrics batch, counted from the start of
    # the request. Once spent, the remaining songs use the fallback grouping, so
    # the response returns before the caller (Vercel function / proxy) times out.
    lyrics_batch_deadline_seconds: float = Field(default=45, gt=0)
    # Largest request body accepted, in bytes (413 above it). The default fits a
    # full lyrics batch of MAX_LYRICS_BATCH=3 large pages; raise it with the batch.
    max_request_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    max_duration_seconds: int = Field(default=1800, ge=1)
    file_ttl_minutes: int = Field(default=30, ge=1)
    signed_url_ttl_seconds: int = Field(default=600, ge=1)
    cleanup_interval_seconds: int = Field(default=300, ge=1)
    # Parallel metadata probes during job creation (each runs yt-dlp + a JS runtime).
    probe_concurrency: int = Field(default=3, ge=1)

    work_dir: Path = Path("/tmp/cloudy")
    # Public base URL used to build signed download links. Render sets
    # RENDER_EXTERNAL_URL automatically (e.g. https://cloudy-api.onrender.com).
    public_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"),
    )

    # Lyrics arrangement LLM, reached through OpenRouter (OpenAI-compatible API).
    # Without a key the lyrics route still works, using the deterministic
    # fallback grouping instead of the LLM.
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("openrouter_api_key", "openrouter_apikey"),
    )
    # An OpenRouter preset that holds the model and the system prompt.
    openrouter_model: str = "@preset/lyrics-generator"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_seconds: float = Field(default=30, gt=0)

    ytdlp_cookies_file: Path | None = None
    ytdlp_proxy: SecretStr | None = None
    # Comma-separated `runtime[:path]` entries, like yt-dlp's --js-runtimes.
    # Node is the fallback for when Deno is unavailable or retired.
    ytdlp_js_runtimes: Annotated[dict[str, str | None], NoDecode] = {
        "deno": None,
        "node": None,
    }
    # yt-dlp --extractor-args, e.g. `youtube:player_client=default,mweb`. The
    # documented levers for YouTube bot checks (player clients, PO tokens).
    ytdlp_extractor_args: Annotated[ExtractorArgs, NoDecode] = {}

    # /v2 (pytubefix) YouTube client. MWEB was the only one that passed the bot
    # check and downloaded everything from Railway (2026-11-09); WEB_KIDS and
    # IOS_KIDS pass too but only see YouTube Kids videos.
    pytubefix_client: str = "MWEB"
    # Applies to pytubefix only. pytubefix installs it as the process-wide
    # urllib proxy (yt-dlp and httpx don't use that opener).
    pytubefix_proxy: SecretStr | None = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [o.strip().rstrip("/") for o in value.split(",") if o.strip()]
        return value

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _strip_base_url(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().rstrip("/") or None
        return value

    @field_validator(
        "ytdlp_cookies_file", "ytdlp_proxy", "pytubefix_proxy", "openrouter_api_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ytdlp_js_runtimes", mode="before")
    @classmethod
    def _parse_js_runtimes(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        runtimes: dict[str, str | None] = {}
        for entry in value.split(","):
            name, _, path = entry.strip().partition(":")
            if name.strip():
                runtimes[name.strip().lower()] = path.strip() or None
        return runtimes

    @field_validator("ytdlp_js_runtimes")
    @classmethod
    def _check_js_runtimes(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        if not value:
            raise ValueError("enable at least one JS runtime")
        if unknown := sorted(value.keys() - JS_RUNTIME_EXECUTABLES.keys()):
            raise ValueError(
                f"unsupported JS runtime(s): {', '.join(unknown)}; "
                f"supported: {', '.join(JS_RUNTIME_EXECUTABLES)}"
            )
        return value

    @field_validator("ytdlp_extractor_args", mode="before")
    @classmethod
    def _parse_extractor_args(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_extractor_args(value)
        return value

    @field_validator("pytubefix_client", mode="before")
    @classmethod
    def _check_pytubefix_client(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        client = value.strip().upper()
        if client not in PYTUBEFIX_CLIENTS:
            raise ValueError(
                f"unsupported pytubefix client: {value!r}; "
                f"supported: {', '.join(PYTUBEFIX_CLIENTS)}"
            )
        return client

    @property
    def file_ttl_seconds(self) -> int:
        return self.file_ttl_minutes * 60


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required values come from env
