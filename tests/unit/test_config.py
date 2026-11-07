from pathlib import Path

import pytest
from pydantic import ValidationError

from cloudy_api.config import Settings, get_settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "API_KEY",
        "SIGNING_SECRET",
        "ALLOWED_ORIGINS",
        "PUBLIC_BASE_URL",
        "RENDER_EXTERNAL_URL",
        "YTDLP_COOKIES_FILE",
        "YTDLP_PROXY",
        "WORK_DIR",
        "MAX_BATCH",
        "YTDLP_JS_RUNTIMES",
        "YTDLP_EXTRACTOR_ARGS",
        "OPENROUTER_API_KEY",
        "OPENROUTER_APIKEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.allowed_origins == []
    assert s.max_concurrent_jobs == 1
    assert s.max_queue == 20
    assert s.max_batch == 10
    assert s.max_duration_seconds == 1800
    assert s.file_ttl_minutes == 30
    assert s.file_ttl_seconds == 1800
    assert s.signed_url_ttl_seconds == 600
    assert s.probe_concurrency == 3
    assert s.work_dir == Path("/tmp/cloudy")
    assert s.public_base_url is None
    assert s.ytdlp_cookies_file is None
    assert s.ytdlp_proxy is None
    assert s.ytdlp_js_runtimes == {"deno": None, "node": None}
    assert s.ytdlp_extractor_args == {}
    assert s.openrouter_api_key is None
    assert s.openrouter_model == "openai/gpt-4o-mini"
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert s.llm_timeout_seconds == 30


def test_env_parsing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("API_KEY", "key")
    clean_env.setenv("SIGNING_SECRET", "secret")
    clean_env.setenv("ALLOWED_ORIGINS", " https://a.vercel.app/ ,http://localhost:3000,, ")
    clean_env.setenv("MAX_BATCH", "5")
    clean_env.setenv("WORK_DIR", "/data/cloudy")
    clean_env.setenv("YTDLP_PROXY", "http://proxy:8080")
    clean_env.setenv("YTDLP_COOKIES_FILE", "/etc/secrets/cookies.txt")

    s = Settings(_env_file=None)

    assert s.api_key.get_secret_value() == "key"
    assert s.allowed_origins == ["https://a.vercel.app", "http://localhost:3000"]
    assert s.max_batch == 5
    assert s.work_dir == Path("/data/cloudy")
    assert s.ytdlp_proxy is not None
    assert s.ytdlp_proxy.get_secret_value() == "http://proxy:8080"
    assert s.ytdlp_cookies_file == Path("/etc/secrets/cookies.txt")


@pytest.mark.parametrize("name", ["OPENROUTER_API_KEY", "OPENROUTER_APIKEY"])
def test_openrouter_settings_from_env(clean_env: pytest.MonkeyPatch, name: str) -> None:
    clean_env.setenv(name, "sk-or-secret")
    clean_env.setenv("OPENROUTER_MODEL", "vendor/model")
    clean_env.setenv("LLM_TIMEOUT_SECONDS", "5.5")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.openrouter_api_key is not None
    assert s.openrouter_api_key.get_secret_value() == "sk-or-secret"
    assert "sk-or-secret" not in repr(s)
    assert s.openrouter_model == "vendor/model"
    assert s.llm_timeout_seconds == 5.5


