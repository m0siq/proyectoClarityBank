"""Application configuration via pydantic-settings.

All configuration arrives via environment variables with the prefix HOTPATH_.
Zero hardcoded values. Zero .env files in production.
"""

from __future__ import annotations

from pydantic import AnyUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="HOTPATH_",
        env_file_encoding="utf-8",
    )

    # ── Azure identity ────────────────────────────────────────────────────────
    azure_client_id: str | None = None  # only if using user-assigned MI

    # ── Event Hubs ───────────────────────────────────────────────────────────
    event_hub_namespace: str = Field(..., description="e.g. ehns-banking-prod")
    event_hub_name: str = Field(default="transactions")
    event_hub_consumer_group: str = Field(default="$Default")
    checkpoint_storage_account: str = Field(...)
    checkpoint_container: str = "checkpoints"

    # ── Cosmos DB ────────────────────────────────────────────────────────────
    cosmos_account: str = Field(...)  # e.g. cosmos-banking-prod
    cosmos_database: str = "banking"
    cosmos_container_transactions: str = "transactions"
    cosmos_container_profiles: str = "user_profiles"
    cosmos_container_feedback: str = "feedback_loop"

    # ── Azure OpenAI ─────────────────────────────────────────────────────────
    openai_endpoint: AnyUrl = Field(...)
    openai_deployment_l2: str = "gpt-4o-mini"
    openai_api_version: str = "2024-10-01-preview"
    openai_timeout_seconds: float = 2.0

    # ── Classifier ───────────────────────────────────────────────────────────
    fasttext_model_path: str = "/app/ml_assets/model.bin"
    fasttext_model_uri: str | None = None  # blob URI to download at boot
    fasttext_model_version: str = "fasttext-dev"
    confidence_threshold: float = 0.85

    # ── Anomaly detection ────────────────────────────────────────────────────
    zscore_threshold: float = 3.0
    profile_cache_ttl_seconds: int = 300

    # ── Telemetry ─────────────────────────────────────────────────────────────
    applicationinsights_connection_string: str = Field(default="")
    log_level: str = "INFO"

    # ── Runtime ──────────────────────────────────────────────────────────────
    api_port: int = 8000
    # DECISION: 2026-05-27 — sync API disabled by default; only true in dev.
    enable_sync_api: bool = False
    # DECISION: 2026-05-27 — fail-loud by default; set dead_letter_mode=true to enable DLQ.
    dead_letter_mode: bool = False
