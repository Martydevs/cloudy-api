import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest
from pytubefix import YouTube
from pytubefix import exceptions as pexc

from cloudy_api.config import Settings
from cloudy_api.schemas import Format
from cloudy_api.services.downloader import Cancelled, DownloaderError
from cloudy_api.services.pytubefix_downloader import (
    PytubefixDownloader,
    _PhaseProgress,
    map_pytubefix_error,
    pick_audio,
    run_ffmpeg,
)

VIDEO_ID = "dQw4w9WgXcQ"


class FakeStream:
    def __init__(
        self,
        itag: int,
        *,
        subtype: str = "mp4",
        height: int | None = None,
        video_codec: str | None = None,
        audio_codec: str | None = None,
        bitrate: int = 1000,
        fps: int = 30,
        default_audio: bool = True,
        size: int = 400,
    ) -> None:
        self.itag = itag
        self.subtype = subtype
        self.height = height
        self.video_codec = video_codec
        self.audio_codec = audio_codec
        self.bitrate = bitrate
        self.fps = fps
        self.includes_video_track = video_codec is not None
        self.includes_audio_track = audio_codec is not None
        self.is_default_audio_track = default_audio and self.includes_audio_track
        self.filesize = size
        self.youtube: FakeYouTube | None = None
        self.calls: list[dict[str, Any]] = []
        self.error: BaseException | None = None
        self.write_file = True

    def download(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        path = Path(kwargs["output_path"]) / kwargs["filename"]
        callback = self.youtube.kwargs.get("on_progress_callback") if self.youtube else None
        with path.open("wb") as fh:
            for remaining in (300, 200, 100, 0):
                fh.write(b"x" * 100)
                if callback:
                    callback(self, b"x" * 100, remaining)
        if not self.write_file:
            path.unlink()
        return str(path)


def video(itag: int, height: int, codec: str = "avc1.4d401f", **kw: Any) -> FakeStream:
    return FakeStream(itag, height=height, video_codec=codec, **kw)


def audio(itag: int, codec: str = "mp4a.40.2", subtype: str = "mp4", **kw: Any) -> FakeStream:
    return FakeStream(itag, subtype=subtype, audio_codec=codec, **kw)


def progressive(itag: int, height: int, subtype: str = "mp4") -> FakeStream:
    return FakeStream(
        itag, subtype=subtype, height=height, video_codec="avc1.42001E", audio_codec="mp4a.40.2"
    )


def default_streams() -> list[FakeStream]:
    return [
        progressive(18, 360),
        video(136, 720),
        video(137, 1080),
        video(247, 720, "vp9", bitrate=5000),
        video(248, 1080, "vp9", bitrate=9000),
        audio(140, bitrate=128_000),
        audio(251, "opus", "webm", bitrate=160_000),
    ]


class FakeYouTube:
    instances: list["FakeYouTube"] = []

    def __init__(self, url: str, **kwargs: Any) -> None:
        self.url = url
        self.kwargs = kwargs
        self.details: dict[str, Any] = {
            "title": "Never Gonna Give You Up",
            "lengthSeconds": "213",
            "author": "Rick Astley",
        }
        self.availability_error: BaseException | None = None
        self.stream_list: list[FakeStream] = default_streams()
        self.thumbnail_url = f"https://i.ytimg.com/vi/{VIDEO_ID}/max.jpg"
        FakeYouTube.instances.append(self)

    @property
    def vid_info(self) -> dict[str, Any]:
        return {"videoDetails": self.details}

    def check_availability(self) -> None:
        if self.availability_error is not None:
            raise self.availability_error

    @property
    def streams(self) -> list[FakeStream]:
        self.check_availability()
        for s in self.stream_list:
            s.youtube = self
        return self.stream_list


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FfmpegRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.error: DownloaderError | None = None

    def __call__(self, args: Sequence[str]) -> None:
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        Path(args[-1]).write_bytes(b"merged")


@pytest.fixture
def ffmpeg() -> FfmpegRecorder:
    return FfmpegRecorder()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def setup(
    make_settings: Callable[..., Settings], ffmpeg: FfmpegRecorder, clock: Clock
) -> Callable[..., tuple[PytubefixDownloader, list[FakeYouTube]]]:
    def factory(
        configure: Callable[[FakeYouTube], None] | None = None,
        *,
        has_ffmpeg: bool = True,
        **settings: Any,
    ) -> tuple[PytubefixDownloader, list[FakeYouTube]]:
        created: list[FakeYouTube] = []

        def yt_factory(url: str, **kwargs: Any) -> FakeYouTube:
            yt = FakeYouTube(url, **kwargs)
            if configure:
                configure(yt)
            created.append(yt)
            return yt

        downloader = PytubefixDownloader(
            make_settings(**settings),
            yt_factory=yt_factory,
            ffmpeg_runner=ffmpeg,
            which=lambda name: f"/usr/bin/{name}" if has_ffmpeg else None,
            now=clock,
        )
        return downloader, created

    return factory


Setup = Callable[..., tuple[PytubefixDownloader, list[FakeYouTube]]]


# -------------------------------------------------------------- construction
def test_default_factory_is_real_youtube(settings: Settings) -> None:
    assert PytubefixDownloader(settings)._yt_factory is YouTube


def test_youtube_options(setup: Setup) -> None:
    downloader, created = setup()
    downloader.probe(VIDEO_ID)
    assert created[0].url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    # No use_po_token: in pytubefix 11 it prompts on stdin.
    assert created[0].kwargs == {"client": "MWEB"}


def test_youtube_options_with_proxy_and_client(setup: Setup) -> None:
    downloader, created = setup(pytubefix_client="TV", pytubefix_proxy="http://p:1")
    downloader.probe(VIDEO_ID)
    assert created[0].kwargs == {
        "client": "TV",
        "proxies": {"http": "http://p:1", "https": "http://p:1"},
    }


# --------------------------------------------------------------------- probe
def test_probe_maps_metadata(setup: Setup) -> None:
    downloader, _ = setup()
    info = downloader.probe(VIDEO_ID)
    assert info.model_dump() == {
        "video_id": VIDEO_ID,
        "title": "Never Gonna Give You Up",
        "duration": 213,
        "thumbnail": f"https://i.ytimg.com/vi/{VIDEO_ID}/max.jpg",
        "channel": "Rick Astley",
        "available_heights": [360, 720, 1080],
        "downloadable": True,
        "reason": None,
    }


def test_probe_falls_back_for_missing_fields(setup: Setup) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.details = {"lengthSeconds": "abc"}

    info = setup(configure)[0].probe(VIDEO_ID)
    assert info.title == VIDEO_ID
    assert info.channel is None
    assert info.duration is None
    assert info.reason == "unknown_duration"
    assert info.downloadable is False


@pytest.mark.parametrize(
    ("details", "error", "duration", "reason"),
    [
        ({"lengthSeconds": "4000"}, None, 4000, "too_long"),
        ({}, None, None, "unknown_duration"),
        ({"lengthSeconds": ""}, None, None, "unknown_duration"),
        ({"lengthSeconds": "0", "isLive": True}, None, None, "live"),
        ({"lengthSeconds": "0", "isUpcoming": True}, None, None, "live"),
        ({"lengthSeconds": "60"}, pexc.LiveStreamError(VIDEO_ID), 60, "live"),
        ({"lengthSeconds": "60"}, pexc.LiveStreamOffline(VIDEO_ID, "soon"), 60, "live"),
    ],
)
def test_probe_downloadable_rules(
    setup: Setup,
    details: dict[str, Any],
    error: BaseException | None,
    duration: int | None,
    reason: str,
) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.details = {"title": "t", **details}
        yt.availability_error = error

    info = setup(configure)[0].probe(VIDEO_ID)
    assert (info.duration, info.reason, info.downloadable) == (duration, reason, False)
    if reason == "live":
        assert info.available_heights == []


def test_probe_is_cached_until_ttl(setup: Setup, clock: Clock) -> None:
    downloader, created = setup()
    downloader.probe(VIDEO_ID)
    clock.value = 599
    downloader.probe(VIDEO_ID)
    assert len(created) == 1
    clock.value = 601
    downloader.probe(VIDEO_ID)
    assert len(created) == 2


@pytest.mark.parametrize(
    ("error", "code", "client_error"),
    [
        (pexc.BotDetection(VIDEO_ID), "bot_check", False),
        (pexc.VideoPrivate(VIDEO_ID), "private", True),
        (pexc.VideoRegionBlocked(VIDEO_ID), "geo_restricted", True),
    ],
)
def test_probe_maps_errors(
    setup: Setup, error: BaseException, code: str, client_error: bool
) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.availability_error = error

    downloader, _ = setup(configure)
    with pytest.raises(DownloaderError) as info:
        downloader.probe(VIDEO_ID)
    assert (info.value.code, info.value.client_error) == (code, client_error)
    assert info.value.__cause__ is error


def test_probe_unexpected_error_is_502_and_logged(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.availability_error = KeyError("streamingData")

    with caplog.at_level(logging.WARNING), pytest.raises(DownloaderError) as info:
        setup(configure)[0].probe(VIDEO_ID)
    assert info.value.code == "download_failed"
    assert info.value.client_error is False
    assert any("pytubefix.unexpected_error" in r.message for r in caplog.records)


def test_probe_reraises_downloader_errors(setup: Setup) -> None:
    error = DownloaderError("x", "y")

    def configure(yt: FakeYouTube) -> None:
        yt.availability_error = error

    with pytest.raises(DownloaderError) as info:
        setup(configure)[0].probe(VIDEO_ID)
    assert info.value is error


# ------------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    ("error", "code", "client_error"),
    [
        (pexc.VideoPrivate(VIDEO_ID), "private", True),
        (pexc.AgeRestrictedError(VIDEO_ID), "age_restricted", True),
        (pexc.AgeCheckRequiredError(VIDEO_ID), "age_restricted", True),
        (pexc.AgeCheckRequiredAccountError(VIDEO_ID), "age_restricted", True),
        (pexc.BotDetection(VIDEO_ID), "bot_check", False),
        (pexc.PoTokenRequired(VIDEO_ID, "WEB"), "bot_check", False),
        (pexc.MembersOnly(VIDEO_ID), "members_only", True),
        (pexc.VideoRegionBlocked(VIDEO_ID), "geo_restricted", True),
        (pexc.LiveStreamError(VIDEO_ID), "live", True),
        (pexc.LiveStreamEnded(VIDEO_ID, "ended"), "live", True),
        (pexc.UnknownVideoError(VIDEO_ID), "download_failed", False),
        (pexc.InnerTubeResponseError(VIDEO_ID, "WEB"), "download_failed", False),
        (pexc.VideoUnavailable(VIDEO_ID), "unavailable", True),
        (pexc.VideoRemovedByUploader(VIDEO_ID, "removed"), "unavailable", True),
        (pexc.LoginRequired(VIDEO_ID, "Sign in to confirm you're not a bot"), "bot_check", False),
        (pexc.LoginRequired(VIDEO_ID, "Sign in to view"), "unavailable", True),
        (pexc.RegexMatchError("f", "p"), "download_failed", False),
        (HTTPError("u", 403, "Forbidden", None, None), "download_failed", False),  # type: ignore[arg-type]
        (RuntimeError(), "download_failed", False),
    ],
)
def test_map_pytubefix_error(error: BaseException, code: str, client_error: bool) -> None:
    mapped = map_pytubefix_error(error)
    assert (mapped.code, mapped.client_error) == (code, client_error)
    assert mapped.message
    assert len(mapped.message) <= 300


# ------------------------------------------------------------------ download
def test_download_mp4_merges_best_avc1_and_m4a(
    setup: Setup, ffmpeg: FfmpegRecorder, tmp_path: Path
) -> None:
    downloader, created = setup()
    progress: list[float] = []
    result = downloader.download(VIDEO_ID, Format.MP4_720, tmp_path, progress.append)

    streams = {s.itag: s for s in created[0].stream_list}
    assert streams[136].calls and streams[140].calls
    assert not streams[247].calls and not streams[137].calls
    call = streams[136].calls[0]
    assert call["skip_existing"] is False
    assert call["max_retries"] > 0 and call["timeout"] > 0

    (args,) = ffmpeg.calls
    assert args[0] == "ffmpeg"
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "copy"
    assert args[-1] == str(tmp_path / f"{VIDEO_ID}.mp4")

    assert result.path == tmp_path / f"{VIDEO_ID}.mp4"
    assert result.title == "Never Gonna Give You Up"
    assert result.actual_height == 720
    assert [p.name for p in tmp_path.iterdir()] == [f"{VIDEO_ID}.mp4"]  # parts removed

    assert progress == sorted(progress)
    assert progress[-1] == 1.0
    assert max(progress[:-1]) == pytest.approx(0.95)
    assert any(0.4 < p < 0.5 for p in progress)  # video phase is the first half


def test_download_1080_prefers_avc1_over_higher_bitrate_vp9(
    setup: Setup, tmp_path: Path
) -> None:
    result = setup()[0].download(VIDEO_ID, Format.MP4_1080, tmp_path, lambda _: None)
    assert result.actual_height == 1080


def test_download_falls_back_to_vp9_and_reencodes_opus(
    setup: Setup, ffmpeg: FfmpegRecorder, tmp_path: Path
) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.stream_list = [
            video(247, 720, "vp9"),
            video(244, 480, "vp9"),
            audio(251, "opus", "webm"),
        ]

    downloader, created = setup(configure)
    result = downloader.download(VIDEO_ID, Format.MP4_1080, tmp_path, lambda _: None)
    assert result.actual_height == 720
    (args,) = ffmpeg.calls
    assert args[args.index("-c:a") + 1] == "aac"
    assert "-b:a" in args
    assert created[0].stream_list[0].calls[0]["filename"] == f"{VIDEO_ID}.video.mp4"
    assert created[0].stream_list[2].calls[0]["filename"] == f"{VIDEO_ID}.audio.webm"


def test_download_progressive_mp4_needs_no_ffmpeg_run(
    setup: Setup, ffmpeg: FfmpegRecorder, tmp_path: Path
) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.stream_list = [progressive(18, 360), video(137, 1080)]

    progress: list[float] = []
    result = setup(configure)[0].download(VIDEO_ID, Format.MP4_720, tmp_path, progress.append)
    assert result.actual_height == 360
    assert result.path.read_bytes() == b"x" * 400
    assert ffmpeg.calls == []
    assert progress[-1] == 1.0
    assert [p.name for p in tmp_path.iterdir()] == [f"{VIDEO_ID}.mp4"]


def test_download_progressive_webm_is_remuxed(
    setup: Setup, ffmpeg: FfmpegRecorder, tmp_path: Path
) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.stream_list = [progressive(43, 360, subtype="webm")]

    result = setup(configure)[0].download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None)
    assert result.path.read_bytes() == b"merged"
    (args,) = ffmpeg.calls
    assert args[args.index("-i") + 1] == str(tmp_path / f"{VIDEO_ID}.single.webm")


