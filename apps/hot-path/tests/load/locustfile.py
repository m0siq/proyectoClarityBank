"""Locust load test (SPEC §11.4).

Scenario:
  - 500 virtual users, ramp-up 60s, hold 5min
  - Each VU submits 1 tx/s to /v1/classify (sync API, enable_sync_api=True)
  - Assertions: p95 < 3000ms, error rate < 0.1%

Run with:
  locust -f tests/load/locustfile.py --host=http://localhost:8000 \
    --users=500 --spawn-rate=10 --run-time=6m --headless
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, UTC
from random import choice, uniform

from locust import HttpUser, between, task

MERCHANTS = [
    "MERCADONA BARCELONA",
    "RENFE CERCANIAS",
    "AMAZON ES",
    "FARMACIA SAN MIGUEL",
    "NETFLIX",
    "COMUNIDAD DE PROPIETARIOS 42",
    "ENDESA ENERGIA",
]


class TransactionUser(HttpUser):
    """Simulates a single banking client submitting transactions."""

    wait_time = between(0.9, 1.1)  # ~1 tx/s per user

    @task
    def classify_transaction(self) -> None:
        payload = {
            "transaction_id": str(uuid.uuid4()),
            "user_id": f"u_{uuid.uuid4().hex[:8]}",
            "amount": str(round(uniform(-500, -1), 2)),
            "currency": "EUR",
            "merchant_raw": choice(MERCHANTS),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with self.client.post(
            "/v1/classify",
            json=payload,
            catch_response=True,
            name="/v1/classify",
        ) as response:
            if response.status_code != 200:
                response.failure(f"Got status {response.status_code}")
            elif response.elapsed.total_seconds() > 3.0:
                response.failure(f"Latency exceeded 3s: {response.elapsed.total_seconds():.2f}s")
            else:
                response.success()
