import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from .config import Settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_app_settings(request: Request) -> Settings:
    # The settings the app was built with (create_app), so routes, the job
    # runner and CORS all agree.
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def require_api_key(
    settings: SettingsDep,
    api_key: Annotated[str | None, Depends(_api_key_header)] = None,
) -> None:
    expected = settings.api_key.get_secret_value().encode()
    if not api_key or not hmac.compare_digest(api_key.encode(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid or missing API key"},
            headers={"WWW-Authenticate": "APIKey"},
        )


def _signature(secret: str, job_id: str, exp: int) -> str:
    message = f"{job_id}:{exp}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign(
    settings: Settings, job_id: str, *, now: Callable[[], float] = time.time
) -> tuple[int, str]:
    """Return ``(exp, sig)`` for a short-lived download link."""
    exp = int(now()) + settings.signed_url_ttl_seconds
    return exp, _signature(settings.signing_secret.get_secret_value(), job_id, exp)


def verify_signature(
    settings: Settings,
    job_id: str,
    exp: int,
    sig: str,
    *,
    now: Callable[[], float] = time.time,
) -> None:
    expected = _signature(settings.signing_secret.get_secret_value(), job_id, exp)
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "invalid_signature", "message": "Invalid download link"},
        )
    if exp < now():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "link_expired", "message": "Download link expired, poll the job again"},
        )
