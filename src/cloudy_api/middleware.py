"""Pure ASGI middleware."""

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _too_large_detail(max_bytes: int) -> dict[str, str]:
    return {
        "code": "request_too_large",
        "message": f"Request body is larger than {max_bytes} bytes",
    }


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with 413.

    FastAPI parses the whole JSON body into Python objects before any schema or
    route check runs, so this is the only place an oversized body can be stopped
    before it costs memory. A ``Content-Length`` over the limit is rejected
    without reading the body. Bodies without one (chunked) or that lie about it
    are counted as they stream in and cut off at the limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            response = JSONResponse(
                {"detail": _too_large_detail(self.max_bytes)},
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                # The body is left unread, so don't try to reuse the connection.
                headers={"Connection": "close"},
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises an HTTPException raised while it reads the
                    # body, and the app's exception handler turns it into a 413.
                    raise HTTPException(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=_too_large_detail(self.max_bytes),
                    )
            return message

        await self.app(scope, limited_receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
