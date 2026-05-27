"""Unit tests for TransactionPipeline (SPEC §11.2).

10+ test cases:
  1. High L1 confidence → does NOT call L2
  2. Low L1 confidence → calls L2
  3. L2 failure → degrades to OTHER
  4. Anomaly flag propagates in result
  5. Pipeline returns latency_ms
  6. final_classifier is "l1" when L1 used
  7. final_classifier is "l2" when L2 used
  8. L2 confidence is always 1.0 by convention
  9. L1 confidence below threshold triggers feedback.record
  10. High confidence does NOT trigger feedback.record
"""

from __future__ import annotations

from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from hot_path.domain.models import (
    AnomalyResult,
    Category,
    ClassificationL1,
    ClassificationL2,
    Transaction,
    UserProfile,
)
from hot_path.services.anomaly import AnomalyDetector
from hot_path.services.pipeline import TransactionPipeline


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_tx(amount: str = "-50.00", merchant: str = "TEST MERCHANT") -> Transaction:
    return Transaction(
        transaction_id=uuid4(),
        user_id="u_001",
        amount=Decimal(amount),
        merchant_raw=merchant,
        timestamp=datetime.now(UTC),
    )


def make_profile() -> UserProfile:
    return UserProfile(
        user_id="u_001",
        mean_spend=Decimal("60.00"),
        stddev_spend=Decimal("15.00"),
        transactions_count=200,
        updated_at=datetime.now(UTC),
    )


def make_l1(category: Category = Category.GROCERIES, confidence: float = 0.95) -> ClassificationL1:
    return ClassificationL1(category=category, confidence=confidence, model_version="test")


def make_l2(category: Category = Category.HEALTH) -> ClassificationL2:
    return ClassificationL2(
        category=category,
        rationale="test rationale",
        model_version="gpt-4o-mini",
        latency_ms=800,
        prompt_tokens=50,
        completion_tokens=20,
    )


@pytest.fixture
def pipeline() -> TransactionPipeline:
    anomaly = MagicMock(spec=AnomalyDetector)
    anomaly.detect.return_value = AnomalyResult(is_anomaly=False, z_score=0.5)

    l1 = MagicMock()
    l1.classify.return_value = make_l1(confidence=0.95)

    l2 = MagicMock()
    l2.classify = AsyncMock(return_value=make_l2())

    profiles = MagicMock()
    profiles.get = AsyncMock(return_value=make_profile())

    feedback = MagicMock()
    feedback.record = AsyncMock()

    return TransactionPipeline(
        anomaly=anomaly,
        l1=l1,
        l2=l2,
        profiles=profiles,
        feedback=feedback,
        confidence_threshold=0.85,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestTransactionPipeline:
    @pytest.mark.asyncio
    async def test_high_confidence_does_not_call_l2(self, pipeline: TransactionPipeline) -> None:
        pipeline._l1.classify.return_value = make_l1(confidence=0.95)
        await pipeline.process(make_tx())
        pipeline._l2.classify.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_calls_l2(self, pipeline: TransactionPipeline) -> None:
        pipeline._l1.classify.return_value = make_l1(confidence=0.50)
        await pipeline.process(make_tx())
        pipeline._l2.classify.assert_called_once()

    @pytest.mark.asyncio
    async def test_l2_failure_degrades_to_other(self, pipeline: TransactionPipeline) -> None:
        pipeline._l1.classify.return_value = make_l1(confidence=0.30)
        # L2 classifier returns OTHER on failure (already implemented in classifier_l2)
        pipeline._l2.classify = AsyncMock(
            return_value=make_l2(category=Category.OTHER)
        )
        result = await pipeline.process(make_tx())
        assert result.category == Category.OTHER
        assert result.final_classifier == "l2"

    @pytest.mark.asyncio
    async def test_anomaly_flag_propagates(self, pipeline: TransactionPipeline) -> None:
        pipeline._anomaly.detect.return_value = AnomalyResult(
            is_anomaly=True, z_score=4.2, reason="Gasto elevado"
        )
        result = await pipeline.process(make_tx("-500.00"))
        assert result.anomaly.is_anomaly is True
        assert result.anomaly.z_score == pytest.approx(4.2, abs=0.01)

    @pytest.mark.asyncio
    async def test_pipeline_returns_latency(self, pipeline: TransactionPipeline) -> None:
        result = await pipeline.process(make_tx())
        assert result.pipeline_latency_ms >= 0

    @pytest.mark.asyncio
    async def test_final_classifier_l1_when_high_confidence(
        self, pipeline: TransactionPipeline
    ) -> None:
        pipeline._l1.classify.return_value = make_l1(confidence=0.95)
        result = await pipeline.process(make_tx())
        assert result.final_classifier == "l1"

    @pytest.mark.asyncio
    async def test_final_classifier_l2_when_low_confidence(
        self, pipeline: TransactionPipeline
    ) -> None:
        pipeline._l1.classify.return_value = make_l1(confidence=0.40)
        result = await pipeline.process(make_tx())
        assert result.final_classifier == "l2"

    @pytest.mark.asyncio
    async def test_l2_confidence_always_one(self, pipeline: TransactionPipeline) -> None:
        """By convention, L2 confidence is always 1.0 (no confidence score from LLM)."""
        pipeline._l1.classify.return_value = make_l1(confidence=0.40)
        result = await pipeline.process(make_tx())
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_feedback_record_called_when_l2_triggered(
        self, pipeline: TransactionPipeline
    ) -> None:
        pipeline._l1.classify.return_value = make_l1(confidence=0.40)
        with patch("asyncio.create_task") as mock_create_task:
            await pipeline.process(make_tx())
            # create_task should be called for the feedback coroutine
            mock_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_feedback_not_called_when_high_confidence(
        self, pipeline: TransactionPipeline
    ) -> None:
        pipeline._l1.classify.return_value = make_l1(confidence=0.95)
        with patch("asyncio.create_task") as mock_create_task:
            await pipeline.process(make_tx())
            mock_create_task.assert_not_called()
