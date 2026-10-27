"""FastAPI application — entry point for the Urja API service."""

from fastapi import FastAPI

from urja.logging import setup_logging

setup_logging()

app = FastAPI(
    title="Urja Meter Ops API",
    description=(
        "Clean REST API wrapping the legacy Urja Meter Ops portal. "
        "Provides access to smart-meter details, consumption readings, "
        "distribution transformers, and the network hierarchy."
    ),
    version="0.1.0",
)


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Basic liveness check."""
    return {"status": "ok"}
