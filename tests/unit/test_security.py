import hmac
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from cloudy_api import security
from cloudy_api.config import Settings
from cloudy_api.security import require_api_key, sign, verify_signature

JOB_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def key_client(settings: Settings) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.settings = settings

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    def protected() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as c:
        yield c


def test_api_key_missing_returns_401(key_client: TestClient) -> None:
    r = key_client.get("/protected")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == "APIKey"


def test_api_key_wrong_returns_401(key_client: TestClient) -> None:
    r = key_client.get("/protected", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_api_key_valid(key_client: TestClient, settings: Settings) -> None:
    r = key_client.get(
        "/protected", headers={"X-API-Key": settings.api_key.get_secret_value()}
    )
    assert r.status_code == 200


def test_api_key_uses_constant_time_compare(
    key_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    key_client.get("/protected", headers={"X-API-Key": "nope"})
    assert calls


def test_sign_then_verify_roundtrip(settings: Settings) -> None:
    exp, sig = sign(settings, JOB_ID, now=lambda: 1000.0)
    assert exp == 1000 + settings.signed_url_ttl_seconds
    assert len(sig) == 64
    verify_signature(settings, JOB_ID, exp, sig, now=lambda: 1001.0)


def test_verify_rejects_expired_link(settings: Settings) -> None:
    exp, sig = sign(settings, JOB_ID, now=lambda: 1000.0)
    with pytest.raises(HTTPException) as excinfo:
        verify_signature(settings, JOB_ID, exp, sig, now=lambda: exp + 1)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "link_expired"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda exp, sig: (JOB_ID, exp + 1, sig),  # extended expiry
        lambda exp, sig: ("f" * 32, exp, sig),  # other job
        lambda exp, sig: (JOB_ID, exp, sig[:-1] + ("0" if sig[-1] != "0" else "1")),
        lambda exp, sig: (JOB_ID, exp, ""),
    ],
)
def test_verify_rejects_tampered_link(settings: Settings, tamper) -> None:  # type: ignore[no-untyped-def]
    exp, sig = sign(settings, JOB_ID, now=lambda: 1000.0)
    job_id, exp2, sig2 = tamper(exp, sig)
    with pytest.raises(HTTPException) as excinfo:
        verify_signature(settings, job_id, exp2, sig2, now=lambda: 1000.0)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "invalid_signature"


def test_signature_depends_on_secret(make_settings) -> None:  # type: ignore[no-untyped-def]
    a = make_settings(signing_secret="one")
    b = make_settings(signing_secret="two")
    _, sig_a = sign(a, JOB_ID, now=lambda: 0)
    _, sig_b = sign(b, JOB_ID, now=lambda: 0)
    assert sig_a != sig_b
