"""Run the cold-path monthly insights flow once from a terminal.

This is a dev/test runner for the same logical steps as the Durable Function:
list users -> aggregate spending -> generate insights -> persist insights.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

from activities.aggregate_user import aggregate_user_activity
from activities.generate_insights import generate_insights
from activities.persist_insight import persist_insights


async def list_active_users(year_month: str, limit: int | None) -> list[str]:
    cosmos_account = os.environ["COSMOS_ACCOUNT"]
    database = os.environ.get("COSMOS_DATABASE", "banking")
    credential = DefaultAzureCredential()
    url = f"https://{cosmos_account}.documents.azure.com:443/"

    try:
        async with CosmosClient(url=url, credential=credential) as client:
            container = client.get_database_client(database).get_container_client("transactions")
            query = (
                "SELECT DISTINCT c.user_id FROM c "
                "WHERE STARTSWITH(c.timestamp, @month_prefix)"
            )
            params = [{"name": "@month_prefix", "value": year_month}]
            users = [
                item["user_id"]
                async for item in container.query_items(query=query, parameters=params)
            ]
    finally:
        await credential.close()

    users = sorted(users)
    return users[:limit] if limit else users


def build_mock_insight(agg: dict[str, Any]) -> dict[str, Any]:
    total_spend = sum(float(v) for v in agg["breakdown"].values())
    top_category = max(agg["breakdown"], key=agg["breakdown"].get, default="sin gasto")
    return {
        "user_id": agg["user_id"],
        "year_month": agg["year_month"],
        "summary_text": (
            f"Resumen sintético: gasto mensual {total_spend:.2f} EUR; "
            f"categoria principal {top_category}."
        ),
        "breakdown": agg["breakdown"],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year-month", default="2026-05")
    parser.add_argument("--limit-users", type=int, default=None)
    parser.add_argument("--mock-insights", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()

    users = await list_active_users(args.year_month, args.limit_users)
    print(f"active_users={len(users)} {users}")

    aggregated = [
        await aggregate_user_activity(user_id=user_id, year_month=args.year_month)
        for user_id in users
    ]
    aggregated = [a for a in aggregated if a.get("transactions_count", 0) > 0]
    print(f"aggregated_users={len(aggregated)}")
    for item in aggregated[:5]:
        print(
            f"aggregate {item['user_id']}: tx={item['transactions_count']} "
            f"income={item['income']:.2f} breakdown={item['breakdown']}"
        )

    if args.mock_insights:
        results = [build_mock_insight(item) for item in aggregated]
    else:
        results = await generate_insights(aggregated)

    print(f"insights_generated={len(results)}")
    for item in results[:5]:
        print(f"insight {item['user_id']}: {item['summary_text'][:160]}")

    if args.no_persist:
        return

    saved = await persist_insights(results)
    print(f"insights_saved={saved}")


if __name__ == "__main__":
    asyncio.run(main())
