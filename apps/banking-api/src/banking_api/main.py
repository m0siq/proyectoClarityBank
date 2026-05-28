from __future__ import annotations

import asyncio
import json
import random
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import fasttext
import orjson
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from azure.eventhub import EventData
from azure.eventhub.aio import EventHubProducerClient
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from azure.storage.blob.aio import BlobClient
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from openai import AsyncAzureOpenAI
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BANKING_",
        protected_namespaces=("settings_",),
    )

    cosmos_account: str = "cosmos-banking-dev-xkz"
    cosmos_database: str = "banking"
    event_hub_namespace: str = "ehns-banking-dev-xkz"
    event_hub_name: str = "transactions"
    openai_endpoint: str = "https://openai-banking-dev-xkz.openai.azure.com/"
    openai_deployment: str = "gpt-4o-mini"
    openai_api_version: str = "2024-10-01-preview"
    model_blob_account: str = "stbankingdevxkz"
    model_blob_container: str = "models"
    cors_origins: str = "*"


class TransactionCreate(BaseModel):
    user_id: str
    amount: Decimal
    merchant_raw: str = Field(min_length=2, max_length=160)
    currency: str = "EUR"
    merchant_mcc: str | None = None
    timestamp: datetime | None = None


class ColdPathRun(BaseModel):
    year_month: str = "2026-05"
    user_id: str | None = None


class MlopsRun(BaseModel):
    year_month: str = "2026-05"
    publish_to_blob: bool = False


settings = Settings()


def dumps(value: Any) -> bytes:
    return orjson.dumps(value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    credential = DefaultAzureCredential()
    app.state.credential = credential
    app.state.cosmos = CosmosClient(
        url=f"https://{settings.cosmos_account}.documents.azure.com:443/",
        credential=credential,
    )
    app.state.producer = EventHubProducerClient(
        fully_qualified_namespace=f"{settings.event_hub_namespace}.servicebus.windows.net",
        eventhub_name=settings.event_hub_name,
        credential=credential,
    )
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default",
    )
    app.state.openai = AsyncAzureOpenAI(
        azure_endpoint=settings.openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version=settings.openai_api_version,
    )
    try:
        yield
    finally:
        await app.state.producer.close()
        await app.state.cosmos.close()
        await credential.close()


