"""Run the MLOps retraining path locally without submitting an AML job."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "components" / "extract_feedback"))
sys.path.insert(0, str(BASE / "components" / "train_fasttext"))
sys.path.insert(0, str(BASE / "components" / "evaluate"))

from extract import main as extract_feedback
from train import main as train_fasttext
from evaluate import main as evaluate_model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year-month", default="2026-05")
    parser.add_argument("--work-dir", default="/private/tmp/hotpath-mlops-smoke")
    parser.add_argument("--skip-train", action="store_true")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    feedback_path = work_dir / "feedback.parquet"
    model_path = work_dir / "model.bin"
    metrics_path = work_dir / "metrics.json"

    print(f"extract_feedback year_month={args.year_month}")
    extract_feedback(str(feedback_path), args.year_month)
    print(f"feedback_path={feedback_path}")

    if args.skip_train:
        return

    print("train_fasttext")
    train_fasttext(
        feedback_path=str(feedback_path),
        historical_path=None,
        output_model_path=str(model_path),
        output_metrics_path=str(metrics_path),
    )
    print(f"model_path={model_path}")
    print(f"metrics_path={metrics_path}")

    print("evaluate_model")
    evaluate_model(str(metrics_path), baseline_metrics_path=None)


if __name__ == "__main__":
    main()
