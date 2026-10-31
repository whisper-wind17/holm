"""Sanitized error responses for the Control API's internal endpoints.

The Gateway's public error contract defines inference error codes; those describe inference
traffic and none of them names "the process failed for an unexpected reason".
The ``/internal/`` endpoints are a different surface, so this module keeps its
own single stable code instead of stretching a Gateway code to cover it.

The privacy policy is the constraint that shapes the body: an outward error
carries a stable code and a Request ID, and never an instance name, address,
path, Secret or raw exception text.
"""

from __future__ import annotations

import logging
from typing import Final, Literal

from holm_common import new_uuid7
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from holm_control_api.http_constants import CACHE_CONTROL_VALUE

INTERNAL_ERROR_CODE: Final[Literal["internal_error"]] = "internal_error"
INTERNAL_ERROR_STATUS: Final[int] = 500

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.control_api")


class InternalErrorBody(BaseModel):
    """The only fields an internal-endpoint failure ever discloses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["internal_error"]
    request_id: str


class InternalErrorResponse(BaseModel):
    """Envelope wrapper so the internal error shape stays extensible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: InternalErrorBody


async def handle_unexpected_error(request: Request, exception: Exception) -> JSONResponse:
    """Turn any uncaught exception into a stable code plus a correlation ID.

    The exception is not re-wrapped or discarded: Starlette's server error
    middleware re-raises it after this response is sent, so the process still
    surfaces the failure to its own logs. Only the *caller* is denied the
    stack, the module path and the exception message, any of which can carry
    internal addresses or request-derived values.
    """
    request_id = new_uuid7()
    _LOGGER.error(
        "unhandled internal error",
        extra={
            "event_name": "internal_endpoint_unhandled_error",
            "component": "control-api",
            "error_code": INTERNAL_ERROR_CODE,
            "request_id": str(request_id),
            # The class name is a bounded identifier chosen by our own code or a
            # library, unlike str(exception), which routinely embeds values.
            "sanitized_message": type(exception).__name__,
        },
    )
    payload = InternalErrorResponse(
        error=InternalErrorBody(code=INTERNAL_ERROR_CODE, request_id=str(request_id))
    )
    rendered_request_id = str(request_id)
    return JSONResponse(
        status_code=INTERNAL_ERROR_STATUS,
        content=payload.model_dump(mode="json"),
        headers={"Cache-Control": CACHE_CONTROL_VALUE, "x-request-id": rendered_request_id},
    )
