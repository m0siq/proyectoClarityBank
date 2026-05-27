"""TransactionRepository — persists processed transactions to Cosmos DB.

Container: transactions
Partition key: /user_id
TTL: 13 months (set at container level in Bicep)
"""

from __future__ import annotations

import time

import orjson
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError

from hot_path.core.logging import logger
from hot_path.core.telemetry import record_cosmos_write_latency
from hot_path.domain.models import TransactionProcessed


class TransactionRepository:
    """Thin wrapper around the transactions Cosmos container."""

    def __init__(
        self,
        client: CosmosClient,
        database: str,
        container: str,
    ) -> None:
        self._container = client.get_database_client(database).get_container_client(container)

    async def save(self, processed: TransactionProcessed) -> None:
        """Persist a processed transaction document."""
        doc = _to_document(processed)
        t0 = time.perf_counter()
        try:
            await self._container.upsert_item(doc)
            latency_ms = int((time.perf_counter() - t0) * 1000)
            record_cosmos_write_latency(latency_ms)
        except CosmosHttpResponseError as exc:
            logger.error(
                "cosmos_write_error",
                container="transactions",
                status=exc.status_code,
                transaction_id=str(processed.transaction.transaction_id),
            )
            raise


def _to_document(processed: TransactionProcessed) -> dict:  # type: ignore[type-arg]
    """Convert a TransactionProcessed into a Cosmos-ready dict.

    Cosmos requires `id` to be a string.
    """
    tx = processed.transaction
    return {
        "id": str(tx.transaction_id),
        "user_id": tx.user_id,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "merchant_raw": tx.merchant_raw,
        "merchant_mcc": tx.merchant_mcc,
        "timestamp": tx.timestamp.isoformat(),
        "category": processed.category.value,
        "final_classifier": processed.final_classifier,
        "confidence": processed.confidence,
        "anomaly": {
            "is_anomaly": processed.anomaly.is_anomaly,
            "z_score": processed.anomaly.z_score,
            "reason": processed.anomaly.reason,
        },
        "processed_at": processed.processed_at.isoformat(),
        "pipeline_latency_ms": processed.pipeline_latency_ms,
    }
