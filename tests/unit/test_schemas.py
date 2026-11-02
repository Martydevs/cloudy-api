import pytest
from pydantic import ValidationError

from cloudy_api.schemas import Format, JobCreate


@pytest.mark.parametrize("video_id", ["dQw4w9WgXcQ", "a_b-C0123zZ", "___________"])
def test_job_create_accepts_valid_ids(video_id: str) -> None:
    job = JobCreate(video_ids=[video_id], format=Format.MP3)
    assert job.video_ids == [video_id]


@pytest.mark.parametrize(
    "video_id",
    [
        "short",
        "dQw4w9WgXcQx",  # 12 chars
        "dQw4w9WgXc!",
        "https://www",
        "dQw4w9 WgXc",
        "",
    ],
)
def test_job_create_rejects_invalid_ids(video_id: str) -> None:
    with pytest.raises(ValidationError):
        JobCreate(video_ids=[video_id], format=Format.MP3)


def test_job_create_requires_at_least_one_id() -> None:
    with pytest.raises(ValidationError):
        JobCreate(video_ids=[], format=Format.MP3)


def test_job_create_dedupes_preserving_order() -> None:
    job = JobCreate(
        video_ids=["bbbbbbbbbbb", "aaaaaaaaaaa", "bbbbbbbbbbb", "aaaaaaaaaaa"],
        format="mp4_720",
    )
    assert job.video_ids == ["bbbbbbbbbbb", "aaaaaaaaaaa"]
    assert job.format is Format.MP4_720


def test_job_create_rejects_unknown_format() -> None:
    with pytest.raises(ValidationError):
        JobCreate(video_ids=["dQw4w9WgXcQ"], format="avi")


@pytest.mark.parametrize(
    ("fmt", "ext", "height"),
    [(Format.MP4_720, "mp4", 720), (Format.MP4_1080, "mp4", 1080), (Format.MP3, "mp3", None)],
)
def test_format_properties(fmt: Format, ext: str, height: int | None) -> None:
    assert fmt.ext == ext
    assert fmt.max_height == height
