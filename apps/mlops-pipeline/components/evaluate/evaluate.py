"""Evaluate new model vs current model — fail pipeline if worse (SPEC §7.2 step 4)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(new_metrics_path: str, baseline_metrics_path: str | None) -> None:
    """Compare new model precision@1 against baseline.

    If baseline does not exist (first run), always pass.
    If new model is worse, exit with code 1 (fails the pipeline).
    """
    with open(new_metrics_path) as f:
        new_metrics = json.load(f)
    new_precision = new_metrics["precision_at_1"]

    if baseline_metrics_path and Path(baseline_metrics_path).exists():
        with open(baseline_metrics_path) as f:
            baseline_metrics = json.load(f)
        baseline_precision = baseline_metrics["precision_at_1"]
    else:
        print("No baseline found. This is the first model. Passing evaluation.")
        baseline_precision = 0.0

    print(f"New model precision@1:      {new_precision:.4f}")
    print(f"Baseline precision@1:       {baseline_precision:.4f}")

    if new_precision < baseline_precision:
        print("❌ New model is worse than baseline. Pipeline FAILED — model will NOT be published.")
        sys.exit(1)

    print(f"✅ New model is better or equal. Delta: {new_precision - baseline_precision:+.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--new-metrics-path", required=True)
    parser.add_argument("--baseline-metrics-path", default=None)
    args = parser.parse_args()
    main(args.new_metrics_path, args.baseline_metrics_path)