app = FastAPI(default_response_class=ORJSONResponse, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def container(request_app: FastAPI, name: str):
    return (
        request_app.state.cosmos
        .get_database_client(settings.cosmos_database)
        .get_container_client(name)
    )


async def query_all(query_iterable: Any) -> list[Any]:
    return [item async for item in query_iterable]


def month_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def normalize_record(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    for key in ("_rid", "_self", "_etag", "_attachments", "_ts"):
        item.pop(key, None)
    return item


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/users")
async def users(month: str = Query(default="2026-05")) -> list[dict[str, Any]]:
    profiles_c = container(app, "user_profiles")
    transactions_c = container(app, "transactions")

    profile_items = await query_all(profiles_c.query_items("SELECT * FROM c"))
    profiles = {item["user_id"]: normalize_record(item) for item in profile_items}

    active = await query_all(
        transactions_c.query_items(
            "SELECT DISTINCT c.user_id FROM c WHERE STARTSWITH(c.timestamp, @month)",
            parameters=[{"name": "@month", "value": month}],
        )
    )
    user_ids = sorted({item["user_id"] for item in active} | set(profiles))

    result = []
    for user_id in user_ids:
        profile = profiles.get(user_id, {})
        result.append(
            {
                "user_id": user_id,
                "display_name": user_id.replace("usr_seed_", "Cuenta "),
                "transactions_count": profile.get("transactions_count", 0),
                "mean_spend": profile.get("mean_spend", "0"),
                "stddev_spend": profile.get("stddev_spend", "0"),
                "top_merchants": profile.get("top_merchants", []),
            }
        )
    return result


@app.get("/api/users/{user_id}/profile")
async def user_profile(user_id: str) -> dict[str, Any]:
    profiles_c = container(app, "user_profiles")
    try:
        return normalize_record(await profiles_c.read_item(item=user_id, partition_key=user_id))
    except CosmosResourceNotFoundError:
        raise HTTPException(status_code=404, detail="profile_not_found") from None


@app.get("/api/users/{user_id}/transactions")
async def user_transactions(
    user_id: str,
    month: str | None = Query(default="2026-05"),
    limit: int = Query(default=160, ge=1, le=500),
) -> list[dict[str, Any]]:
    tx_c = container(app, "transactions")
    if month:
        query = (
            "SELECT * FROM c WHERE c.user_id = @user_id "
            "AND STARTSWITH(c.timestamp, @month) "
            f"ORDER BY c.timestamp DESC OFFSET 0 LIMIT {limit}"
        )
        params = [{"name": "@user_id", "value": user_id}, {"name": "@month", "value": month}]
    else:
        query = (
            "SELECT * FROM c WHERE c.user_id = @user_id "
            f"ORDER BY c.timestamp DESC OFFSET 0 LIMIT {limit}"
        )
        params = [{"name": "@user_id", "value": user_id}]
    items = await query_all(tx_c.query_items(query=query, parameters=params, partition_key=user_id))
    return [normalize_record(item) for item in items]


@app.post("/api/transactions")
async def create_transaction(payload: TransactionCreate) -> dict[str, Any]:
    transaction_id = str(uuid.uuid4())
    timestamp = payload.timestamp or datetime.now(UTC)
    event_payload = {
        "transaction_id": transaction_id,
        "user_id": payload.user_id,
        "amount": str(payload.amount),
        "currency": payload.currency,
        "merchant_raw": payload.merchant_raw,
        "merchant_mcc": payload.merchant_mcc,
        "timestamp": timestamp.astimezone(UTC).isoformat(),
    }
    batch = await app.state.producer.create_batch(partition_key=payload.user_id)
    batch.add(EventData(json.dumps(event_payload)))
    await app.state.producer.send_batch(batch)
    return {
        "transaction_id": transaction_id,
        "user_id": payload.user_id,
        "status": "sent",
    }


@app.get("/api/transactions/{transaction_id}")
async def transaction_status(transaction_id: str, user_id: str | None = None) -> dict[str, Any]:
    tx_c = container(app, "transactions")
    if user_id:
        try:
            return {"found": True, "transaction": normalize_record(await tx_c.read_item(transaction_id, user_id))}
        except CosmosResourceNotFoundError:
            return {"found": False}

    items = await query_all(
        tx_c.query_items(
            "SELECT * FROM c WHERE c.id = @id",
            parameters=[{"name": "@id", "value": transaction_id}],
        )
    )
    return {"found": bool(items), "transaction": normalize_record(items[0]) if items else None}


async def list_active_users(year_month: str, user_id: str | None) -> list[str]:
    if user_id:
        return [user_id]
    tx_c = container(app, "transactions")
    items = await query_all(
        tx_c.query_items(
            "SELECT DISTINCT c.user_id FROM c WHERE STARTSWITH(c.timestamp, @month)",
            parameters=[{"name": "@month", "value": year_month}],
        )
    )
    return sorted(item["user_id"] for item in items)


async def aggregate_user(user_id: str, year_month: str) -> dict[str, Any]:
    tx_c = container(app, "transactions")
    items = await query_all(
        tx_c.query_items(
            "SELECT c.category, c.amount FROM c "
            "WHERE c.user_id = @user_id AND STARTSWITH(c.timestamp, @month)",
            parameters=[
                {"name": "@user_id", "value": user_id},
                {"name": "@month", "value": year_month},
            ],
            partition_key=user_id,
        )
    )
    breakdown: defaultdict[str, float] = defaultdict(float)
    income = 0.0
    count = 0
    for item in items:
        amount = float(item["amount"])
        if amount < 0:
            breakdown[item.get("category", "other")] += abs(amount)
            count += 1
        else:
            income += amount
    return {
        "user_id": user_id,
        "year_month": year_month,
        "breakdown": dict(breakdown),
        "income": income,
        "transactions_count": count,
    }


def fallback_summary(agg: dict[str, Any]) -> str:
    total = sum(float(value) for value in agg["breakdown"].values())
    top = max(agg["breakdown"], key=agg["breakdown"].get, default="sin movimientos")
    return (
        f"En {agg['year_month']} el gasto fue de {total:.2f} EUR. "
        f"La categoria con mas peso fue {top}. "
        "Mantener revisiones semanales ayudara a anticipar desviaciones."
    )


async def generate_insight(agg: dict[str, Any]) -> dict[str, Any]:
    try:
        breakdown_lines = "\n".join(
            f"- {category}: {amount:.2f} EUR"
            for category, amount in sorted(agg["breakdown"].items(), key=lambda item: item[1], reverse=True)
        )
        response = await app.state.openai.chat.completions.create(
            model=settings.openai_deployment,
            temperature=0.2,
            max_tokens=180,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres un asesor financiero bancario. Escribe un resumen breve, "
                        "claro y profesional en espanol. No menciones modelos ni sistemas internos."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Usuario: {agg['user_id']}\n"
                        f"Mes: {agg['year_month']}\n"
                        f"Ingresos: {agg['income']:.2f} EUR\n"
                        f"Movimientos de gasto: {agg['transactions_count']}\n"
                        f"Desglose:\n{breakdown_lines}"
                    ),
                },
            ],
        )
        summary = response.choices[0].message.content or fallback_summary(agg)
    except Exception:
        summary = fallback_summary(agg)
    return {
        "user_id": agg["user_id"],
        "year_month": agg["year_month"],
        "summary_text": summary,
        "breakdown": agg["breakdown"],
        "income": agg["income"],
        "transactions_count": agg["transactions_count"],
        "generated_at": datetime.now(UTC).isoformat(),
    }


