"""Unit tests for OpenAIClassifier (SPEC §11.2).

6+ test cases using unittest.mock to avoid real API calls:
  1. Happy path — valid JSON response
  2. Timeout → Category.OTHER (fallback)
  3. RateLimitError (429) → Category.OTHER (fallback)
  4. Invalid JSON in response → Category.OTHER (parse error)
  5. Invented category (not in enum) → Category.OTHER (parse error)
  6. API returns valid health category
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from openai import APITimeoutError, RateLimitError

from hot_path.domain.models import Category, Transaction
from hot_path.services.classifier_l2 import OpenAIClassifier


def make_tx(merchant: str = "FARMACIA SAN MIGUEL", amount: str = "-12.50") -> Transaction:
    return Transaction(
        transaction_id=uuid4(),
        user_id="u_test",
        amount=Decimal(amount),
        merchant_raw=merchant,
        timestamp=datetime.now(UTC),
    )


def make_response(category: str, rationale: str = "test", model: str = "gpt-4o-mini") -> MagicMock:
    """Build a mock ChatCompletion response."""
    msg = MagicMock()
    msg.content = json.dumps({"category": category, "rationale": rationale})
    choice = MagicMock()
    choice.message = msg
    usage = MagicMock()
    usage.prompt_tokens = 50
    usage.completion_tokens = 20
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


@pytest.fixture
def classifier() -> OpenAIClassifier:
    """OpenAIClassifier with mocked Azure OpenAI client."""
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    clf = OpenAIClassifier(
        client=mock_client,
        deployment="gpt-4o-mini",
        model_version="gpt-4o-mini",
    )
    return clf


class TestOpenAIClassifier:
    @pytest.mark.asyncio
    async def test_happy_path_groceries(self, classifier: OpenAIClassifier) -> None:
        classifier._client.chat.completions.create = AsyncMock(
            return_value=make_response("groceries", "Supermercado reconocido")
        )
        result = await classifier.classify(make_tx("MERCADONA"))
        assert result.category == Category.GROCERIES
        assert "Supermercado" in result.rationale

    @pytest.mark.asyncio
    async def test_health_category(self, classifier: OpenAIClassifier) -> None:
        classifier._client.chat.completions.create = AsyncMock(
            return_value=make_response("health", "Farmacia identificada")
        )
        result = await classifier.classify(make_tx("FARMACIA SAN MIGUEL"))
        assert result.category == Category.HEALTH

    @pytest.mark.asyncio
    async def test_timeout_returns_fallback(self, classifier: OpenAIClassifier) -> None:
        classifier._client.chat.completions.create = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )
        result = await classifier.classify(make_tx("SLOW MERCHANT"))
        assert result.category == Category.OTHER
        assert result.rationale == "fallback failed"

    @pytest.mark.asyncio
    async def test_rate_limit_returns_fallback(self, classifier: OpenAIClassifier) -> None:
        classifier._client.chat.completions.create = AsyncMock(
            side_effect=RateLimitError(
                message="429 Too Many Requests",
                response=MagicMock(status_code=429, headers={}),
                body={},
            )
        )
        result = await classifier.classify(make_tx("BUSY MERCHANT"))
        assert result.category == Category.OTHER

    @pytest.mark.asyncio
    async def test_invalid_json_returns_parse_error(self, classifier: OpenAIClassifier) -> None:
        bad_msg = MagicMock()
        bad_msg.content = "This is not JSON at all!"
        bad_choice = MagicMock()
        bad_choice.message = bad_msg
        bad_response = MagicMock()
        bad_response.choices = [bad_choice]
        bad_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        classifier._client.chat.completions.create = AsyncMock(return_value=bad_response)
        result = await classifier.classify(make_tx("RANDOM"))
        assert result.category == Category.OTHER
        assert result.rationale == "parse error"

    @pytest.mark.asyncio
    async def test_invented_category_returns_other(self, classifier: OpenAIClassifier) -> None:
        """The LLM invents 'crypto' which is not in the Category enum."""
        classifier._client.chat.completions.create = AsyncMock(
            return_value=make_response("crypto", "Invented category")
        )
        result = await classifier.classify(make_tx("BINANCE"))
        assert result.category == Category.OTHER