def test_empty_openrouter_key_means_disabled(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("OPENROUTER_API_KEY", " ")
    assert Settings(api_key="k", signing_secret="s", _env_file=None).openrouter_api_key is None


def test_llm_timeout_must_be_positive(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(api_key="k", signing_secret="s", llm_timeout_seconds=0, _env_file=None)


def test_secrets_are_not_shown_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(api_key="super-secret", signing_secret="other-secret", _env_file=None)
    assert "super-secret" not in repr(s)
    assert "other-secret" not in repr(s)


def test_empty_optional_values_become_none(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("YTDLP_PROXY", "")
    clean_env.setenv("YTDLP_COOKIES_FILE", "  ")
    clean_env.setenv("PUBLIC_BASE_URL", " ")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.ytdlp_proxy is None
    assert s.ytdlp_cookies_file is None
    assert s.public_base_url is None


def test_public_base_url_falls_back_to_render_url(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("RENDER_EXTERNAL_URL", "https://cloudy-api.onrender.com/")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.public_base_url == "https://cloudy-api.onrender.com"


def test_public_base_url_takes_precedence(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("RENDER_EXTERNAL_URL", "https://cloudy-api.onrender.com")
    clean_env.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.public_base_url == "https://api.example.com"


def test_origins_accept_a_list(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(api_key="k", signing_secret="s", allowed_origins=["https://x"], _env_file=None)
    assert s.allowed_origins == ["https://x"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("node", {"node": None}),
        (" Deno , node ", {"deno": None, "node": None}),
        ("node:/opt/node/bin/node,bun", {"node": "/opt/node/bin/node", "bun": None}),
        ("quickjs:/usr/local/bin/qjs,,", {"quickjs": "/usr/local/bin/qjs"}),
        ("node:", {"node": None}),
    ],
)
def test_js_runtimes_parsing(
    clean_env: pytest.MonkeyPatch, raw: str, expected: dict[str, str | None]
) -> None:
    clean_env.setenv("YTDLP_JS_RUNTIMES", raw)
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.ytdlp_js_runtimes == expected


@pytest.mark.parametrize(("raw", "message"), [("", "at least one"), (" , ", "at least one"),
                                             ("node,rhino", "unsupported JS runtime(s): rhino")])
def test_js_runtimes_validation(clean_env: pytest.MonkeyPatch, raw: str, message: str) -> None:
    clean_env.setenv("YTDLP_JS_RUNTIMES", raw)
    with pytest.raises(ValidationError) as excinfo:
        Settings(api_key="k", signing_secret="s", _env_file=None)
    assert message in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ("youtube:player_client=default,mweb", {"youtube": {"player_client": ["default", "mweb"]}}),
        (
            "youtube:player_client=tv;po_token=mweb.gvs+TOKEN youtubepot-bgutilhttp:base_url=http://pot:4416",
            {
                "youtube": {"player_client": ["tv"], "po_token": ["mweb.gvs+TOKEN"]},
                "youtubepot-bgutilhttp": {"base_url": ["http://pot:4416"]},
            },
        ),
        # same normalisation as the yt-dlp CLI: lower-case keys, '-' -> '_', `\,` escapes
        # (single quotes keep the backslash away from shell-style splitting)
        ("'YouTube:Player-Client=a\\,b'", {"youtube": {"player_client": ["a,b"]}}),
        ("'youtube:player_skip=webpage, configs'", {"youtube": {"player_skip": ["webpage", "configs"]}}),
    ],
)
def test_extractor_args_parsing(
    clean_env: pytest.MonkeyPatch, raw: str, expected: dict
) -> None:
    clean_env.setenv("YTDLP_EXTRACTOR_ARGS", raw)
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.ytdlp_extractor_args == expected


@pytest.mark.parametrize("raw", ["nocolon", "youtube:x=1 bad"])
def test_extractor_args_validation(clean_env: pytest.MonkeyPatch, raw: str) -> None:
    clean_env.setenv("YTDLP_EXTRACTOR_ARGS", raw)
    with pytest.raises(ValidationError) as excinfo:
        Settings(api_key="k", signing_secret="s", _env_file=None)
    assert "wrong --extractor-args formatting" in str(excinfo.value)


def test_extractor_args_ignore_ytdlp_config_files(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A stray yt-dlp config on the host must not leak into the service.
    config_dir = tmp_path / "yt-dlp"
    config_dir.mkdir()
    (config_dir / "config").write_text("--extractor-args youtube:player_client=evil\n")
    clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path))
    clean_env.setenv("YTDLP_EXTRACTOR_ARGS", "youtube:player_client=tv")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.ytdlp_extractor_args == {"youtube": {"player_client": ["tv"]}}


def test_extractor_args_accepts_a_dict(clean_env: pytest.MonkeyPatch) -> None:
    value = {"youtube": {"player_client": ["tv"]}}
    s = Settings(api_key="k", signing_secret="s", ytdlp_extractor_args=value, _env_file=None)
    assert s.ytdlp_extractor_args == value


def test_js_runtimes_accepts_a_dict(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(api_key="k", signing_secret="s", ytdlp_js_runtimes={"bun": None}, _env_file=None)
    assert s.ytdlp_js_runtimes == {"bun": None}


@pytest.mark.parametrize("missing", ["API_KEY", "SIGNING_SECRET"])
def test_missing_required_vars_fail(clean_env: pytest.MonkeyPatch, missing: str) -> None:
    clean_env.setenv("API_KEY", "k")
    clean_env.setenv("SIGNING_SECRET", "s")
    clean_env.delenv(missing)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert missing.lower() in str(excinfo.value)


def test_invalid_limits_fail(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(api_key="k", signing_secret="s", max_batch=0, _env_file=None)


def test_get_settings_is_cached(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_env.setenv("API_KEY", "k")
    clean_env.setenv("SIGNING_SECRET", "s")
    clean_env.chdir(tmp_path)  # no .env here
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
