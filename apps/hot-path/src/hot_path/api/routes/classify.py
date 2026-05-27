"""Synchronous classify endpoint (dev/testing only, SPEC §5.11).

Only enabled when enable_sync_api=True.
Allows testing the pipeline without Event Hubs.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import ORJSONResponse

from hot_path.domain.models import Transaction, TransactionProcessed

router = APIRouter(tags=["classify"])


@router.post(
    "/classify",
    response_model=TransactionProcessed,
    summary="Synchronous classify (dev only)",
)
async def classify(request: Request, transaction: Transaction) -> ORJSONResponse:
    """Process a single transaction synchronously.

    Only available when HOTPATH_ENABLE_SYNC_API=true.
    Used for integration tests and local development.
    """
    pipeline = request.app.state.pipeline
    processed = await pipeline.process(transaction)
    return ORJSONResponse(processed.model_dump(mode="json"))
