import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError

from cloudy_api.config import Settings
from cloudy_api.schemas import Format
from cloudy_api.services import downloader as dl
from cloudy_api.services.downloader import (
    Cancelled,
    Downloader,
    DownloaderError,
    available_heights,
    map_ytdlp_error,
    watch_url,
)

VIDEO_ID = "dQw4w9WgXcQ"

FORMATS = [
    {"format_id": "sb0", "vcodec": "none", "height": 90},  # storyboard
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "height": None},
    {"format_id": "18", "vcodec": "avc1.42001E", "height": 360},
    {"format_id": "136", "vcodec": "avc1.4d401f", "height": 720},
    {"format_id": "137", "vcodec": "avc1.640028", "height": 1080},
    {"format_id": "248", "vcodec": "vp9", "height": 1080},
    {"format_id": "x", "height": 480},  # vcodec missing
]


def base_info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": VIDEO_ID,
        "title": "Never Gonna Give You Up",
        "duration": 213,
        "thumbnail": "https://i.ytimg.com/vi/x/max.jpg",
        "channel": "Rick Astley",
        "live_status": "not_live",
        "formats": FORMATS,
    }
    info.update(overrides)
    return info


class FakeYoutubeDL:
    """Records options and simulates yt-dlp without network or ffmpeg."""

    instances: list["FakeYoutubeDL"] = []
    info: dict[str, Any] = {}
    error: BaseException | None = None
    # Hook events emitted during a download; the final file is written after.
    hook_events: list[dict[str, Any]] = []
    write_ext: str | None = None
    report_filepath: bool = True

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.calls: list[tuple[str, bool]] = []
        self.closed = False
        type(self).instances.append(self)

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        self.calls.append((url, download))
        if self.error is not None:
            raise self.error
        info = dict(self.info)
        if not download:
            return info
        for event in self.hook_events:
            for hook in self.opts.get("progress_hooks", []):
                hook(event)
        if self.write_ext:
            template = self.opts["outtmpl"]["default"]
            path = Path(template.replace("%(id)s", info["id"]).replace("%(ext)s", self.write_ext))
            path.write_bytes(b"media")
            if self.report_filepath:
                info["requested_downloads"] = [{"filepath": str(path)}]
        return info


@pytest.fixture
def fake_ydl() -> type[FakeYoutubeDL]:
    class Fake(FakeYoutubeDL):
        instances = []
        info = base_info()
        error = None
        hook_events = []
        write_ext = None
        report_filepath = True

    return Fake


@pytest.fixture
def make_downloader(
    make_settings: Callable[..., Settings], fake_ydl: type[FakeYoutubeDL]
) -> Callable[..., Downloader]:
    def factory(clock: Callable[[], float] = lambda: 0.0, **settings: Any) -> Downloader:
        return Downloader(make_settings(**settings), ydl_factory=fake_ydl, now=clock)

    return factory


# --------------------------------------------------------------- build_opts
def test_default_factory_is_real_youtubedl(settings: Settings) -> None:
    assert Downloader(settings)._ydl_factory is yt_dlp.YoutubeDL


def test_build_opts_base(make_downloader: Callable[..., Downloader]) -> None:
    opts = make_downloader().build_opts()
    assert opts["noplaylist"] is True
    assert opts["js_runtimes"] == {"deno": {}, "node": {}}
    assert "format" not in opts
    assert "cookiefile" not in opts
    assert "proxy" not in opts
    assert "extractor_args" not in opts


@pytest.mark.parametrize(("fmt", "height"), [(Format.MP4_720, 720), (Format.MP4_1080, 1080)])
def test_build_opts_mp4(make_downloader: Callable[..., Downloader], fmt: Format, height: int) -> None:
    opts = make_downloader().build_opts(fmt)
    assert opts["format"] == (
        f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]"
        f"/bv*[height<={height}]+ba/b[height<={height}]"
    )
    assert opts["merge_output_format"] == "mp4"
    assert opts["postprocessors"] == [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]


def test_build_opts_mp3(make_downloader: Callable[..., Downloader]) -> None:
    opts = make_downloader().build_opts(Format.MP3)
    assert opts["format"] == "ba/b"
    assert "merge_output_format" not in opts
    assert opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
    ]


def test_build_opts_js_runtimes_with_paths(make_downloader: Callable[..., Downloader]) -> None:
    opts = make_downloader(ytdlp_js_runtimes="node:/opt/node/bin/node, bun").build_opts()
    assert opts["js_runtimes"] == {"node": {"path": "/opt/node/bin/node"}, "bun": {}}


def test_js_runtimes_opts_are_accepted_by_ytdlp(make_settings: Callable[..., Settings]) -> None:
    # Real YoutubeDL validates js_runtimes in __init__ without running anything.
    for value in ("deno,node", "node:/usr/bin/node", "bun,quickjs"):
        opts = Downloader(make_settings(ytdlp_js_runtimes=value)).build_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            assert set(ydl.params["js_runtimes"]) == set(opts["js_runtimes"])