def test_download_without_fitting_stream(setup: Setup, tmp_path: Path) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.stream_list = [video(137, 1080), audio(140)]

    with pytest.raises(DownloaderError) as info:
        setup(configure)[0].download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None)
    assert (info.value.code, info.value.client_error) == ("format_unavailable", True)


def test_download_mp3_uses_best_audio(
    setup: Setup, ffmpeg: FfmpegRecorder, tmp_path: Path
) -> None:
    downloader, created = setup()
    progress: list[float] = []
    result = downloader.download(VIDEO_ID, Format.MP3, tmp_path, progress.append)

    opus = next(s for s in created[0].stream_list if s.itag == 251)
    assert opus.calls  # highest bitrate wins for MP3, codec doesn't matter
    (args,) = ffmpeg.calls
    assert args[args.index("-c:a") + 1] == "libmp3lame"
    assert "-vn" in args
    assert result.path == tmp_path / f"{VIDEO_ID}.mp3"
    assert result.actual_height is None
    assert progress[-1] == 1.0
    assert max(progress[:-1]) == pytest.approx(0.95)


def test_download_mp3_from_progressive(setup: Setup, tmp_path: Path) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.stream_list = [progressive(18, 360), video(137, 1080)]

    downloader, created = setup(configure)
    downloader.download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert created[0].stream_list[0].calls


