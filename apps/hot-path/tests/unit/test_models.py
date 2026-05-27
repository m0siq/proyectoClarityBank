"""Unit tests for Pydantic domain models (SPEC §11.2)."""

from __future__ import annotations

from datetime import datetime, UTC
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hot_path.domain.models import (
    AnomalyResult,
    Category,
    ClassificationL1,
    Transaction,
    TransactionProcessed,
    UserProfile,
)


class TestCategory:
    def test_all_categories_accessible(self) -> None:
        assert Category.GROCERIES == "groceries"
        assert Category.TRANSPORT == "transport"

    def test_invalid_category_raises(self) -> None:
        with pytest.raises(ValueError):
            Category("crypto")


class TestTransaction:
    def test_valid_transaction(self) -> None:
        tx = Transaction(
            transaction_id=uuid4(),
            user_id="u_1",
            amount=Decimal("-42.50"),
            merchant_raw="MERCADONA",
            timestamp=datetime.now(UTC),
        )
        assert tx.currency == "EUR"  # default

    def test_frozen_model_cannot_be_mutated(self) -> None:
        tx = Transaction(
            transaction_id=uuid4(),
            user_id="u_1",
            amount=Decimal("-10"),
            merchant_raw="TEST",
            timestamp=datetime.now(UTC),
        )
        with pytest.raises(Exception):  # ValidationError or AttributeError depending on pydantic
            tx.user_id = "changed"  # type: ignore[misc]

    def test_positive_amount_allowed(self) -> None:
        tx = Transaction(
            transaction_id=uuid4(),
            user_id="u_1",
            amount=Decimal("1500.00"),
            merchant_raw="NOMINA EMPRESA",
            timestamp=datetime.now(UTC),
        )
        assert tx.amount > 0


class TestClassificationL1:
    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ClassificationL1(category=Category.GROCERIES, confidence=1.5, model_version="v1")

    def test_valid_classification(self) -> None:
        clf = ClassificationL1(
            category=Category.TRANSPORT,
            confidence=0.92,
            model_version="fasttext-2026-05",
        )
        assert clf.category == Category.TRANSPORT


class TestUserProfile:
    def test_empty_top_merchants_default(self) -> None:
        profile = UserProfile(
            user_id="u_1",
            mean_spend=Decimal("100"),
            stddev_spend=Decimal("20"),
            transactions_count=50,
            updated_at=datetime.now(UTC),
        )
        assert profile.top_merchants == []
