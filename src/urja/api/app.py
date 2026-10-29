"""FastAPI application — entry point for the Urja API service."""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from urja.api.errors import (
    MeterNotFoundError,
    NodeNotFoundError,
    http_exception_handler,
    meter_not_found_handler,
    node_not_found_handler,
    portal_upstream_error_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from urja.api.v1.router import router as v1_router
from urja.client.exceptions import PortalError
from urja.logging import setup_logging

setup_logging()

app = FastAPI(
    title="Urja Meter Ops API",
    description=(
        "Clean, documented REST API wrapping the legacy Urja Meter Ops utility portal.\n\n"
        "### Key Capabilities:\n"
        "- **Meters List**: Query smart meters with cursor pagination and multi-parameter filters.\n"
        "- **Meter Detail**: Full nameplate, GPS coordinates, and 7-level hierarchy path (`Zone › Circle › Division › Subdivision › Substation › Feeder › DT`).\n"
        "- **Consumption**: 30-minute interval readings time series with date window filtering.\n"
        "- **Freshness Engine**: Per-resource TTLs, single-flight request coalescing, and stale-while-revalidate.\n"
        "- **Error Handling**: RFC 7807 Problem Details on all failure responses (`application/problem+json`)."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ── Register RFC 7807 Error Handlers ──────────────────────────────────
app.add_exception_handler(MeterNotFoundError, meter_not_found_handler)
app.add_exception_handler(NodeNotFoundError, node_not_found_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(PortalError, portal_upstream_error_handler)
app.add_exception_handler(httpx.HTTPError, portal_upstream_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

# ── Include Routers ───────────────────────────────────────────────────
app.include_router(v1_router)


@app.get("/health", tags=["ops"], summary="Liveness check")
async def root_health() -> dict[str, str]:
    """Root liveness probe check."""
    return {"status": "ok"}


@app.get("/scalar", include_in_schema=False)
async def scalar_docs() -> HTMLResponse:
    """Render interactive API documentation via Scalar."""
    html_content = """
    <!doctype html>
    <html>
      <head>
        <title>Urja Meter Ops API — Scalar Reference</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
      </head>
      <body>
        <script id="api-reference" data-url="/openapi.json"></script>
        <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
      </body>
    </html>
    """
    return HTMLResponse(content=html_content)
