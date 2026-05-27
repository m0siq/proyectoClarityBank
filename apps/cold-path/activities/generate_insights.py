"""Generate financial insights using synchronous parallel Azure OpenAI calls with a semaphore (no-batch)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import logging
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI
from shared.prompts import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

def _anonymize_user_id(user_id: str) -> str:
    """SHA-256 truncated to 16 chars — sufficient for 340k users (SPEC §6.4)."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


async def generate_insights(aggregated_list: list[dict]) -> list[dict]:
    """Generate monthly insights for all aggregated users in parallel using a semaphore."""
    endpoint = os.environ["OPENAI_ENDPOINT"]
    deployment = os.environ.get("OPENAI_BATCH_DEPLOYMENT", "gpt-4o-mini")  # default to gpt-4o-mini
    api_version = os.environ.get("OPENAI_API_VERSION", "2024-10-01-preview")

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    client = AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )

    # Use a semaphore to limit concurrent HTTP calls (TPM / rate limiting protection)
    semaphore = asyncio.Semaphore(10)

    async def generate_one(agg: dict) -> dict | None:
        user_id = agg["user_id"]
        user_hash = _anonymize_user_id(user_id)
        user_msg = build_user_prompt(
            user_hash=user_hash,
            year_month=agg["year_month"],
            breakdown=agg["breakdown"],
        )

        try:
            async with semaphore:
                response = await client.chat.completions.create(
                    model=deployment,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.3,
                    max_tokens=200,
                )
                summary_text = response.choices[0].message.content or ""
                return {
                    "user_id": user_id,
                    "year_month": agg["year_month"],
                    "summary_text": summary_text,
                    "breakdown": agg["breakdown"],
                }
        except Exception as exc:
            logger.error(f"Error generating insight for {user_id}: {exc}")
            return None

    tasks = [generate_one(agg) for agg in aggregated_list]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]
