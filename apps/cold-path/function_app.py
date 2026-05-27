"""Azure Durable Functions entry point for the cold-path service.

Triggers:
  - TimerTrigger: CRON "0 0 2 1 * *" (02:00 UTC on the 1st of each month)
    → starts the monthly_insights orchestrator.

Activities registered here:
  - list_active_users
  - aggregate_user
  - generate_insights
  - persist_insights
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, UTC

import azure.functions as func
import azure.durable_functions as df
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from activities.aggregate_user import aggregate_user_activity
from activities.generate_insights import generate_insights as run_generate_insights
from activities.persist_insight import persist_insights as run_persist_insights
from orchestrators.monthly_insights import main as orchestrator_main

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

logger = logging.getLogger(__name__)


# ── Timer trigger ─────────────────────────────────────────────────────────────

@app.timer_trigger(
    schedule="0 0 2 1 * *",  # 02:00 UTC on the 1st of each month (SPEC §6.3)
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
@app.durable_client_input(client_name="client")
async def monthly_insights_trigger(
    timer: func.TimerRequest,
    client: df.DurableOrchestrationClient,
) -> None:
    """Kick off the monthly insights orchestration."""
    year_month = datetime.now(UTC).strftime("%Y-%m")
    instance_id = await client.start_new("monthly_insights_orchestrator", instance_id=None, client_input=year_month)
    logger.info(f"Started monthly_insights orchestrator: {instance_id} for {year_month}")


# ── Orchestrator ──────────────────────────────────────────────────────────────

@app.orchestration_trigger(context_name="context")
def monthly_insights_orchestrator(context: df.DurableOrchestrationContext):
    return orchestrator_main(context)


# ── Activities ────────────────────────────────────────────────────────────────

@app.activity_trigger(input_name="year_month")
async def list_active_users(year_month: str) -> list[str]:
    """Return all user_ids that had transactions in the given year_month."""
    cosmos_account = os.environ["COSMOS_ACCOUNT"]
    database = os.environ.get("COSMOS_DATABASE", "banking")
    credential = DefaultAzureCredential()
    url = f"https://{cosmos_account}.documents.azure.com:443/"

    user_ids = set()
    async with CosmosClient(url=url, credential=credential) as client:
        container = client.get_database_client(database).get_container_client("transactions")
        # DECISION: 2026-05-27 — cross-partition query to get distinct user_ids.
        # This is acceptable once/month in the cold path. Not allowed in hot path.
        query = (
            "SELECT DISTINCT c.user_id FROM c "
            "WHERE STARTSWITH(c.timestamp, @month_prefix)"
        )
        params = [{"name": "@month_prefix", "value": year_month}]
        async for item in container.query_items(query=query, parameters=params):
            user_ids.add(item["user_id"])

    return list(user_ids)


@app.activity_trigger(input_name="payload")
async def aggregate_user(payload: dict) -> dict:
    return await aggregate_user_activity(
        user_id=payload["user_id"],
        year_month=payload["year_month"],
    )


@app.activity_trigger(input_name="aggregated_list")
async def generate_insights(aggregated_list: list) -> list[dict]:
    return await run_generate_insights(aggregated_list)


@app.activity_trigger(input_name="results")
async def persist_insights(results: list) -> int:
    return await run_persist_insights(results)
