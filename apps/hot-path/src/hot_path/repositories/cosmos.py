"""Base Cosmos DB client factory.

Connection via AAD / Managed Identity — no master keys ever.
Per SPEC §5.5:
  - Endpoint pattern: https://{account}.documents.azure.com:443/
  - Never use read_all_items; always query with partition key.
  - Retries handled by the SDK; we only catch CosmosHttpResponseError for logging.
"""

from __future__ import annotations

from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential


def create_cosmos_client(account: str) -> CosmosClient:
    """Create an async CosmosClient authenticated via DefaultAzureCredential."""
    url = f"https://{account}.documents.azure.com:443/"
    credential = DefaultAzureCredential()
    return CosmosClient(url=url, credential=credential)
