"""Shared Transaction schema — importable by any service in the monorepo.

Kept separate from hot_path.domain.models so cold-path and mlops-pipeline
can import it without depending on the full hot-path package.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class Category(StrEnum):
    GROCERIES = "groceries"
    TRANSPORT = "transport"
    LEISURE = "leisure"
    HOUSING = "housing"
    HEALTH = "health"
    UTILITIES = "utilities"
    INCOME = "income"
    TRANSFERS = "transfers"
    OTHER = "other"


class TransactionSchema(BaseModel):
    """Canonical transaction schema shared across all services."""

    model_config = {"frozen": True}

    transaction_id: UUID
    user_id: str
    amount: Decimal
    currency: str = "EUR"
    merchant_raw: str
    merchant_mcc: str | None = None
    timestamp: datetime
