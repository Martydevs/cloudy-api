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
        "WORK_DIR",
        "MAX_BATCH",
        "OPENROUTER_API_KEY",
        "OPENROUTER_APIKEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
        "PYTUBEFIX_CLIENT",
        "PYTUBEFIX_PROXY",
        "YOUTUBE_DATA_API_KEY",
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
    assert s.openrouter_api_key is None
    assert s.openrouter_model == "@preset/lyrics-generator"
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert s.llm_timeout_seconds == 30
    assert s.pytubefix_client == "MWEB"
    assert s.pytubefix_proxy is None
    assert s.youtube_data_api_key is None


def test_env_parsing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("API_KEY", "key")
    clean_env.setenv("SIGNING_SECRET", "secret")
    clean_env.setenv("ALLOWED_ORIGINS", " https://a.vercel.app/ ,http://localhost:3000,, ")
    clean_env.setenv("MAX_BATCH", "5")
    clean_env.setenv("WORK_DIR", "/data/cloudy")
    clean_env.setenv("PYTUBEFIX_PROXY", "http://proxy:8080")

    s = Settings(_env_file=None)

    assert s.api_key.get_secret_value() == "key"
    assert s.allowed_origins == ["https://a.vercel.app", "http://localhost:3000"]
    assert s.max_batch == 5
    assert s.work_dir == Path("/data/cloudy")
    assert s.pytubefix_proxy is not None
    assert s.pytubefix_proxy.get_secret_value() == "http://proxy:8080"


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
    clean_env.setenv("PYTUBEFIX_PROXY", "")
    clean_env.setenv("PUBLIC_BASE_URL", " ")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.pytubefix_proxy is None
    assert s.public_base_url is None


def test_youtube_data_api_key_from_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("YOUTUBE_DATA_API_KEY", "AIza-secret")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.youtube_data_api_key is not None
    assert s.youtube_data_api_key.get_secret_value() == "AIza-secret"
    assert "AIza-secret" not in repr(s)


def test_empty_youtube_data_api_key_means_disabled(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("YOUTUBE_DATA_API_KEY", " ")
    assert Settings(api_key="k", signing_secret="s", _env_file=None).youtube_data_api_key is None


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


# ---------------------------------------------------------------- pytubefix
def test_pytubefix_settings_from_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PYTUBEFIX_CLIENT", " web_kids ")
    clean_env.setenv("PYTUBEFIX_PROXY", "http://user:pass@proxy:8080")
    s = Settings(api_key="k", signing_secret="s", _env_file=None)
    assert s.pytubefix_client == "WEB_KIDS"
    assert s.pytubefix_proxy is not None
    assert s.pytubefix_proxy.get_secret_value() == "http://user:pass@proxy:8080"
    assert "user:pass" not in repr(s)


def test_empty_pytubefix_proxy_means_none(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PYTUBEFIX_PROXY", " ")
    assert Settings(api_key="k", signing_secret="s", _env_file=None).pytubefix_proxy is None


def test_unknown_pytubefix_client_fails(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PYTUBEFIX_CLIENT", "NETSCAPE")
    with pytest.raises(ValidationError, match="unsupported pytubefix client"):
        Settings(api_key="k", signing_secret="s", _env_file=None)


def test_pytubefix_client_accepts_non_string_passthrough(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(api_key="k", signing_secret="s", pytubefix_client=1, _env_file=None)
