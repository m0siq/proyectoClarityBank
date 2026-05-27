"""FastAPI application factory (SPEC §5.11)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import orjson
from fastapi import FastAPI, Request, Response
from fastapi.responses import ORJSONResponse

from hot_path.core.config import Settings
from hot_path.api.routes import health, classify

if TYPE_CHECKING:
    from hot_path.services.pipeline import TransactionPipeline


def build_api(settings: Settings, pipeline: "TransactionPipeline") -> FastAPI:
    """Construct and configure the FastAPI application."""
    app = FastAPI(
        title="Hot-Path Transaction Categorizer",
        version="0.1.0",
        default_response_class=ORJSONResponse,
        docs_url="/docs" if settings.enable_sync_api else None,
        redoc_url=None,
    )

    # Attach shared state
    app.state.settings = settings
    app.state.pipeline = pipeline
    app.state.start_time = time.time()

    # Routers
    app.include_router(health.router)
    if settings.enable_sync_api:
        app.include_router(classify.router, prefix="/v1")

    return app
