"""Azure OpenAI L2 classifier (SPEC §5.8).

Used when L1 confidence < threshold.

The OpenAI client is injected (created by azure_clients.create_openai_client)
so this class is decoupled from authentication — easier to test and to swap.

Design decisions:
  - Auth via Managed Identity (configured in azure_clients.py, not here).
  - temperature=0.0 + json_object response_format for determinism.
  - Hard timeout: 2 s (configured at client creation time).
  - Any failure (timeout, 429, 500, bad JSON, invalid category) is caught,
    logged as warning, and returns Category.OTHER with rationale="fallback failed".
    The hot path is NEVER blocked by L2 failures.
"""

from __future__ import annotations

import json
import time

from openai import AsyncAzureOpenAI, APIError, APITimeoutError, RateLimitError

from hot_path.core.logging import logger
from hot_path.core.telemetry import record_l2_failure, record_l2_invocation
from hot_path.domain.models import Category, ClassificationL2, Transaction

CATEGORIES_LIST = ", ".join(c.value for c in Category)

SYSTEM_PROMPT = f"""Eres un clasificador determinístico de transacciones bancarias.
Recibirás el texto crudo del comercio y debes devolver UNA categoría de esta lista, exactamente con esa cadena:
{CATEGORIES_LIST}

Devuelve un JSON: {{"category": "<categoria>", "rationale": "<breve explicación>"}}
No inventes categorías. Si dudas, usa "other"."""

_FALLBACK = ClassificationL2(
    category=Category.OTHER,
    rationale="fallback failed",
    model_version="unknown",
    latency_ms=0,
    prompt_tokens=0,
    completion_tokens=0,
)


class OpenAIClassifier:
    """Async Azure OpenAI classifier.

    The client is injected (created by azure_clients.create_openai_client)
    so auth setup stays in one place.
    """

    def __init__(
        self,
        client: AsyncAzureOpenAI,
        deployment: str,
        model_version: str,
    ) -> None:
        self._client = client
        self._deployment = deployment
        self._version = model_version

    async def classify(self, tx: Transaction) -> ClassificationL2:
        """Classify a transaction using Azure OpenAI.

        Never raises — always returns ClassificationL2, using OTHER as the
        safe fallback on any error.
        """
        record_l2_invocation()
        t0 = time.perf_counter()

        try:
            response = await self._client.chat.completions.create(
                model=self._deployment,
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=120,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Comercio: {tx.merchant_raw}\n"
                            f"Importe: {tx.amount} {tx.currency}"
                        ),
                    },
                ],
            )
        except (APITimeoutError, RateLimitError, APIError) as exc:
            record_l2_failure()
            logger.warning(
                "l2_classifier_api_error",
                error=str(exc),
                transaction_id=str(tx.transaction_id),
            )
            return _FALLBACK

        latency_ms = int((time.perf_counter() - t0) * 1000)

        try:
            payload = json.loads(response.choices[0].message.content or "{}")
            category = Category(payload["category"])
            rationale = payload.get("rationale", "")
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            record_l2_failure()
            logger.warning(
                "l2_classifier_parse_error",
                error=str(exc),
                raw_content=response.choices[0].message.content,
                transaction_id=str(tx.transaction_id),
            )
            return ClassificationL2(
                category=Category.OTHER,
                rationale="parse error",
                model_version=self._version,
                latency_ms=latency_ms,
                prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
                completion_tokens=response.usage.completion_tokens if response.usage else 0,
            )

        return ClassificationL2(
            category=category,
            rationale=rationale,
            model_version=self._version,
            latency_ms=latency_ms,
            prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
            completion_tokens=response.usage.completion_tokens if response.usage else 0,
        )