def test_build_opts_extractor_args(make_downloader: Callable[..., Downloader]) -> None:
    opts = make_downloader(
        ytdlp_extractor_args="youtube:player_client=default,mweb;po_token=mweb.gvs+T"
    ).build_opts(Format.MP3)
    assert opts["extractor_args"] == {
        "youtube": {"player_client": ["default", "mweb"], "po_token": ["mweb.gvs+T"]}
    }


def test_build_opts_cookies_and_proxy(make_downloader: Callable[..., Downloader]) -> None:
    opts = make_downloader(
        ytdlp_cookies_file="/etc/secrets/cookies.txt", ytdlp_proxy="http://p:1"
    ).build_opts()
    assert opts["cookiefile"] == "/etc/secrets/cookies.txt"
    assert opts["proxy"] == "http://p:1"


def test_cookie_file_is_copied_per_instance(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    cookies.chmod(0o444)  # like a read-only mounted secret file
    downloader = make_downloader(ytdlp_cookies_file=cookies)

    downloader.probe(VIDEO_ID)

    used = fake_ydl.instances[0].opts["cookiefile"]
    assert used != str(cookies)
    assert not os.path.exists(used)  # temp copy removed
    assert cookies.read_text() == "# Netscape HTTP Cookie File\n"


def test_logger_routes_messages(caplog: pytest.LogCaptureFixture) -> None:
    logger = dl._YtDlpLogger()
    with caplog.at_level(logging.DEBUG, logger=dl.log.name):
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
    assert [r.levelno for r in caplog.records] == [
        logging.DEBUG, logging.DEBUG, logging.WARNING, logging.INFO,
    ]


# -------------------------------------------------------------------- probe
def test_watch_url() -> None:
    assert watch_url(VIDEO_ID) == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_available_heights_ignores_audio_and_storyboards() -> None:
    assert available_heights(FORMATS) == [360, 720, 1080]
    assert available_heights([]) == []


def test_probe_maps_metadata(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL]
) -> None:
    info = make_downloader().probe(VIDEO_ID)
    assert info.video_id == VIDEO_ID
    assert info.title == "Never Gonna Give You Up"
    assert info.duration == 213
    assert info.channel == "Rick Astley"
    assert info.available_heights == [360, 720, 1080]
    assert info.downloadable is True
    assert info.reason is None
    assert fake_ydl.instances[0].calls == [(watch_url(VIDEO_ID), False)]


def test_probe_falls_back_for_missing_fields(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL]
) -> None:
    fake_ydl.info = {"id": VIDEO_ID, "duration": 12.7, "uploader": "Uploader"}
    info = make_downloader().probe(VIDEO_ID)
    assert info.title == VIDEO_ID
    assert info.duration == 12
    assert info.channel == "Uploader"
    assert info.available_heights == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"duration": 1801}, "too_long"),
        ({"duration": 1800}, None),
        ({"duration": None}, "unknown_duration"),
        ({"duration": "n/a"}, "unknown_duration"),
        ({"live_status": "is_live"}, "live"),
        ({"live_status": "is_upcoming"}, "live"),
        ({"live_status": None, "is_live": True}, "live"),
    ],
)
def test_probe_downloadable_rules(
    make_downloader: Callable[..., Downloader],
    fake_ydl: type[FakeYoutubeDL],
    overrides: dict[str, Any],
    reason: str | None,
) -> None:
    fake_ydl.info = base_info(**overrides)
    info = make_downloader().probe(VIDEO_ID)
    assert info.reason == reason
    assert info.downloadable is (reason is None)


def test_probe_is_cached_until_ttl(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL]
) -> None:
    clock = [0.0]
    downloader = make_downloader(clock=lambda: clock[0])
    first = downloader.probe(VIDEO_ID)
    clock[0] = dl.PROBE_CACHE_TTL_SECONDS - 1
    assert downloader.probe(VIDEO_ID) is first
    assert len(fake_ydl.instances) == 1

    clock[0] = dl.PROBE_CACHE_TTL_SECONDS * 3
    assert downloader.probe(VIDEO_ID) is not first
    assert len(fake_ydl.instances) == 2


def test_probe_maps_errors(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL]
) -> None:
    fake_ydl.error = DownloadError("ERROR: [youtube] dQw4w9WgXcQ: Private video. Sign in")
    with pytest.raises(DownloaderError) as excinfo:
        make_downloader().probe(VIDEO_ID)
    assert excinfo.value.code == "private"
    assert excinfo.value.client_error is True
    assert excinfo.value.message == "Private video. Sign in"


# ---------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    ("message", "code", "client_error"),
    [
        ("ERROR: [youtube] x: Private video", "private", True),
        ("Sign in to confirm your age. This video may be inappropriate", "age_restricted", True),
        ("This video is age-restricted", "age_restricted", True),
        ("Sign in to confirm you're not a bot", "bot_check", False),
        ("Postprocessing: ffprobe and ffmpeg not found", "ffmpeg_missing", False),
        ("Join this channel to get access to members-only content", "members_only", True),
        ("The uploader has not made this video available in your country", "geo_restricted", True),
        ("This video is not available in your country", "geo_restricted", True),
        ("This live event will begin in 3 hours", "live", True),
        ("Premieres in 2 days", "live", True),
        ("Requested format is not available", "format_unavailable", True),
        ("Video unavailable", "unavailable", True),
        ("This video has been removed by the uploader", "unavailable", True),
        ("Something exploded", "download_failed", False),
    ],
)
def test_map_ytdlp_error(message: str, code: str, client_error: bool) -> None:
    error = map_ytdlp_error(DownloadError(message))
    assert error.code == code
    assert error.client_error is client_error


