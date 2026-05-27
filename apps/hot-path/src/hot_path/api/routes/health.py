"""Health and readiness probes (SPEC §5.11)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> ORJSONResponse:
    """Returns 200 as long as the process is alive."""
    return ORJSONResponse({"status": "ok"})


@router.get("/ready", summary="Readiness probe")
async def ready(request: Request) -> ORJSONResponse:
    """Returns 200 when the service is ready to process transactions.

    Checks:
      - fastText model is loaded (pipeline exists on app state)
      - Cosmos connectivity is operational (settings exist)

    DECISION: 2026-05-27 — we don't do a live Cosmos ping on every /ready
    call to avoid adding read RU/s. Instead we check that the pipeline object
    is initialized (which requires Cosmos at boot). A full live check would be
    more rigorous but adds unnecessary overhead for frequent probe calls.
    """
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        return ORJSONResponse({"status": "not_ready", "reason": "pipeline_not_initialized"}, status_code=503)

    return ORJSONResponse({"status": "ready"})
