"""Transaction pipeline orchestrator (SPEC §5.9).

Composes the 4 stages:
  A. Anomaly detection (Z-Score)
  B. L1 classification (fastText)
  C. Conditional L2 classification (Azure OpenAI) if L1 confidence < threshold
  D. Feedback recording (fire-and-forget)

This class performs NO I/O directly — all I/O is injected via repositories.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from hot_path.core.logging import logger
from hot_path.core.telemetry import (
    record_l1_confidence,
    record_pipeline_latency,
    record_tx_processed,
)
from hot_path.domain.models import Transaction, TransactionProcessed
from hot_path.repositories.feedback import FeedbackRepository
from hot_path.repositories.profiles import ProfileRepository
from hot_path.services.anomaly import AnomalyDetector
from hot_path.services.classifier_l1 import FastTextClassifier
from hot_path.services.classifier_l2 import OpenAIClassifier


class TransactionPipeline:
    """Orchestrates the full transaction processing pipeline."""

    def __init__(
        self,
        anomaly: AnomalyDetector,
        l1: FastTextClassifier,
        l2: OpenAIClassifier,
        profiles: ProfileRepository,
        feedback: FeedbackRepository,
        confidence_threshold: float,
    ) -> None:
        self._anomaly = anomaly
        self._l1 = l1
        self._l2 = l2
        self._profiles = profiles
        self._feedback = feedback
        self._threshold = confidence_threshold

    async def process(self, tx: Transaction) -> TransactionProcessed:
        """Process a single transaction through the 4-stage pipeline.

        Returns a TransactionProcessed ready to be persisted in Cosmos.
        """
        t0 = time.perf_counter()

        # Stage A: Anomaly detection
        profile = await self._profiles.get(tx.user_id)
        anomaly_result = self._anomaly.detect(tx, profile)

        # Stage B: L1 classification (synchronous, in-memory, < 5ms)
        l1_result = self._l1.classify(tx)
        record_l1_confidence(l1_result.confidence)

        # Stage C: Conditional L2 fallback
        final_category = l1_result.category
        final_classifier = "l1"
        final_confidence = l1_result.confidence

        if l1_result.confidence < self._threshold:
            l2_result = await self._l2.classify(tx)
            final_category = l2_result.category
            final_classifier = "l2"
            final_confidence = 1.0  # by convention, L2 does not expose confidence score

            # Stage D: Fire-and-forget feedback for MLOps retraining
            asyncio.create_task(
                self._feedback.record(tx, l1_result, l2_result),
                name=f"feedback_{tx.transaction_id}",
            )

        latency_ms = int((time.perf_counter() - t0) * 1000)

        # Telemetry
        record_tx_processed(
            final_classifier=final_classifier,
            is_anomaly=anomaly_result.is_anomaly,
        )
        record_pipeline_latency(latency_ms)

        logger.info(
            "transaction_processed",
            transaction_id=str(tx.transaction_id),
            category=final_category.value,
            classifier=final_classifier,
            confidence=final_confidence,
            is_anomaly=anomaly_result.is_anomaly,
            latency_ms=latency_ms,
        )

        return TransactionProcessed(
            transaction=tx,
            category=final_category,
            final_classifier=final_classifier,
            confidence=final_confidence,
            anomaly=anomaly_result,
            processed_at=datetime.now(UTC),
            pipeline_latency_ms=latency_ms,
        )
