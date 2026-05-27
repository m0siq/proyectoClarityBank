"""Domain models — all immutable Pydantic v2 models.

Per SPEC §5.4 all models use frozen=True.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class Category(StrEnum):
    """Financial transaction categories.

    These are the only valid categories. The enum prevents the LLM from
    inventing new categories (SPEC §5.4).
    """

    GROCERIES = "groceries"
    TRANSPORT = "transport"
    LEISURE = "leisure"
    HOUSING = "housing"
    HEALTH = "health"
    UTILITIES = "utilities"
    INCOME = "income"
    TRANSFERS = "transfers"
    OTHER = "other"


class Transaction(BaseModel):
    """Raw transaction as received from Event Hubs."""

    model_config = {"frozen": True}

    transaction_id: UUID
    user_id: str
    amount: Decimal  # negative = expense, positive = income
    currency: str = "EUR"
    merchant_raw: str  # raw merchant text — what fastText classifies
    merchant_mcc: str | None = None  # Merchant Category Code if available
    timestamp: datetime  # when it happened at the bank, not when it arrived


class UserProfile(BaseModel):
    """Statistical profile of a user's spending habits."""

    model_config = {"frozen": True}

    user_id: str
    mean_spend: Decimal
    stddev_spend: Decimal
    transactions_count: int
    top_merchants: list[str] = Field(default_factory=list, max_length=20)
    updated_at: datetime


class AnomalyResult(BaseModel):
    """Result of the Z-Score anomaly detection."""

    model_config = {"frozen": True}

    is_anomaly: bool
    z_score: float
    reason: str | None = None  # human-readable, shown in app


class ClassificationL1(BaseModel):
    """fastText classification result."""

    model_config = {"frozen": True}

    category: Category
    confidence: float = Field(..., ge=0.0, le=1.0)
    model_version: str  # e.g. "fasttext-2026-03-15"


class ClassificationL2(BaseModel):
    """Azure OpenAI classification result."""

    model_config = {"frozen": True}

    category: Category
    rationale: str  # LLM explanation, for auditing
    model_version: str  # e.g. "gpt-4o-mini-2024-07-18"
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int


class TransactionProcessed(BaseModel):
    """Final enriched transaction stored in Cosmos DB."""

    model_config = {"frozen": True}

    transaction: Transaction
    category: Category
    final_classifier: str  # "l1" or "l2"
    confidence: float
    anomaly: AnomalyResult
    processed_at: datetime
    pipeline_latency_ms: int
