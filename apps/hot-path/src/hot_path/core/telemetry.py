"""OpenTelemetry + Azure Monitor telemetry setup.

Metrics emitted (per SPEC §5.12):
  - tx_processed_total      (counter, labels: final_classifier, is_anomaly)
  - tx_pipeline_latency_ms  (histogram)
  - l1_confidence           (histogram)
  - l2_invocations_total    (counter)
  - l2_failures_total       (counter)
  - cosmos_write_latency_ms (histogram)
"""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

SERVICE_NAME = "hot-path"
_meter: metrics.Meter | None = None


def setup_telemetry(connection_string: str, service_name: str = SERVICE_NAME) -> None:
    """Initialize Azure Monitor OpenTelemetry distro.

    If connection_string is empty (local dev without App Insights), fall back
    to a no-op provider so the rest of the code can always call metrics safely.
    """
    global _meter  # noqa: PLW0603

    if connection_string:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor

            configure_azure_monitor(
                connection_string=connection_string,
                resource_attributes={"service.name": service_name},
            )
        except Exception:  # noqa: BLE001 — telemetry must never crash the app
            pass

    # Always get a meter; if Azure Monitor is configured it'll export there
    _meter = metrics.get_meter(service_name)
    _setup_instruments()


# ── Instruments ───────────────────────────────────────────────────────────────

_tx_processed_total: metrics.Counter | None = None
_tx_pipeline_latency_ms: metrics.Histogram | None = None
_l1_confidence: metrics.Histogram | None = None
_l2_invocations_total: metrics.Counter | None = None
_l2_failures_total: metrics.Counter | None = None
_cosmos_write_latency_ms: metrics.Histogram | None = None


def _setup_instruments() -> None:
    global _tx_processed_total, _tx_pipeline_latency_ms, _l1_confidence, _l2_invocations_total, _l2_failures_total, _cosmos_write_latency_ms  # noqa: PLW0603
    if _meter is None:
        return

    _tx_processed_total = _meter.create_counter(
        "tx_processed_total",
        description="Number of transactions processed",
    )
    _tx_pipeline_latency_ms = _meter.create_histogram(
        "tx_pipeline_latency_ms",
        unit="ms",
        description="End-to-end pipeline latency",
    )
    _l1_confidence = _meter.create_histogram(
        "l1_confidence",
        description="fastText confidence score distribution",
    )
    _l2_invocations_total = _meter.create_counter(
        "l2_invocations_total",
        description="Number of L2 (OpenAI) classifier invocations",
    )
    _l2_failures_total = _meter.create_counter(
        "l2_failures_total",
        description="Number of L2 classifier failures",
    )
    _cosmos_write_latency_ms = _meter.create_histogram(
        "cosmos_write_latency_ms",
        unit="ms",
        description="Cosmos DB write latency",
    )


# ── Helper functions (safe no-op when not initialized) ───────────────────────

def record_tx_processed(final_classifier: str, is_anomaly: bool) -> None:
    if _tx_processed_total:
        _tx_processed_total.add(
            1, {"final_classifier": final_classifier, "is_anomaly": str(is_anomaly)}
        )


def record_pipeline_latency(latency_ms: int) -> None:
    if _tx_pipeline_latency_ms:
        _tx_pipeline_latency_ms.record(latency_ms)


def record_l1_confidence(confidence: float) -> None:
    if _l1_confidence:
        _l1_confidence.record(confidence)


def record_l2_invocation() -> None:
    if _l2_invocations_total:
        _l2_invocations_total.add(1)


def record_l2_failure() -> None:
    if _l2_failures_total:
        _l2_failures_total.add(1)


def record_cosmos_write_latency(latency_ms: int) -> None:
    if _cosmos_write_latency_ms:
        _cosmos_write_latency_ms.record(latency_ms)
