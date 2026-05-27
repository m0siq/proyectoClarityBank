"""Azure client factory for the mlops-pipeline.

Authentication: DefaultAzureCredential
  - Local dev: az login
  - AML compute: Managed Identity attached to the compute cluster

RBAC required for the AML compute Managed Identity:
  - Cosmos DB Built-in Data Reader  on container 'feedback_loop'
  - AcrPush                          on the Container Registry
  - Storage Blob Data Contributor    on the models container
"""

from __future__ import annotations

import os

from azure.ai.ml import MLClient
from azure.identity import DefaultAzureCredential


def create_ml_client() -> MLClient:
    """Create an Azure ML client using Managed Identity.

    Reads AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AML_WORKSPACE from env.

    Example:
        >>> ml_client = create_ml_client()
        >>> job = ml_client.jobs.create_or_update(pipeline_job)
    """
    credential = DefaultAzureCredential()
    return MLClient(
        credential=credential,
        subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
        resource_group_name=os.environ["AZURE_RESOURCE_GROUP"],
        workspace_name=os.environ["AML_WORKSPACE"],
    )


def create_cosmos_client():  # type: ignore[return]
    """Create a synchronous Cosmos DB client for MLOps components.

    Used by extract_feedback component to query feedback_loop.
    RBAC required: Cosmos DB Built-in Data Reader on 'feedback_loop'.

    Example:
        >>> client = create_cosmos_client()
        >>> container = client.get_database_client("banking").get_container_client("feedback_loop")
    """
    from azure.cosmos import CosmosClient  # noqa: PLC0415
    from azure.identity import DefaultAzureCredential as SyncCred  # noqa: PLC0415

    account = os.environ["COSMOS_ACCOUNT"]
    url = f"https://{account}.documents.azure.com:443/"
    credential = SyncCred()
    return CosmosClient(url=url, credential=credential)


def create_blob_client_for_model(blob_url: str):  # type: ignore[return]
    """Create a BlobClient for uploading the trained model.

    RBAC required: Storage Blob Data Contributor on the models container.

    Args:
        blob_url: Full SAS-free blob URL, e.g.
            'https://stbankingprod.blob.core.windows.net/models/fasttext-2026-05.bin'

    Example:
        >>> client = create_blob_client_for_model("https://stbankingprod.blob.core.windows.net/models/fasttext-2026-05.bin")
        >>> with open("model.bin", "rb") as f:
        ...     client.upload_blob(f, overwrite=True)
    """
    from azure.storage.blob import BlobClient  # noqa: PLC0415
    from azure.identity import DefaultAzureCredential as SyncCred  # noqa: PLC0415

    credential = SyncCred()
    return BlobClient.from_blob_url(blob_url, credential=credential)
