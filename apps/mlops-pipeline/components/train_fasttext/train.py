"""Train fastText model on feedback + historical data (SPEC §7.2 step 3).

Hyperparameters per SPEC:
  lr=0.5, epoch=25, wordNgrams=2, bucket=200000, dim=100
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

import fasttext
import mlflow
import pandas as pd
from sklearn.model_selection import train_test_split


def normalize(text: str) -> str:
    """Same normalization as the hot-path classifier (must stay in sync)."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"\d+", "", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def to_fasttext_format(row: pd.Series) -> str:  # type: ignore[type-arg]
    return f"__label__{row['label']} {normalize(row['merchant_raw'])}"


def main(
    feedback_path: str,
    historical_path: str | None,
    output_model_path: str,
    output_metrics_path: str,
) -> None:
    # Load data
    df = pd.read_parquet(feedback_path)
    if historical_path and Path(historical_path).exists():
        hist_df = pd.read_parquet(historical_path)
        df = pd.concat([df, hist_df], ignore_index=True)

    df = df.dropna(subset=["merchant_raw", "label"])
    df["merchant_raw"] = df["merchant_raw"].astype(str)
    df["label"] = df["label"].astype(str)

    # Split 80/10/10
    train_df, temp_df = train_test_split(df, test_size=0.2, random_state=42, stratify=df["label"])
    val_df, test_df = train_test_split(temp_df, test_size=0.5, random_state=42, stratify=temp_df["label"])

    # Write fastText format files
    train_file = "/tmp/train.txt"
    val_file = "/tmp/val.txt"
    test_file = "/tmp/test.txt"

    for split_df, path in [(train_df, train_file), (val_df, val_file), (test_df, test_file)]:
        with open(path, "w") as f:
            for _, row in split_df.iterrows():
                f.write(to_fasttext_format(row) + "\n")

    # Train
    mlflow.start_run()
    model = fasttext.train_supervised(
        input=train_file,
        lr=0.5,
        epoch=25,
        wordNgrams=2,
        bucket=200000,
        dim=100,
        verbose=2,
    )

    # Evaluate on validation set
    val_result = model.test(val_file)
    n_val, precision_val, recall_val = val_result
    test_result = model.test(test_file)
    n_test, precision_test, recall_test = test_result

    mlflow.log_metrics({
        "val_precision_at_1": precision_val,
        "val_recall_at_1": recall_val,
        "test_precision_at_1": precision_test,
        "test_recall_at_1": recall_test,
        "train_samples": len(train_df),
        "val_samples": len(val_df),
        "test_samples": len(test_df),
    })
    mlflow.log_params({
        "lr": 0.5,
        "epoch": 25,
        "wordNgrams": 2,
        "bucket": 200000,
        "dim": 100,
    })

    # Save model
    Path(output_model_path).parent.mkdir(parents=True, exist_ok=True)
    model.save_model(output_model_path)

    # Write metrics for evaluate component
    import json
    Path(output_metrics_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_metrics_path, "w") as f:
        json.dump({"precision_at_1": precision_test}, f)

    mlflow.end_run()
    print(f"Model saved to {output_model_path}")
    print(f"Test precision@1: {precision_test:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--feedback-path", required=True)
    parser.add_argument("--historical-path", default=None)
    parser.add_argument("--output-model-path", required=True)
    parser.add_argument("--output-metrics-path", required=True)
    args = parser.parse_args()
    main(
        args.feedback_path,
        args.historical_path,
        args.output_model_path,
        args.output_metrics_path,
    )
