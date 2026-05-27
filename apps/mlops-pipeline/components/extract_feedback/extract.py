"""Extract feedback records from Cosmos DB for MLOps retraining (SPEC §7.2)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential


def main(output_path: str, year_month: str) -> None:
    """Query Cosmos feedback_loop and save as parquet.

    Output schema: merchant_raw (str), label (str)
    where label = l2_prediction.category (the "correct" label according to L2).
    """
    cosmos_account = os.environ["COSMOS_ACCOUNT"]
    database = os.environ.get("COSMOS_DATABASE", "banking")

    credential = DefaultAzureCredential()
    url = f"https://{cosmos_account}.documents.azure.com:443/"

    client = CosmosClient(url=url, credential=credential)
    container = client.get_database_client(database).get_container_client("feedback_loop")

    query = (
        "SELECT c.merchant_raw, c.l2_prediction.category AS label "
        "FROM c WHERE c.year_month = @year_month"
    )
    params = [{"name": "@year_month", "value": year_month}]

    records = list(
        container.query_items(
            query=query,
            parameters=params,
            partition_key=year_month,
        )
    )

    if not records:
        print(f"No feedback records found for {year_month}")
        return

    df = pd.DataFrame(records)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    print(f"Extracted {len(df)} records → {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--year-month", required=True)
    args = parser.parse_args()
    main(args.output_path, args.year_month)
