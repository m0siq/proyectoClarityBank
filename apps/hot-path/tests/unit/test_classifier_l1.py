"""Unit tests for FastTextClassifier (SPEC §11.2).

Uses unittest.mock to avoid loading a real fastText model.
5+ test cases:
  1. Valid label → correct category
  2. Invalid label → Category.OTHER with confidence=0.0
  3. Text normalization (accents, digits, special chars)
  4. Low confidence is passed through correctly
  5. High confidence is passed through correctly
"""

from __future__ import annotations

from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from hot_path.domain.models import Category, Transaction
from hot_path.services.classifier_l1 import FastTextClassifier


def make_tx(merchant: str = "MERCADONA BARCELONA") -> Transaction:
    return Transaction(
        transaction_id=uuid4(),
        user_id="u_test",
        amount=Decimal("-42.50"),
        merchant_raw=merchant,
        timestamp=datetime.now(UTC),
    )


@pytest.fixture
def fake_model() -> MagicMock:
    """A mock fasttext model."""
    model = MagicMock()
    model.predict.return_value = (["__label__groceries"], [0.97])
    return model


@pytest.fixture
def classifier(fake_model: MagicMock) -> FastTextClassifier:
    """FastTextClassifier with a mocked model (no disk I/O)."""
    with patch("fasttext.load_model", return_value=fake_model):
        clf = FastTextClassifier(model_path="/fake/model.bin", model_version="test-v1")
    return clf


class TestFastTextClassifier:
    def test_valid_label_returns_category(
        self, classifier: FastTextClassifier, fake_model: MagicMock
    ) -> None:
        fake_model.predict.return_value = (["__label__groceries"], [0.94])
        result = classifier.classify(make_tx("MERCADONA BARCELONA"))
        assert result.category == Category.GROCERIES
        assert result.confidence == pytest.approx(0.94, abs=0.001)
        assert result.model_version == "test-v1"

    def test_invalid_label_returns_other_with_zero_confidence(
        self, classifier: FastTextClassifier, fake_model: MagicMock
    ) -> None:
        """Unknown label from a newer model should return OTHER with confidence=0."""
        fake_model.predict.return_value = (["__label__crypto"], [0.88])
        result = classifier.classify(make_tx("BINANCE TRADE"))
        assert result.category == Category.OTHER
        assert result.confidence == 0.0

    def test_normalization_removes_accents_and_digits(self) -> None:
        """Static normalization method should strip accents, digits, special chars."""
        normalized = FastTextClassifier._normalize("Farmacía Núm. 123 S.L.")
        assert "1" not in normalized
        assert "2" not in normalized
        assert "3" not in normalized
        assert "á" not in normalized
        assert "ú" not in normalized
        assert normalized == normalized.lower()

    def test_low_confidence_passed_through(
        self, classifier: FastTextClassifier, fake_model: MagicMock
    ) -> None:
        fake_model.predict.return_value = (["__label__other"], [0.40])
        result = classifier.classify(make_tx("RANDOM MERCHANT XYZ"))
        assert result.category == Category.OTHER
        assert result.confidence == pytest.approx(0.40, abs=0.001)

    def test_high_confidence_passed_through(
        self, classifier: FastTextClassifier, fake_model: MagicMock
    ) -> None:
        fake_model.predict.return_value = (["__label__transport"], [0.99])
        result = classifier.classify(make_tx("RENFE CERCANIAS"))
        assert result.category == Category.TRANSPORT
        assert result.confidence == pytest.approx(0.99, abs=0.001)