@app.post("/api/cold-path/run")
async def run_cold_path(payload: ColdPathRun) -> dict[str, Any]:
    users_for_month = await list_active_users(payload.year_month, payload.user_id)
    aggregated = [await aggregate_user(user_id, payload.year_month) for user_id in users_for_month]
    aggregated = [item for item in aggregated if item["transactions_count"] > 0]
    insights = await asyncio.gather(*(generate_insight(item) for item in aggregated))

    insights_c = container(app, "insights")
    saved = 0
    for item in insights:
        doc = dict(item)
        doc["id"] = f"{item['user_id']}_{item['year_month']}"
        await insights_c.upsert_item(doc)
        saved += 1
    return {"processed": len(aggregated), "saved": saved, "insights": insights}


@app.get("/api/insights")
async def get_insight(user_id: str, year_month: str = Query(default="2026-05")) -> dict[str, Any]:
    insights_c = container(app, "insights")
    doc_id = f"{user_id}_{year_month}"
    try:
        return {"found": True, "insight": normalize_record(await insights_c.read_item(doc_id, user_id))}
    except CosmosResourceNotFoundError:
        return {"found": False, "insight": None}


@app.get("/api/mlops/feedback")
async def mlops_feedback(year_month: str = Query(default="2026-05")) -> dict[str, Any]:
    feedback_c = container(app, "feedback_loop")
    records = await query_all(
        feedback_c.query_items(
            "SELECT * FROM c WHERE c.year_month = @year_month",
            parameters=[{"name": "@year_month", "value": year_month}],
            partition_key=year_month,
        )
    )
    labels = Counter(item.get("l2_prediction", {}).get("category", "other") for item in records)
    avg_conf = 0.0
    if records:
        avg_conf = sum(float(item.get("l1_prediction", {}).get("confidence", 0)) for item in records) / len(records)
    return {
        "year_month": year_month,
        "total": len(records),
        "labels": dict(sorted(labels.items())),
        "avg_l1_confidence": avg_conf,
    }


def to_fasttext_line(record: dict[str, Any]) -> str:
    label = record.get("l2_prediction", {}).get("category", "other")
    merchant = str(record.get("merchant_raw", "")).lower()
    safe = "".join(ch if ch.isalpha() or ch.isspace() else " " for ch in merchant)
    safe = " ".join(safe.split())
    return f"__label__{label} {safe}"


@app.post("/api/mlops/run-local")
async def run_local_mlops(payload: MlopsRun) -> dict[str, Any]:
    feedback_c = container(app, "feedback_loop")
    records = await query_all(
        feedback_c.query_items(
            "SELECT * FROM c WHERE c.year_month = @year_month",
            parameters=[{"name": "@year_month", "value": payload.year_month}],
            partition_key=payload.year_month,
        )
    )
    if len(records) < 30:
        raise HTTPException(status_code=400, detail="not_enough_feedback")

    rng = random.Random(20260528)
    rng.shuffle(records)
    split = max(1, int(len(records) * 0.8))
    train_records = records[:split]
    test_records = records[split:] or records[:]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        train_path = tmp_path / "train.txt"
        test_path = tmp_path / "test.txt"
        model_path = tmp_path / f"fasttext-{payload.year_month}.bin"
        train_path.write_text("\n".join(to_fasttext_line(record) for record in train_records), encoding="utf-8")
        test_path.write_text("\n".join(to_fasttext_line(record) for record in test_records), encoding="utf-8")

        model = fasttext.train_supervised(
            input=str(train_path),
            lr=0.5,
            epoch=25,
            wordNgrams=2,
            bucket=200000,
            dim=100,
            verbose=0,
        )
        n_test, precision, recall = model.test(str(test_path))
        model.save_model(str(model_path))
        model_size = model_path.stat().st_size

        blob_url = None
        if payload.publish_to_blob and settings.model_blob_account:
            blob_url = (
                f"https://{settings.model_blob_account}.blob.core.windows.net/"
                f"{settings.model_blob_container}/fasttext-{payload.year_month}-{uuid.uuid4().hex[:8]}.bin"
            )
            blob_client = BlobClient.from_blob_url(blob_url, credential=app.state.credential)
            with model_path.open("rb") as f:
                await blob_client.upload_blob(f, overwrite=True)
            await blob_client.close()

    return {
        "year_month": payload.year_month,
        "feedback_records": len(records),
        "train_records": len(train_records),
        "test_records": len(test_records),
        "precision_at_1": precision,
        "recall_at_1": recall,
        "test_examples": n_test,
        "model_size_bytes": model_size,
        "published_model_uri": blob_url,
    }
