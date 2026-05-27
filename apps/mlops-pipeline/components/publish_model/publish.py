"""Publish the new fastText model to Azure ML Model Registry and Blob (SPEC §7.2 step 5)."""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime, UTC
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from azure.ai.ml.entities import Model
from azure_clients import create_ml_client, create_blob_client_for_model


def main(model_path: str, metrics_path: str) -> None:
    """Register model in AML registry and upload to Blob.

    After publishing, triggers the CD GitHub Actions workflow via webhook.
    """
    ml_client = create_ml_client()

    version_tag = datetime.now(UTC).strftime("%Y-%m-%d")
    model_name = "fasttext-transaction-classifier"

    # Register model in AML
    model = Model(
        path=model_path,
        name=model_name,
        description=f"fastText transaction classifier trained on feedback data",
        tags={"version": version_tag, "framework": "fasttext"},
        type="custom_model",
    )
    registered = ml_client.models.create_or_update(model)
    print(f"Model registered: {registered.name} version {registered.version}")

    # Upload model to Blob for hot-path download
    blob_account = os.environ.get("MODEL_BLOB_ACCOUNT", "")
    blob_container = os.environ.get("MODEL_BLOB_CONTAINER", "models")
    if blob_account:
        blob_name = f"fasttext-{version_tag}.bin"
        blob_url = f"https://{blob_account}.blob.core.windows.net/{blob_container}/{blob_name}"
        blob_client = create_blob_client_for_model(blob_url)
        with open(model_path, "rb") as f:
            blob_client.upload_blob(f, overwrite=True)
        print(f"Model uploaded to: {blob_url}")

        # Trigger GitHub Actions CD workflow via repository dispatch
        # DECISION: 2026-05-27 — webhook dispatch is the simplest integration
        # between AML and GitHub Actions without shared state. Reversible by
        # using AML webhooks natively if AML ever supports GitHub OIDC directly.
        gh_token = os.environ.get("GITHUB_PAT", "")
        gh_repo = os.environ.get("GITHUB_REPO", "")
        if gh_token and gh_repo:
            import json, urllib.request  # noqa: PLC0415, E401
            payload = json.dumps({
                "event_type": "model-published",
                "client_payload": {"model_uri": blob_url, "version": version_tag},
            }).encode()
            req = urllib.request.Request(
                f"https://api.github.com/repos/{gh_repo}/dispatches",
                data=payload,
                headers={
                    "Authorization": f"Bearer {gh_token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req)
            print(f"Triggered CD workflow for repo {gh_repo}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--metrics-path", required=True)
    args = parser.parse_args()
    main(args.model_path, args.metrics_path)
