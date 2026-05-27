"""Azure Event Hubs consumer (SPEC §5.10).

The EventHubConsumerClient is injected (built by azure_clients.py with
DefaultAzureCredential), so this class only owns the processing logic.

Strategy:
  - receive_batch: max 50 messages or 5 s wait (SPEC §5.10).
  - Per-partition ordering preserved.
  - Error handling modes:
      * fail-loud (default): on pipeline failure, do NOT checkpoint; message
        will be redelivered after lease timeout.
      * dead-letter mode: write raw message + traceback to Cosmos DLQ and advance.
        Enabled by config.dead_letter_mode.
"""

from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from azure.eventhub import EventData
from azure.eventhub.aio import EventHubConsumerClient

from hot_path.core.config import Settings
from hot_path.core.logging import logger
from hot_path.domain.models import Transaction
from hot_path.repositories.profiles import ProfileRepository
from hot_path.repositories.transactions import TransactionRepository
from hot_path.services.pipeline import TransactionPipeline


class TransactionConsumer:
    """Async Event Hub consumer that drives the processing pipeline."""

    def __init__(
        self,
        config: Settings,
        pipeline: TransactionPipeline,
        transactions_repo: TransactionRepository,
        profiles_repo: ProfileRepository,
        cosmos_client: Any = None,       # for DLQ in dead-letter mode
        eh_client: EventHubConsumerClient | None = None,  # injected from azure_clients
    ) -> None:
        self._cfg = config
        self._pipeline = pipeline
        self._transactions_repo = transactions_repo
        self._profiles_repo = profiles_repo
        self._cosmos_client = cosmos_client
        self._eh_client = eh_client  # pre-built with Managed Identity

    async def run(self) -> None:
        """Start consuming from Event Hubs. Runs until cancelled."""
        if self._eh_client is None:
            raise RuntimeError(
                "EventHubConsumerClient not provided. "
                "Use azure_clients.create_event_hub_consumer() and inject the result."
            )

        logger.info(
            "consumer_starting",
            namespace=self._cfg.event_hub_namespace,
            event_hub=self._cfg.event_hub_name,
            consumer_group=self._cfg.event_hub_consumer_group,
        )

        async with self._eh_client:
            await self._eh_client.receive_batch(
                on_event_batch=self._on_batch,
                max_batch_size=50,
                max_wait_time=5,
                # DECISION: 2026-05-27 — @latest on fresh boot so dev runs don't
                # reprocess old test messages. In prod after a crash the checkpoint
                # store (Blob) picks up from the last committed offset automatically.
                starting_position="@latest",
            )

    async def _on_batch(
        self,
        partition_context: Any,
        events: list[EventData],
    ) -> None:
        """Process a batch of events from a single partition in order."""
        partition_id = partition_context.partition_id
        structlog.contextvars.bind_contextvars(partition_id=partition_id)

        for event in events:
            await self._process_event(event)

        # Checkpoint after the full batch — balances durability vs. blob RU cost
        await partition_context.update_checkpoint()

    async def _process_event(self, event: EventData) -> None:
        """Process a single event through the 4-stage pipeline."""
        raw_body = event.body_as_str(encoding="UTF-8")

        # ── Parse ─────────────────────────────────────────────────────────────
        try:
            tx = Transaction.model_validate_json(raw_body)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "event_deserialize_error",
                error=str(exc),
                raw_body=raw_body[:500],
            )
            if self._cfg.dead_letter_mode:
                await self._dead_letter(raw_body, exc)
            return  # always advance past unparseable messages

        structlog.contextvars.bind_contextvars(
            transaction_id=str(tx.transaction_id),
            user_id=tx.user_id,
        )

        # ── Pipeline + persist ────────────────────────────────────────────────
        try:
            processed = await self._pipeline.process(tx)
            await self._transactions_repo.save(processed)
            asyncio.create_task(
                self._profiles_repo.update_stats(tx.user_id, tx.amount),
                name=f"profile_{tx.user_id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pipeline_error",
                error=str(exc),
                transaction_id=str(tx.transaction_id),
                traceback=traceback.format_exc(),
            )
            if self._cfg.dead_letter_mode:
                await self._dead_letter(raw_body, exc)
                return
            raise  # fail-loud: no checkpoint, re-deliver

        structlog.contextvars.unbind_contextvars("transaction_id", "user_id")

    async def _dead_letter(self, raw_body: str, exc: Exception) -> None:
        """Write a failed message to the DLQ container in Cosmos DB."""
        if self._cosmos_client is None:
            logger.warning("dlq_skipped_no_cosmos_client")
            return
        try:
            db = self._cosmos_client.get_database_client(self._cfg.cosmos_database)
            container = db.get_container_client("dlq")
            doc = {
                "id": str(uuid.uuid4()),
                "raw_body": raw_body[:2000],
                "error": str(exc),
                "traceback": traceback.format_exc()[:4000],
                "captured_at": datetime.now(UTC).isoformat(),
            }
            await container.create_item(doc)
        except Exception as dlq_exc:  # noqa: BLE001
            logger.error("dlq_write_error", error=str(dlq_exc))
