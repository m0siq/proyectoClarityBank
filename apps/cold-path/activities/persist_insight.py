"""Persist OpenAI Batch results as monthly insights in Cosmos DB."""

from __future__ import annotations

import json
import os
from datetime import datetime, UTC

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity.aio import DefaultAzureCredential


async def persist_insights(results: list[dict]) -> int:
    """Write monthly insight documents to Cosmos DB `insights` container.

    Args:
        results: list of dicts with keys: user_id, year_month, summary_text, breakdown

    Returns:
        Number of successfully persisted insights.
    """
    cosmos_account = os.environ["COSMOS_ACCOUNT"]
    database = os.environ.get("COSMOS_DATABASE", "banking")

    credential = DefaultAzureCredential()
    url = f"https://{cosmos_account}.documents.azure.com:443/"

    saved = 0
    try:
        async with CosmosClient(url=url, credential=credential) as client:
            container = client.get_database_client(database).get_container_client("insights")

            for result in results:
                user_id = result["user_id"]
                year_month = result["year_month"]
                doc = {
                    "id": f"{user_id}_{year_month}",
                    "user_id": user_id,
                    "year_month": year_month,
                    "summary_text": result.get("summary_text", ""),
                    "breakdown": result.get("breakdown", {}),
                    "generated_at": datetime.now(UTC).isoformat(),
                }
                try:
                    await container.upsert_item(doc)
                    saved += 1
                except CosmosHttpResponseError:
                    # Non-critical: log and continue with other users
                    pass
    finally:
        await credential.close()

    return saved
