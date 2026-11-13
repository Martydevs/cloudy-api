"""Engine-neutral pieces of services.downloader (the engine has its own tests)."""

import pytest
from conftest import video_info

from cloudy_api.services import downloader as dl
from cloudy_api.services.downloader import (
    DownloaderError,
    ProbeCache,
    not_downloadable_reason,
    watch_url,
)

VIDEO_ID = "dQw4w9WgXcQ"


def test_watch_url() -> None:
    assert watch_url(VIDEO_ID) == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_downloader_error_defaults_to_client_error() -> None:
    error = DownloaderError("private", "Private video")
    assert (error.code, error.message, error.client_error) == ("private", "Private video", True)
    assert str(error) == "Private video"
    assert DownloaderError("x", "y", client_error=False).client_error is False


# --------------------------------------------------------------- probe cache
def test_probe_cache_hits_until_ttl() -> None:
    clock = [0.0]
    cache = ProbeCache(lambda: clock[0])
    info = video_info(VIDEO_ID)
    assert cache.get(VIDEO_ID) is None

    cache.put(info)
    clock[0] = dl.PROBE_CACHE_TTL_SECONDS - 1
    assert cache.get(VIDEO_ID) is info

    clock[0] = dl.PROBE_CACHE_TTL_SECONDS + 1
    assert cache.get(VIDEO_ID) is None


def test_probe_cache_evicts_every_expired_entry() -> None:
    clock = [0.0]
    cache = ProbeCache(lambda: clock[0], ttl=10)
    cache.put(video_info("aaaaaaaaaaa"))
    clock[0] = 5
    cache.put(video_info("bbbbbbbbbbb"))
    clock[0] = 12
    # Looking up anything sweeps the stale "a" entry; "b" is still fresh.
    assert cache.get("ccccccccccc") is None
    assert set(cache._entries) == {"bbbbbbbbbbb"}
    assert cache.get("bbbbbbbbbbb") is not None


# --------------------------------------------------------- downloadable rules
@pytest.mark.parametrize(
    ("live", "duration", "reason"),
    [
        (False, 1800, None),
        (False, 1801, "too_long"),
        (False, None, "unknown_duration"),
        (True, 60, "live"),
        (True, None, "live"),
    ],
)
def test_not_downloadable_reason(live: bool, duration: int | None, reason: str | None) -> None:
    assert not_downloadable_reason(live=live, duration=duration, max_duration=1800) == reason


# ------------------------------------------------------------ error messages
def test_clean_message_strips_prefixes() -> None:
    assert dl._clean_message("ERROR: [youtube] x: Private video. Sign in") == "Private video. Sign in"
    assert dl._clean_message("ERROR: plain failure") == "plain failure"
    assert dl._clean_message("[weird no colon") == "[weird no colon"


def test_clean_message_is_trimmed() -> None:
    assert len(dl._clean_message("x" * 1000)) == 300
