"""Anomaly detection via Z-Score.

Deterministic, no external dependencies — designed for TDD (SPEC §5.6).

Rules:
  - Only applied to expenses (negative amounts). Income is never an anomaly.
  - If std_dev == 0 (insufficient history), returns is_anomaly=False.
  - Threshold is configurable; default 3σ.
"""

from __future__ import annotations

from hot_path.domain.models import AnomalyResult, Transaction, UserProfile


class AnomalyDetector:
    """Stateless Z-Score anomaly detector."""

    def __init__(self, threshold: float = 3.0) -> None:
        self._threshold = threshold

    def detect(self, tx: Transaction, profile: UserProfile) -> AnomalyResult:
        """Detect whether a transaction is anomalous given the user's profile.

        Returns AnomalyResult with is_anomaly=False for income (amount >= 0).
        """
        # Only evaluate expenses
        if tx.amount >= 0:
            return AnomalyResult(is_anomaly=False, z_score=0.0)

        spend = abs(float(tx.amount))
        mean = float(profile.mean_spend)
        std = float(profile.stddev_spend)

        # Guard: std == 0 means no history — never divide by zero
        if std == 0:
            return AnomalyResult(is_anomaly=False, z_score=0.0)

        z = (spend - mean) / std

        if z > self._threshold:
            reason = (
                f"Importe {spend:.2f}€ supera tu media habitual "
                f"({mean:.2f}€) en {z:.1f}σ"
            )
            return AnomalyResult(is_anomaly=True, z_score=z, reason=reason)

        return AnomalyResult(is_anomaly=False, z_score=z)
