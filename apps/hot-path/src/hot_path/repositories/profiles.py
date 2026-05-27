"""ProfileRepository — reads and updates user spending profiles.

Container: user_profiles
Partition key: /user_id
TTL: disabled (profiles are persistent)

Caching strategy: in-memory LRU cache with TTL=5min (config.profile_cache_ttl_seconds).
This prevents hammering Cosmos DB on every transaction for the same user.

Profile update policy: fire-and-forget every N=10 transactions.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, UTC
from decimal import Decimal

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError

from hot_path.core.logging import logger
from hot_path.domain.models import UserProfile

# DECISION: 2026-05-27 — simple dict cache with timestamp check.
# TTL eviction is per-entry on access (lazy expiry). Good enough for
# a single-process monolith. If we ever horizontally scale, consider
# Redis. Reversible in repositories/profiles.py.
_cache: dict[str, tuple[UserProfile, float]] = {}
_UPDATE_EVERY_N = 10  # recalculate profile stats every N transactions
_pending_counts: dict[str, int] = {}  # user_id -> tx count since last update


class ProfileRepository:
    """Manage user spending profiles with TTL-based memory caching."""

    def __init__(
        self,
        client: CosmosClient,
        database: str,
        container: str,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._container = client.get_database_client(database).get_container_client(container)
        self._ttl = cache_ttl_seconds

    async def get(self, user_id: str) -> UserProfile:
        """Return the user profile, from cache if fresh, else from Cosmos.

        If the profile does not exist, creates and returns an empty default.
        Per SPEC §5.14: missing profile → empty profile with mean=0, std=0.
        """
        now = time.monotonic()
        if user_id in _cache:
            profile, cached_at = _cache[user_id]
            if (now - cached_at) < self._ttl:
                return profile

        try:
            item = await self._container.read_item(item=user_id, partition_key=user_id)
            profile = _from_document(item)
        except CosmosResourceNotFoundError:
            profile = _empty_profile(user_id)
            # Persist the empty profile so subsequent reads find it
            asyncio.create_task(self._save(profile))
        except CosmosHttpResponseError as exc:
            logger.error(
                "cosmos_profile_read_error",
                user_id=user_id,
                status=exc.status_code,
            )
            # Degrade gracefully: return empty profile
            profile = _empty_profile(user_id)

        _cache[user_id] = (profile, now)
        return profile

    async def update_stats(self, user_id: str, amount: Decimal) -> None:
        """Increment the pending counter and recalculate every N transactions.

        This is called fire-and-forget from the consumer.
        """
        _pending_counts[user_id] = _pending_counts.get(user_id, 0) + 1
        if _pending_counts[user_id] < _UPDATE_EVERY_N:
            return

        _pending_counts[user_id] = 0

        try:
            # Read current profile for recalculation
            item = await self._container.read_item(item=user_id, partition_key=user_id)
            profile = _from_document(item)
        except (CosmosResourceNotFoundError, CosmosHttpResponseError):
            return  # nothing to update

        # Welford's online algorithm for mean and variance update
        n = profile.transactions_count + 1
        old_mean = float(profile.mean_spend)
        spend = abs(float(amount)) if amount < 0 else 0.0
        new_mean = old_mean + (spend - old_mean) / n

        # Approximate stddev update (simplified; full Welford requires M2 state)
        # DECISION: 2026-05-27 — storing M2 in Cosmos adds complexity. Use an
        # approximation: recalculate from persisted mean and stddev. Reversible
        # if accuracy matters more.
        old_std = float(profile.stddev_spend)
        new_std = max(0.0, old_std + (abs(spend - old_mean) - old_std) / n)

        updated = UserProfile(
            user_id=user_id,
            mean_spend=Decimal(str(round(new_mean, 4))),
            stddev_spend=Decimal(str(round(new_std, 4))),
            transactions_count=n,
            top_merchants=profile.top_merchants,
            updated_at=datetime.now(UTC),
        )
        await self._save(updated)
        # Invalidate cache so next read gets fresh data
        _cache.pop(user_id, None)

    async def _save(self, profile: UserProfile) -> None:
        doc = _to_document(profile)
        try:
            await self._container.upsert_item(doc)
        except CosmosHttpResponseError as exc:
            logger.error(
                "cosmos_profile_write_error",
                user_id=profile.user_id,
                status=exc.status_code,
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_profile(user_id: str) -> UserProfile:
    return UserProfile(
        user_id=user_id,
        mean_spend=Decimal("0"),
        stddev_spend=Decimal("0"),
        transactions_count=0,
        top_merchants=[],
        updated_at=datetime.now(UTC),
    )


def _from_document(item: dict) -> UserProfile:  # type: ignore[type-arg]
    return UserProfile(
        user_id=item["user_id"],
        mean_spend=Decimal(str(item["mean_spend"])),
        stddev_spend=Decimal(str(item["stddev_spend"])),
        transactions_count=item["transactions_count"],
        top_merchants=item.get("top_merchants", []),
        updated_at=datetime.fromisoformat(item["updated_at"]),
    )


def _to_document(profile: UserProfile) -> dict:  # type: ignore[type-arg]
    return {
        "id": profile.user_id,
        "user_id": profile.user_id,
        "mean_spend": str(profile.mean_spend),
        "stddev_spend": str(profile.stddev_spend),
        "transactions_count": profile.transactions_count,
        "top_merchants": profile.top_merchants,
        "updated_at": profile.updated_at.isoformat(),
    }
