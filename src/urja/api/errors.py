"""RFC 7807 Problem Details error handling for the Urja API.

Specifies application/problem+json response payloads according to RFC 7807:
https://datatracker.ietf.org/doc/html/rfc7807
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from urja.client.exceptions import (
    MeterNotFoundError,
)

logger = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"
ERROR_TYPE_BASE = "https://urja-ops.flockenergy.tech/errors"


class ProblemDetail(BaseModel):
    """RFC 7807 problem details object."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    invalid_params: list[dict[str, Any]] | None = None


class NodeNotFoundError(Exception):
    """Raised when a hierarchy node code is not found."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(f"Hierarchy node '{node_id}' not found.")


def create_problem_response(
    status: int,
    title: str,
    detail: str,
    error_type: str,
    instance: str,
    invalid_params: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """Construct an RFC 7807 compliant JSONResponse."""
    problem = ProblemDetail(
        type=f"{ERROR_TYPE_BASE}/{error_type}",
        title=title,
        status=status,
        detail=detail,
        instance=instance,
        invalid_params=invalid_params,
    )
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def meter_not_found_handler(request: Request, exc: MeterNotFoundError) -> JSONResponse:
    return create_problem_response(
        status=404,
        title="Meter Not Found",
        detail=f"Meter with ID '{exc.meter_id}' was not found.",
        error_type="meter-not-found",
        instance=request.url.path,
    )


async def node_not_found_handler(request: Request, exc: NodeNotFoundError) -> JSONResponse:
    return create_problem_response(
        status=404,
        title="Node Not Found",
        detail=f"Hierarchy node with code '{exc.node_id}' was not found.",
        error_type="node-not-found",
        instance=request.url.path,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    invalid_params = []
    for err in exc.errors():
        loc = " -> ".join(str(x) for x in err.get("loc", []))
        invalid_params.append(
            {
                "name": loc,
                "reason": err.get("msg", "Invalid value"),
                "type": err.get("type", "value_error"),
            }
        )

    return create_problem_response(
        status=422,
        title="Unprocessable Entity",
        detail="The request parameters failed validation schema checks.",
        error_type="validation-error",
        instance=request.url.path,
        invalid_params=invalid_params,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    title_map = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }
    title = title_map.get(exc.status_code, "HTTP Error")
    detail = str(exc.detail) if exc.detail else title

    return create_problem_response(
        status=exc.status_code,
        title=title,
        detail=detail,
        error_type=title.lower().replace(" ", "-"),
        instance=request.url.path,
    )


async def portal_upstream_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    logger.error("Upstream portal error handling request %s: %s", request.url.path, exc)
    return create_problem_response(
        status=502,
        title="Bad Gateway",
        detail=f"Upstream portal communication failed: {exc}",
        error_type="upstream-portal-error",
        instance=request.url.path,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server exception at %s: %s", request.url.path, exc)
    return create_problem_response(
        status=500,
        title="Internal Server Error",
        detail="An unexpected internal error occurred.",
        error_type="internal-error",
        instance=request.url.path,
    )
