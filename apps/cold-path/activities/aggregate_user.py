"""Aggregate user spending from Cosmos DB transactions container."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, UTC

import azure.functions as func
import azure.durable_functions as df

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential


async def aggregate_user_activity(user_id: str, year_month: str) -> dict:
    """Query Cosmos DB and aggregate spending by category for a user/month.

    Returns a dict with keys: user_id, year_month, breakdown (dict category→amount),
    income, transactions_count.
    """
    cosmos_account = os.environ["COSMOS_ACCOUNT"]
    database = os.environ.get("COSMOS_DATABASE", "banking")

    credential = DefaultAzureCredential()
    url = f"https://{cosmos_account}.documents.azure.com:443/"

    async with CosmosClient(url=url, credential=credential) as client:
        container = client.get_database_client(database).get_container_client("transactions")

        # Query all transactions for this user in the given month
        # DECISION: 2026-05-27 — we filter by timestamp string prefix which works
        # because ISO8601 is lexicographically sortable. Reversible with a proper
        # date range query if needed.
        query = (
            "SELECT c.category, c.amount FROM c "
            "WHERE c.user_id = @user_id "
            "AND STARTSWITH(c.timestamp, @month_prefix)"
        )
        params = [
            {"name": "@user_id", "value": user_id},
            {"name": "@month_prefix", "value": year_month},
        ]

        breakdown: dict[str, float] = defaultdict(float)
        income = 0.0
        count = 0

        async for item in container.query_items(
            query=query,
            parameters=params,
            partition_key=user_id,
        ):
            amount = float(item["amount"])
            if amount < 0:
                category = item.get("category", "other")
                breakdown[category] += abs(amount)
                count += 1
            else:
                income += amount

    return {
        "user_id": user_id,
        "year_month": year_month,
        "breakdown": dict(breakdown),
        "income": income,
        "transactions_count": count,
    }