def test_download_mp3_without_audio(setup: Setup, tmp_path: Path) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.stream_list = [video(137, 1080)]

    with pytest.raises(DownloaderError) as info:
        setup(configure)[0].download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert info.value.code == "format_unavailable"


def test_download_without_ffmpeg(setup: Setup, tmp_path: Path) -> None:
    downloader, created = setup(has_ffmpeg=False)
    with pytest.raises(DownloaderError) as info:
        downloader.download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert (info.value.code, info.value.client_error) == ("ffmpeg_missing", False)
    assert created == []


def test_download_cancelled_mid_stream(setup: Setup, tmp_path: Path) -> None:
    calls = 0

    def is_cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 3

    with pytest.raises(Cancelled):
        setup()[0].download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None, is_cancelled)
    assert list(tmp_path.iterdir()) == []


def test_download_cancelled_before_start(setup: Setup, tmp_path: Path) -> None:
    downloader, created = setup()
    with pytest.raises(Cancelled):
        downloader.download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None, lambda: True)
    assert not any(s.calls for s in created[0].stream_list)


def test_download_maps_stream_errors_and_cleans_up(setup: Setup, tmp_path: Path) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.stream_list[5].error = HTTPError("u", 403, "Forbidden", None, None)  # type: ignore[arg-type]

    with pytest.raises(DownloaderError) as info:
        setup(configure)[0].download(VIDEO_ID, Format.MP4_720, tmp_path, lambda _: None)
    assert (info.value.code, info.value.client_error) == ("download_failed", False)
    assert list(tmp_path.iterdir()) == []


