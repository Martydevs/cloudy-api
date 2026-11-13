"""OpenAPI error-response docs shared by the routers (shown in the Scalar docs)."""

from typing import Any

from ..schemas import ErrorResponse

Responses = dict[int | str, dict[str, Any]]


def error(description: str, model: type = ErrorResponse) -> dict[str, Any]:
    return {"model": model, "description": description}


UNAUTHORIZED: Responses = {
    401: error("Missing or invalid `X-API-Key` header (`unauthorized`)."),
}

ENGINE_FAILED: Responses = {
    502: error(
        "YouTube or the metadata API failed on our side: `bot_check`, `quota_exceeded`, "
        "`metadata_unavailable`, `download_failed`, … Retry later."
    ),
}
