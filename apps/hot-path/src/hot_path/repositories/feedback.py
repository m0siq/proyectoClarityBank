"""FeedbackRepository — records transactions where L1 fell below threshold.

Container: feedback_loop
Partition key: /year_month  (e.g. "2026-05")
TTL: 6 months (set at container level in Bicep)

These records feed the monthly MLOps retraining pipeline.
"""

from __future__ import annotations

import uuid
from datetime import datetime, UTC

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError

from hot_path.core.logging import logger
from hot_path.domain.models import ClassificationL1, ClassificationL2, Transaction


class FeedbackRepository:
    """Stores L1→L2 fallback cases for model retraining."""

    def __init__(
        self,
        client: CosmosClient,
        database: str,
        container: str,
    ) -> None:
        self._container = client.get_database_client(database).get_container_client(container)

    async def record(
        self,
        tx: Transaction,
        l1: ClassificationL1,
        l2: ClassificationL2,
    ) -> None:
        """Persist a feedback record when L1 confidence < threshold."""
        now = datetime.now(UTC)
        year_month = now.strftime("%Y-%m")
        doc = {
            "id": str(uuid.uuid4()),
            "year_month": year_month,
            "transaction_id": str(tx.transaction_id),
            "merchant_raw": tx.merchant_raw,
            "l1_prediction": {
                "category": l1.category.value,
                "confidence": l1.confidence,
            },
            "l2_prediction": {
                "category": l2.category.value,
                "rationale": l2.rationale,
            },
            "captured_at": now.isoformat(),
        }
        try:
            await self._container.create_item(doc)
        except CosmosHttpResponseError as exc:
            # Non-critical: a failed feedback write does not affect the hot path
            logger.warning(
                "feedback_write_error",
                status=exc.status_code,
                transaction_id=str(tx.transaction_id),
            )