def test_download_maps_availability_errors(setup: Setup, tmp_path: Path) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.availability_error = pexc.BotDetection(VIDEO_ID)

    with pytest.raises(DownloaderError) as info:
        setup(configure)[0].download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert info.value.code == "bot_check"


def test_download_ffmpeg_failure_removes_output(
    setup: Setup, ffmpeg: FfmpegRecorder, tmp_path: Path
) -> None:
    ffmpeg.error = DownloaderError("download_failed", "ffmpeg failed: boom", client_error=False)
    (tmp_path / f"{VIDEO_ID}.mp3").write_bytes(b"stale")
    with pytest.raises(DownloaderError) as info:
        setup()[0].download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert info.value is ffmpeg.error
    assert list(tmp_path.iterdir()) == []


def test_download_stream_without_file(setup: Setup, tmp_path: Path) -> None:
    def configure(yt: FakeYouTube) -> None:
        for s in yt.stream_list:
            s.write_file = False

    with pytest.raises(DownloaderError) as info:
        setup(configure)[0].download(VIDEO_ID, Format.MP3, tmp_path, lambda _: None)
    assert info.value.code == "download_failed"


def test_download_title_falls_back_to_id(setup: Setup, tmp_path: Path) -> None:
    def configure(yt: FakeYouTube) -> None:
        yt.details = {}

    result = setup(configure)[0].download(VIDEO_ID, Format.MP3, tmp_path / "new", lambda _: None)
    assert result.title == VIDEO_ID
    assert result.path.parent == tmp_path / "new"


