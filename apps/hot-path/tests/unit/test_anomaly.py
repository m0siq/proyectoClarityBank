"""Unit tests for AnomalyDetector — TDD as required by SPEC §11.2.

8+ test cases covering:
  1. Normal spend within threshold
  2. Extreme spend above threshold
  3. Income (positive amount) is never anomaly
  4. std=0 never returns anomaly
  5. Adjustable threshold
  6. Zero amount
  7. Exactly at threshold boundary
  8. Negative z-score (spend below mean)
"""

from __future__ import annotations

from datetime import datetime, UTC
from decimal import Decimal
from uuid import uuid4

import pytest

from hot_path.domain.models import AnomalyResult, Transaction, UserProfile
from hot_path.services.anomaly import AnomalyDetector


def make_tx(amount: str = "-100.00", merchant: str = "TEST") -> Transaction:
    return Transaction(
        transaction_id=uuid4(),
        user_id="u_test",
        amount=Decimal(amount),
        merchant_raw=merchant,
        timestamp=datetime.now(UTC),
    )


def make_profile(mean: str = "100.00", std: str = "20.00", count: int = 100) -> UserProfile:
    return UserProfile(
        user_id="u_test",
        mean_spend=Decimal(mean),
        stddev_spend=Decimal(std),
        transactions_count=count,
        updated_at=datetime.now(UTC),
    )


class TestAnomalyDetector:
    def setup_method(self) -> None:
        self.detector = AnomalyDetector(threshold=3.0)

    def test_normal_spend_not_anomaly(self) -> None:
        """Spend within 1σ of mean should not be anomaly."""
        tx = make_tx("-110.00")
        profile = make_profile(mean="100.00", std="20.00")
        result = self.detector.detect(tx, profile)
        assert result.is_anomaly is False
        assert result.z_score == pytest.approx(0.5, abs=0.01)

    def test_extreme_spend_is_anomaly(self) -> None:
        """Spend 4σ above mean should be flagged."""
        tx = make_tx("-180.00")  # z = (180-100)/20 = 4.0
        profile = make_profile(mean="100.00", std="20.00")
        result = self.detector.detect(tx, profile)
        assert result.is_anomaly is True
        assert result.z_score == pytest.approx(4.0, abs=0.01)
        assert result.reason is not None
        assert "180" in result.reason

    def test_income_never_anomaly(self) -> None:
        """Positive amounts (income) must never be flagged."""
        tx = make_tx("5000.00")  # large income
        profile = make_profile(mean="100.00", std="20.00")
        result = self.detector.detect(tx, profile)
        assert result.is_anomaly is False
        assert result.z_score == 0.0

    def test_zero_amount_not_anomaly(self) -> None:
        """Zero amount is treated as non-expense."""
        tx = make_tx("0.00")
        profile = make_profile()
        result = self.detector.detect(tx, profile)
        assert result.is_anomaly is False

    def test_std_zero_not_anomaly(self) -> None:
        """When std_dev=0 (new user), never flag anomaly (avoid division by zero)."""
        tx = make_tx("-999.99")
        profile = make_profile(mean="50.00", std="0.00", count=1)
        result = self.detector.detect(tx, profile)
        assert result.is_anomaly is False
        assert result.z_score == 0.0

    def test_threshold_adjustable(self) -> None:
        """Custom threshold of 2σ should flag spend at 2.5σ."""
        detector = AnomalyDetector(threshold=2.0)
        tx = make_tx("-150.00")  # z = (150-100)/20 = 2.5
        profile = make_profile(mean="100.00", std="20.00")
        result = detector.detect(tx, profile)
        assert result.is_anomaly is True

    def test_exactly_at_threshold_not_anomaly(self) -> None:
        """Spend exactly at threshold (z=3.0) is NOT anomaly (strict >)."""
        tx = make_tx("-160.00")  # z = (160-100)/20 = 3.0 exactly
        profile = make_profile(mean="100.00", std="20.00")
        result = self.detector.detect(tx, profile)
        assert result.is_anomaly is False

    def test_z_score_below_mean_not_anomaly(self) -> None:
        """Spend below mean gives negative-ish z-score — never anomaly."""
        tx = make_tx("-50.00")  # z = (50-100)/20 = -2.5
        profile = make_profile(mean="100.00", std="20.00")
        result = self.detector.detect(tx, profile)
        assert result.is_anomaly is False
        assert result.z_score < 0
