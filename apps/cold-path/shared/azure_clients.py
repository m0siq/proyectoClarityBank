"""Azure client factory for the cold-path (Durable Functions).

All authentication uses DefaultAzureCredential:
  - Local dev: az login / VS Code
  - In Azure:  Managed Identity bound to the Function App

RBAC required for the cold-path Managed Identity:
  - Cosmos DB Built-in Data Contributor  on DB 'banking'
  - Cognitive Services OpenAI Contributor on the OpenAI account (Batch needs Contributor)
  - Storage Blob Data Contributor         on the 'batch-io' container
"""

from __future__ import annotations

import os

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI


def create_cosmos_client() -> CosmosClient:
    """Create async Cosmos DB client using Managed Identity.

    Reads COSMOS_ACCOUNT from environment.

    Example:
        >>> async with create_cosmos_client() as client:
        ...     container = client.get_database_client("banking").get_container_client("insights")
    """
    account = os.environ["COSMOS_ACCOUNT"]
    url = f"https://{account}.documents.azure.com:443/"
    credential = DefaultAzureCredential()
    return CosmosClient(url=url, credential=credential)


def create_openai_client() -> AsyncAzureOpenAI:
    """Create async Azure OpenAI client using Managed Identity.

    Reads OPENAI_ENDPOINT and OPENAI_API_VERSION from environment.

    Example:
        >>> client = create_openai_client()
        >>> batch = await client.batches.create(...)
    """
    endpoint = os.environ["OPENAI_ENDPOINT"]
    api_version = os.environ.get("OPENAI_API_VERSION", "2024-10-01-preview")

    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default",
    )
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )


def create_blob_service_client(storage_account: str | None = None):  # type: ignore[return]
    """Create async BlobServiceClient using Managed Identity.

    Used to upload/download Batch API JSONL files.

    RBAC required: Storage Blob Data Contributor on 'batch-io' container.

    Example:
        >>> blob_svc = create_blob_service_client()
        >>> container = blob_svc.get_container_client("batch-io")
        >>> await container.upload_blob("batch.jsonl", data)
    """
    from azure.storage.blob.aio import BlobServiceClient  # noqa: PLC0415

    account = storage_account or os.environ.get("BATCH_STORAGE_ACCOUNT", "")
    if not account:
        raise ValueError("BATCH_STORAGE_ACCOUNT env var not set")

    credential = DefaultAzureCredential()
    url = f"https://{account}.blob.core.windows.net"
    return BlobServiceClient(account_url=url, credential=credential)