# ------------------------------------------------------------------- helpers
def test_pick_audio_prefers_original_track() -> None:
    dubbed = audio(1, bitrate=999_999, default_audio=False)
    original = audio(2, bitrate=1_000)
    assert pick_audio([dubbed, original]) is original
    assert pick_audio([]) is None


def test_progress_tracker_edge_cases() -> None:
    values: list[float] = []
    cancelled = False
    tracker = _PhaseProgress(values.append, lambda: cancelled)
    stream = FakeStream(1, size=0)
    tracker.on_chunk(stream, b"", 0)  # unknown size: nothing to report
    assert values == []
    stream.filesize = 100
    tracker.on_chunk(stream, b"", 150)  # remaining > size is clamped
    tracker.on_chunk(stream, b"", 50)
    tracker.on_chunk(stream, b"", 60)  # never goes backwards
    assert values == [pytest.approx(0.475)]
    cancelled = True
    with pytest.raises(Cancelled):
        tracker.finish_phase()


def test_run_ffmpeg_success() -> None:
    run_ffmpeg([sys.executable, "-c", "pass"])


def test_run_ffmpeg_failure_reports_last_stderr_line() -> None:
    script = "import sys; sys.stderr.write('first\\nInvalid data found\\n'); sys.exit(1)"
    with pytest.raises(DownloaderError) as info:
        run_ffmpeg([sys.executable, "-c", script])
    assert info.value.message == "ffmpeg failed: Invalid data found"
    assert info.value.client_error is False


def test_run_ffmpeg_failure_without_stderr() -> None:
    with pytest.raises(DownloaderError) as info:
        run_ffmpeg([sys.executable, "-c", "raise SystemExit(3)"])
    assert info.value.message == "ffmpeg failed: exit code 3"