def test_map_error_message_is_trimmed() -> None:
    error = map_ytdlp_error(ExtractorError("x" * 1000, expected=True))
    assert len(error.message) <= 300


def test_clean_message_keeps_plain_text() -> None:
    assert dl._clean_message("ERROR: plain failure") == "plain failure"
    assert dl._clean_message("[weird no colon") == "[weird no colon"


# ----------------------------------------------------------------- download
def downloading(done: int, total: int | None, estimate: int | None = None) -> dict[str, Any]:
    return {
        "status": "downloading",
        "downloaded_bytes": done,
        "total_bytes": total,
        "total_bytes_estimate": estimate,
    }


def test_download_mp4_reports_monotonic_progress(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.info = base_info(height=720)
    fake_ydl.write_ext = "mp4"
    fake_ydl.hook_events = [
        downloading(50, 100),
        {"status": "finished"},  # video stream
        downloading(0, None),  # unknown size: no regression
        downloading(50, None, estimate=100),
        {"status": "finished"},  # audio stream
        {"status": "error"},  # ignored
    ]
    progress: list[float] = []

    result = make_downloader().download(VIDEO_ID, Format.MP4_720, tmp_path / "job", progress.append)

    assert result.path == tmp_path / "job" / f"{VIDEO_ID}.mp4"
    assert result.title == "Never Gonna Give You Up"
    assert result.actual_height == 720
    assert progress == sorted(progress)
    assert progress[0] == pytest.approx(0.25 * 0.95)
    assert progress[-1] == 1.0
    opts = fake_ydl.instances[0].opts
    assert opts["outtmpl"] == {"default": str(tmp_path / "job" / "%(id)s.%(ext)s")}
    assert fake_ydl.instances[0].calls == [(watch_url(VIDEO_ID), True)]


def test_download_1080_fallback_reports_actual_height(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.info = base_info(height=720)  # video only had 720p
    fake_ydl.write_ext = "mp4"
    result = make_downloader().download(VIDEO_ID, Format.MP4_1080, tmp_path, lambda _: None)
    assert result.actual_height == 720


def test_download_mp3_has_no_height(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.info = base_info(height=1080, title=None)
    fake_ydl.write_ext = "mp3"
    fake_ydl.hook_events = [downloading(10, 10)]
    progress: list[float] = []
    result = make_downloader().download(VIDEO_ID, Format.MP3, tmp_path, progress.append)
    assert result.path.suffix == ".mp3"
    assert result.actual_height is None
    assert result.title == VIDEO_ID
    assert progress == [0.95, 1.0]


def test_download_non_int_height_is_none(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.info = base_info(height=None)
    fake_ydl.write_ext = "mp4"
    result = make_downloader().download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None)
    assert result.actual_height is None


def test_download_finds_file_without_requested_downloads(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.write_ext = "mp3"
    fake_ydl.report_filepath = False
    result = make_downloader().download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert result.path == tmp_path / f"{VIDEO_ID}.mp3"


def test_download_globs_for_unexpected_extension(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.write_ext = "webm"
    fake_ydl.report_filepath = False
    (tmp_path / f"{VIDEO_ID}.f137.mp4.part").write_bytes(b"partial")
    result = make_downloader().download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None)
    assert result.path == tmp_path / f"{VIDEO_ID}.webm"


def test_download_skips_stale_requested_paths(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.info = base_info(requested_downloads=[{"filepath": str(tmp_path / "gone.mp4")}, {}])
    fake_ydl.write_ext = "mp4"
    fake_ydl.report_filepath = False
    result = make_downloader().download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None)
    assert result.path == tmp_path / f"{VIDEO_ID}.mp4"


def test_download_without_output_file_fails(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    (tmp_path / f"{VIDEO_ID}.mp4.part").write_bytes(b"partial")
    with pytest.raises(DownloaderError) as excinfo:
        make_downloader().download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None)
    assert excinfo.value.code == "download_failed"
    assert excinfo.value.client_error is False


def test_download_maps_errors(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.error = DownloadError("ERROR: [youtube] x: Video unavailable")
    with pytest.raises(DownloaderError) as excinfo:
        make_downloader().download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert excinfo.value.code == "unavailable"


def test_download_cancelled_by_ytdlp(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.error = DownloadCancelled("stop")
    with pytest.raises(Cancelled):
        make_downloader().download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)


def test_progress_hook_aborts_when_cancelled(
    make_downloader: Callable[..., Downloader], fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    fake_ydl.hook_events = [downloading(1, 10)]
    fake_ydl.write_ext = "mp4"
    with pytest.raises(Cancelled):
        make_downloader().download(
            VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None, is_cancelled=lambda: True
        )
