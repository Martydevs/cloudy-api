from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, StringConstraints

VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
HEX_COLOR_PATTERN = r"^#?[0-9A-Fa-f]{6}$"
MAX_LYRICS_HTML_CHARS = 500_000

VideoId = Annotated[str, StringConstraints(pattern=VIDEO_ID_PATTERN)]


class Format(StrEnum):
    MP4_720 = "mp4_720"
    MP4_1080 = "mp4_1080"
    MP3 = "mp3"

    @property
    def ext(self) -> str:
        return "mp3" if self is Format.MP3 else "mp4"

    @property
    def max_height(self) -> int | None:
        return {Format.MP4_720: 720, Format.MP4_1080: 1080}.get(self)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    ZIPPING = "zipping"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


class ItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _dedupe(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


class JobCreate(BaseModel):
    # The upper bound comes from MAX_BATCH and is enforced in the route.
    video_ids: Annotated[
        list[VideoId], Field(min_length=1), AfterValidator(_dedupe)
    ]
    format: Format


class ErrorInfo(BaseModel):
    code: str
    message: str


class VideoInfo(BaseModel):
    video_id: str
    title: str
    duration: int | None
    thumbnail: str | None
    channel: str | None
    available_heights: list[int]
    downloadable: bool
    reason: str | None = None


class ItemOut(BaseModel):
    video_id: str
    title: str | None
    status: ItemStatus
    progress: float
    error: ErrorInfo | None
    actual_height: int | None


class JobCreated(BaseModel):
    job_id: str
    status: JobStatus


class JobOut(BaseModel):
    job_id: str
    status: JobStatus
    format: Format
    progress: float
    items: list[ItemOut]
    download_url: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    expires_at: float | None = None


class LyricsRequest(BaseModel):
    # Lyrics HTML the frontend scraped from Letras: the lyrics container is
    # enough, but a whole page works too.
    html: Annotated[str, Field(min_length=1, max_length=MAX_LYRICS_HTML_CHARS)]
    # Overrides the song title found in the HTML (used for the file name).
    title: Annotated[str, Field(max_length=200)] | None = None
    # Text color as RRGGBB hex, with or without "#".
    font_color: Annotated[str, StringConstraints(pattern=HEX_COLOR_PATTERN)] = "000000"
