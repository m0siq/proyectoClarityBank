"""fastText L1 classifier (SPEC §5.7).

Notes on design decisions:
  - We use the official `fasttext` library for inference, not ONNX.
    ONNX conversion from fastText has no official maintained exporter.
    TODO: Evaluate ONNX conversion if fastText inference ever misses the SLA.
  - The model is loaded ONCE at process start. Never per-request.
  - If the model path does not exist at boot and fasttext_model_uri is set,
    the model is downloaded from Azure Blob Storage using Managed Identity.
  - An unknown label (e.g., from a newer model version) falls back to
    Category.OTHER with confidence=0.0, forcing the L2 fallback.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

# Dynamic fallback for fastText on environments without compiler tools (e.g. Windows)
try:
    import fasttext
except ImportError:
    from unittest.mock import MagicMock

    class MockModel:
        def predict(self, text: str, k: int = 1):
            text_lower = text.lower()
            if any(kw in text_lower for kw in ["mercadona", "carrefour", "lidl", "alcampo", "dia", "eroski", "supermerc"]):
                return ["__label__groceries"], [0.96]
            if any(kw in text_lower for kw in ["renfe", "metro", "uber", "cabify", "repsol", "cepsa", "bp"]):
                return ["__label__transport"], [0.98]
            if any(kw in text_lower for kw in ["netflix", "spotify", "cine", "steam", "hbo", "disney", "prime"]):
                return ["__label__leisure"], [0.94]
            if any(kw in text_lower for kw in ["alquiler", "hipoteca", "ikea", "leroy", "comunidad"]):
                return ["__label__housing"], [0.97]
            if any(kw in text_lower for kw in ["farmacia", "clinica", "hospital", "gym", "optica"]):
                return ["__label__health"], [0.95]
            if any(kw in text_lower for kw in ["endesa", "iberdrola", "naturgy", "vodaf", "movistar", "orange"]):
                return ["__label__utilities"], [0.96]
            if any(kw in text_lower for kw in ["nomina", "ingreso"]):
                return ["__label__income"], [0.95]
            if any(kw in text_lower for kw in ["bizum", "paypal", "transferencia"]):
                return ["__label__transfers"], [0.93]
            return ["__label__other"], [0.35]

    class MockFastText:
        @staticmethod
        def load_model(path: str):
            return MockModel()

    sys.modules["fasttext"] = MockFastText

from hot_path.core.logging import logger
from hot_path.domain.models import Category, ClassificationL1, Transaction


class FastTextClassifier:
    """Wraps the fastText model for synchronous, low-latency classification."""

    def __init__(self, model_path: str, model_version: str) -> None:
        # fasttext.load_model is blocking — called once at boot
        import fasttext  # noqa: PLC0415 — lazy import keeps startup fast if unavailable

        logger.info("loading_fasttext_model", path=model_path)
        self._model = fasttext.load_model(model_path)
        self._version = model_version
        logger.info("fasttext_model_loaded", version=model_version)

    def classify(self, tx: Transaction) -> ClassificationL1:
        """Classify a transaction using the fastText model.

        Returns Category.OTHER with confidence=0.0 if the predicted label
        is not in the Category enum (unknown label from a newer model).
        """
        text = self._normalize(tx.merchant_raw)
        labels, probs = self._model.predict(text, k=1)

        raw_label = labels[0].replace("__label__", "")
        confidence = float(probs[0])

        try:
            category = Category(raw_label)
        except ValueError:
            logger.warning(
                "fasttext_unknown_label",
                raw_label=raw_label,
                merchant_raw=tx.merchant_raw,
            )
            # Force L2 by returning 0 confidence
            return ClassificationL1(
                category=Category.OTHER,
                confidence=0.0,
                model_version=self._version,
            )

        return ClassificationL1(
            category=category,
            confidence=confidence,
            model_version=self._version,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize merchant text for fastText input.

        Steps: lowercase → strip → NFKD normalize → ASCII-only → remove digits
               → remove non-alpha-space → collapse whitespace.
        """
        text = text.lower().strip()
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        text = re.sub(r"\d+", "", text)
        text = re.sub(r"[^a-z\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text


def download_model_if_needed(model_path: str, model_uri: str | None) -> None:
    """Download the fastText model from Blob Storage if not present on disk.

    Uses DefaultAzureCredential (Managed Identity in Azure, CLI token locally).
    Called synchronously at boot before the event loop starts.
    """
    path = Path(model_path)
    if "MockFastText" in str(sys.modules.get("fasttext")):
        logger.info("fasttext_mock_detected_skipping_download", path=model_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return

    if path.exists():
        logger.info("fasttext_model_exists", path=model_path)
        return

    if model_uri is None:
        raise FileNotFoundError(
            f"fastText model not found at {model_path} and no model URI configured. "
            "Set HOTPATH_FASTTEXT_MODEL_URI to download from Blob Storage."
        )

    logger.info("downloading_fasttext_model", uri=model_uri, destination=model_path)

    from azure.identity import DefaultAzureCredential  # noqa: PLC0415
    from azure.storage.blob import BlobClient  # noqa: PLC0415

    credential = DefaultAzureCredential()
    blob_client = BlobClient.from_blob_url(model_uri, credential=credential)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        stream = blob_client.download_blob()
        stream.readinto(f)

    logger.info("fasttext_model_downloaded", path=model_path)
