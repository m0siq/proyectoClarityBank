"""Monthly insights Durable Functions orchestrator (SPEC §6.2).

Pattern: fan-out / fan-in.
  1. list_active_users          → list of user IDs
  2. aggregate_user (parallel)  → list of AggregatedSpend
  3. generate_insights          → generate insights in parallel with semaphore
  4. persist_insights           → write to Cosmos
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC

import azure.durable_functions as df


def orchestrator_function(context: df.DurableOrchestrationContext):
    """Durable orchestrator — fan-out / fan-in pattern."""
    year_month = context.get_input() or datetime.now(UTC).strftime("%Y-%m")
    logger = logging.getLogger(__name__)

    # Step 1: list all active users
    user_ids: list[str] = yield context.call_activity("list_active_users", year_month)
    logger.info(f"Processing {len(user_ids)} users for {year_month}")

    # Step 2: fan-out — aggregate each user in parallel
    aggregate_tasks = [
        context.call_activity("aggregate_user", {"user_id": uid, "year_month": year_month})
        for uid in user_ids
    ]
    aggregated_list: list[dict] = yield context.task_all(aggregate_tasks)

    # Filter out users with no transactions
    aggregated_list = [a for a in aggregated_list if a.get("transactions_count", 0) > 0]

    if not aggregated_list:
        logger.warning(f"No active users with transactions for {year_month}")
        return {"processed": 0}

    # Step 3: generate insights synchronously in parallel using semaphore
    results: list[dict] = yield context.call_activity("generate_insights", aggregated_list)

    # Step 4: persist insights
    saved: int = yield context.call_activity("persist_insights", results)

    return {"processed": len(aggregated_list), "saved": saved}


main = df.Orchestrator.create(orchestrator_function)
