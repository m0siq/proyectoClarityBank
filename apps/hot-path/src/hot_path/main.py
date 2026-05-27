"""Entry point for the hot-path service (SPEC §5.13).

Starts two coroutines in parallel on the same event loop:
  1. FastAPI HTTP server (port 8000) — /health, /ready, optional /v1/classify.
  2. Event Hubs consumer — the main driver that reads, classifies, persists.

Both share the same in-process instances of all services and models.
That shared state is the reason for the monolith architecture (SPEC §2.1).

Authentication: ALL Azure SDK calls use DefaultAzureCredential.
  - Locally:  az login  (or VS Code Azure extension)
  - In Azure: Managed Identity bound to the Container App
  - Set AZURE_CLIENT_ID env var when using a user-assigned MI
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from hot_path.api.app import build_api
from hot_path.consumers.event_hub import TransactionConsumer
from hot_path.core.azure_clients import (
    create_cosmos_client,
    create_event_hub_consumer,
    create_openai_client,
)
from hot_path.core.config import Settings
from hot_path.core.logging import logger, setup_logging
from hot_path.core.telemetry import setup_telemetry
from hot_path.repositories.feedback import FeedbackRepository
from hot_path.repositories.profiles import ProfileRepository
from hot_path.repositories.transactions import TransactionRepository
from hot_path.services.anomaly import AnomalyDetector
from hot_path.services.classifier_l1 import FastTextClassifier, download_model_if_needed
from hot_path.services.classifier_l2 import OpenAIClassifier
from hot_path.services.pipeline import TransactionPipeline


async def main() -> None:
    """Bootstrap and run the hot-path service."""
    settings = Settings()

    # ── Logging + Telemetry ──────────────────────────────────────────────────
    setup_logging(settings.log_level)
    setup_telemetry(
        settings.applicationinsights_connection_string,
        service_name="hot-path",
    )

    logger.info("hot_path_starting", version="0.1.0")

    # ── Download fastText model if not on disk ───────────────────────────────
    # Blocking; the service MUST NOT start without the model.
    # If fasttext_model_path exists: use it.
    # If not: download from fasttext_model_uri (Blob) using Managed Identity.
    try:
        download_model_if_needed(
            model_path=settings.fasttext_model_path,
            model_uri=settings.fasttext_model_uri,
        )
    except FileNotFoundError as exc:
        logger.error("model_download_failed", error=str(exc))
        # SPEC §5.14: fail fast — Container Apps will restart the container.
        sys.exit(1)

    # ── Load fastText model (blocking, once per process) ─────────────────────
    try:
        l1 = FastTextClassifier(
            model_path=settings.fasttext_model_path,
            model_version=settings.fasttext_model_version,
        )
    except Exception as exc:
        logger.error("fasttext_load_failed", error=str(exc))
        sys.exit(1)

    # ── Azure clients (all use DefaultAzureCredential / Managed Identity) ────
    cosmos_client = create_cosmos_client(settings.cosmos_account)

    profiles_repo = ProfileRepository(
        client=cosmos_client,
        database=settings.cosmos_database,
        container=settings.cosmos_container_profiles,
        cache_ttl_seconds=settings.profile_cache_ttl_seconds,
    )
    transactions_repo = TransactionRepository(
        client=cosmos_client,
        database=settings.cosmos_database,
        container=settings.cosmos_container_transactions,
    )
    feedback_repo = FeedbackRepository(
        client=cosmos_client,
        database=settings.cosmos_database,
        container=settings.cosmos_container_feedback,
    )

    # ── OpenAI L2 client (Managed Identity via token provider) ───────────────
    openai_async_client = create_openai_client(
        endpoint=str(settings.openai_endpoint),
        api_version=settings.openai_api_version,
        timeout=settings.openai_timeout_seconds,
    )
    l2 = OpenAIClassifier(
        client=openai_async_client,
        deployment=settings.openai_deployment_l2,
        model_version=settings.openai_deployment_l2,
    )

    # ── Services ──────────────────────────────────────────────────────────────
    anomaly = AnomalyDetector(threshold=settings.zscore_threshold)
    pipeline = TransactionPipeline(
        anomaly=anomaly,
        l1=l1,
        l2=l2,
        profiles=profiles_repo,
        feedback=feedback_repo,
        confidence_threshold=settings.confidence_threshold,
    )

    # ── Event Hub consumer (Managed Identity) ────────────────────────────────
    eh_client, _ = create_event_hub_consumer(
        namespace=settings.event_hub_namespace,
        eventhub_name=settings.event_hub_name,
        consumer_group=settings.event_hub_consumer_group,
        checkpoint_storage_account=settings.checkpoint_storage_account,
        checkpoint_container=settings.checkpoint_container,
    )
    consumer = TransactionConsumer(
        config=settings,
        pipeline=pipeline,
        transactions_repo=transactions_repo,
        profiles_repo=profiles_repo,
        cosmos_client=cosmos_client,
        eh_client=eh_client,
    )

    # ── FastAPI ───────────────────────────────────────────────────────────────
    api = build_api(settings, pipeline)

    # ── Run both concurrently on the same event loop ──────────────────────────
    logger.info("hot_path_ready", port=settings.api_port)
    api_server = uvicorn.Server(
        uvicorn.Config(
            api,
            host="0.0.0.0",  # noqa: S104
            port=settings.api_port,
            log_config=None,  # structlog handles all logging
        )
    )

    await asyncio.gather(
        consumer.run(),
        api_server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
