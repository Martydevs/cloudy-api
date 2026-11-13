from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from pytubefix.innertube import _default_clients as PYTUBEFIX_CLIENTS

from .schemas import MAX_LYRICS_BATCH_ITEMS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: SecretStr
    signing_secret: SecretStr
    allowed_origins: Annotated[list[str], NoDecode] = []

    max_concurrent_jobs: int = Field(default=1, ge=1)
    max_queue: int = Field(default=20, ge=1)
    max_batch: int = Field(default=10, ge=1)
    # Songs per POST /api/lyrics/pptx/batch request (processed one at a time).
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
    # Parallel metadata probes during job creation without a YouTube Data API key
    # (each runs a pytubefix extraction, plus Node for the PO token).
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

    # YouTube Data API v3 key for video metadata (GET /videos, job preflight).
    # Unset → the download engine (pytubefix) probes metadata itself.
    youtube_data_api_key: SecretStr | None = None

    # pytubefix YouTube client. MWEB was the only one that passed the bot check
    # and downloaded everything from Railway (2026-11-09); WEB_KIDS and IOS_KIDS
    # pass too but only see YouTube Kids videos.
    pytubefix_client: str = "MWEB"
    # Proxy for YouTube downloads. pytubefix installs it as the process-wide
    # urllib proxy (the YouTube Data API client uses httplib2, not that opener).
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
        "pytubefix_proxy", "openrouter_api_key", "youtube_data_api_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
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
