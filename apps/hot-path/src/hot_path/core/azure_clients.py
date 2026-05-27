"""Azure client factory using Managed Identity (DefaultAzureCredential).

This module centralises all Azure SDK client creation for the hot-path service.
Zero secrets, zero connection strings — every client authenticates via
Managed Identity (or CLI/VS Code credential in local development).

Authentication chain (DefaultAzureCredential tries in order):
  1. EnvironmentCredential      — CI/CD with env vars
  2. WorkloadIdentityCredential — Kubernetes / ACA pod identity
  3. ManagedIdentityCredential  — Azure-hosted resources (Container Apps, Functions)
  4. SharedTokenCacheCredential — dev machine token cache
  5. VisualStudioCodeCredential — VS Code Azure account
  6. AzureCliCredential         — az login on dev machine
  7. AzurePowerShellCredential  — PowerShell Az module

For user-assigned Managed Identity, set AZURE_CLIENT_ID env var to the MI's client ID.
"""

from __future__ import annotations

from functools import lru_cache

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.cosmos.aio import CosmosClient
from azure.eventhub.aio import EventHubConsumerClient
from azure.eventhub.extensions.checkpointstoreblobaio import BlobCheckpointStore
from openai import AsyncAzureOpenAI

from hot_path.core.config import Settings
from hot_path.core.logging import logger


# ── Cosmos DB ─────────────────────────────────────────────────────────────────

def create_cosmos_client(account_name: str) -> CosmosClient:
    """Create an async Cosmos DB client authenticated via Managed Identity.

    Args:
        account_name: The Cosmos DB account name (e.g. 'cosmos-banking-prod').
                      The endpoint is derived as https://<account>.documents.azure.com:443/

    Returns:
        An async CosmosClient ready for use.

    Example:
        >>> client = create_cosmos_client("cosmos-banking-dev")
        >>> db = client.get_database_client("banking")
        >>> container = db.get_container_client("transactions")
    """
    endpoint = f"https://{account_name}.documents.azure.com:443/"
    credential = DefaultAzureCredential()
    logger.info("cosmos_client_created", endpoint=endpoint)
    return CosmosClient(url=endpoint, credential=credential)


# ── Azure OpenAI ──────────────────────────────────────────────────────────────

def create_openai_client(
    endpoint: str,
    api_version: str,
    timeout: float = 2.0,
) -> AsyncAzureOpenAI:
    """Create an async Azure OpenAI client authenticated via Managed Identity.

    Uses get_bearer_token_provider so the SDK automatically refreshes tokens
    before expiry — no manual token management needed.

    Args:
        endpoint: Azure OpenAI endpoint URL
                  (e.g. 'https://openai-banking-prod.openai.azure.com/')
        api_version: API version string (e.g. '2024-10-01-preview')
        timeout: Hard timeout per request in seconds (default 2.0 for L2).

    Returns:
        An async AsyncAzureOpenAI client.

    Required RBAC:
        - hot-path MI: 'Cognitive Services OpenAI User' on the OpenAI account
        - cold-path MI: 'Cognitive Services OpenAI Contributor' (Batch needs Contributor)

    Example:
        >>> client = create_openai_client(
        ...     endpoint="https://openai-banking-dev.openai.azure.com/",
        ...     api_version="2024-10-01-preview",
        ... )
        >>> response = await client.chat.completions.create(...)
    """
    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default",
    )
    logger.info("openai_client_created", endpoint=endpoint, api_version=api_version)
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
        timeout=timeout,
    )


# ── Event Hubs ────────────────────────────────────────────────────────────────

