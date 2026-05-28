"""Anomaly detection via amount Z-Score and merchant novelty.

Deterministic, no external dependencies — designed for TDD (SPEC §5.6).

Rules:
  - Only applied to expenses (negative amounts). Income is never an anomaly.
  - If std_dev == 0 (insufficient history), returns is_anomaly=False.
  - Threshold is configurable; default 3σ.
  - Merchant text that L1 can only classify as OTHER with very low confidence
    is treated as out-of-distribution and flagged as a separate anomaly signal.
"""

from __future__ import annotations

import re

from hot_path.domain.models import AnomalyResult, Category, ClassificationL1, Transaction, UserProfile


_DOMAIN_LIKE_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]{2,}\.(?:app|biz|cn|com|info|net|online|ru|shop|site|top|xyz)\b",
    re.IGNORECASE,
)


class AnomalyDetector:
    """Stateless anomaly detector."""

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

    def add_merchant_signal(
        self,
        result: AnomalyResult,
        tx: Transaction,
        l1: ClassificationL1,
        *,
        confidence_threshold: float = 0.50,
    ) -> AnomalyResult:
        """Flag merchant text that appears outside the L1 training distribution."""
        if tx.amount >= 0:
            return result

        reasons: list[str] = []
        if l1.category == Category.OTHER and l1.confidence < confidence_threshold:
            reasons.append(
                "Comercio fuera de distribución para el modelo L1 "
                f"(categoria=other, confianza={l1.confidence:.2f})"
            )

        format_reason = self._merchant_format_reason(tx.merchant_raw)
        if format_reason:
            reasons.append(format_reason)

        if not reasons:
            return result

        merchant_reason = f"{'; '.join(reasons)}: {tx.merchant_raw}"
        if result.is_anomaly:
            reason = f"{result.reason}; {merchant_reason}" if result.reason else merchant_reason
            return result.model_copy(update={"reason": reason})

        return AnomalyResult(
            is_anomaly=True,
            z_score=result.z_score,
            reason=merchant_reason,
        )

    def _merchant_format_reason(self, merchant_raw: str) -> str | None:
        text = merchant_raw.strip()
        if not text:
            return "Comercio sin descriptor legible"

        if _DOMAIN_LIKE_RE.search(text):
            return "Comercio con formato atípico tipo dominio"

        compact = re.sub(r"[^a-z0-9]", "", text.lower())
        if len(compact) >= 9 and len(compact) % 3 == 0:
            chunk = compact[: len(compact) // 3]
            if chunk * 3 == compact:
                return "Comercio con patrón repetitivo poco habitual"

        punctuation = sum(1 for char in text if not char.isalnum() and not char.isspace())
        if len(text) >= 12 and punctuation / len(text) > 0.25:
            return "Comercio con exceso de símbolos en el descriptor"

        return None