def create_event_hub_consumer(
    namespace: str,
    eventhub_name: str,
    consumer_group: str,
    checkpoint_storage_account: str,
    checkpoint_container: str,
) -> tuple[EventHubConsumerClient, BlobCheckpointStore]:
    """Create an Event Hub consumer client with Blob checkpoint store.

    Both the Event Hub namespace and the Blob Storage use Managed Identity.

    Args:
        namespace: Event Hub namespace name (e.g. 'ehns-banking-prod').
        eventhub_name: Event hub name (e.g. 'transactions').
        consumer_group: Consumer group (e.g. '$Default').
        checkpoint_storage_account: Storage account name for checkpoints.
        checkpoint_container: Blob container name for checkpoints.

    Returns:
        Tuple of (EventHubConsumerClient, BlobCheckpointStore).

    Required RBAC:
        - hot-path MI: 'Azure Event Hubs Data Receiver' on the transactions hub
        - hot-path MI: 'Storage Blob Data Contributor' on the checkpoints container

    Example:
        >>> consumer, store = create_event_hub_consumer(
        ...     namespace="ehns-banking-dev",
        ...     eventhub_name="transactions",
        ...     consumer_group="$Default",
        ...     checkpoint_storage_account="stbankingdev",
        ...     checkpoint_container="checkpoints",
        ... )
        >>> async with consumer:
        ...     await consumer.receive_batch(on_event_batch=handler)
    """
    credential = DefaultAzureCredential()

    checkpoint_store = BlobCheckpointStore(
        blob_account_url=f"https://{checkpoint_storage_account}.blob.core.windows.net",
        container_name=checkpoint_container,
        credential=credential,
    )

    fully_qualified_namespace = f"{namespace}.servicebus.windows.net"

    client = EventHubConsumerClient(
        fully_qualified_namespace=fully_qualified_namespace,
        eventhub_name=eventhub_name,
        consumer_group=consumer_group,
        checkpoint_store=checkpoint_store,
        credential=credential,
    )

    logger.info(
        "event_hub_consumer_created",
        namespace=fully_qualified_namespace,
        eventhub=eventhub_name,
        consumer_group=consumer_group,
    )
    return client, checkpoint_store


# ── Event Hubs Producer (for tests / dev tooling) ────────────────────────────

def create_event_hub_producer(namespace: str, eventhub_name: str):  # type: ignore[return]
    """Create an Event Hub producer client (used by test scripts / dev tools).

    Required RBAC:
        - Caller identity: 'Azure Event Hubs Data Sender' on the transactions hub

    Example:
        >>> producer = create_event_hub_producer("ehns-banking-dev", "transactions")
        >>> async with producer:
        ...     await producer.send_batch(batch)
    """
    from azure.eventhub.aio import EventHubProducerClient  # noqa: PLC0415

    credential = DefaultAzureCredential()
    return EventHubProducerClient(
        fully_qualified_namespace=f"{namespace}.servicebus.windows.net",
        eventhub_name=eventhub_name,
        credential=credential,
    )


# ── Azure Blob Storage ────────────────────────────────────────────────────────

def create_blob_service_client(storage_account: str):  # type: ignore[return]
    """Create a BlobServiceClient authenticated via Managed Identity.

    Used for: downloading the fastText model at boot, batch I/O for cold-path.

    Required RBAC:
        - Caller MI: 'Storage Blob Data Reader' (download) or
                     'Storage Blob Data Contributor' (upload) on the container.

    Example:
        >>> blob_svc = create_blob_service_client("stbankingdev")
        >>> container_client = blob_svc.get_container_client("models")
        >>> blob_client = container_client.get_blob_client("fasttext-2026-05.bin")
        >>> with open("model.bin", "wb") as f:
        ...     stream = blob_client.download_blob()
        ...     stream.readinto(f)
    """
    from azure.storage.blob import BlobServiceClient  # noqa: PLC0415
    from azure.identity import DefaultAzureCredential as SyncCredential  # noqa: PLC0415

    # Uses sync credential for the synchronous BlobServiceClient (model download at boot)
    credential = SyncCredential()
    url = f"https://{storage_account}.blob.core.windows.net"
    return BlobServiceClient(account_url=url, credential=credential)


# ── Key Vault (optional — for any residual secrets) ───────────────────────────

def create_keyvault_client(vault_name: str):  # type: ignore[return]
    """Create a SecretClient for Key Vault authenticated via Managed Identity.

    This is the fallback for any secret that cannot be expressed as a
    Managed Identity role (e.g. external webhooks). Prefer MI roles over secrets.

    Required RBAC:
        - Caller MI: 'Key Vault Secrets User' on the vault.

    Example:
        >>> kv = create_keyvault_client("kv-banking-prod")
        >>> secret = kv.get_secret("github-pat")
        >>> value = secret.value
    """
    from azure.keyvault.secrets import SecretClient  # noqa: PLC0415
    from azure.identity import DefaultAzureCredential as SyncCredential  # noqa: PLC0415

    credential = SyncCredential()
    vault_url = f"https://{vault_name}.vault.azure.net/"
    return SecretClient(vault_url=vault_url, credential=credential)
