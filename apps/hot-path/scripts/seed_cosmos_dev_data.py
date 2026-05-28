"""Seed Cosmos DB with realistic synthetic banking data for dev/testing.

Creates:
  - processed transaction documents in `transactions`
  - statistical user profiles in `user_profiles`
  - low-confidence L1 -> L2 feedback records in `feedback_loop`

The script is idempotent for the same prefix: document IDs are deterministic.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity.aio import DefaultAzureCredential


CATEGORIES = (
    "groceries",
    "transport",
    "leisure",
    "housing",
    "health",
    "utilities",
    "income",
    "transfers",
    "other",
)

MERCHANTS: dict[str, list[str]] = {
    "groceries": [
        "MERCADONA SUPERMERC",
        "CARREFOUR MARKET",
        "LIDL",
        "ALCAMPO",
        "DIA SUPERMERCADO",
        "EROSKI",
        "CONSUM",
        "SUPERCOR",
    ],
    "transport": [
        "RENFE CERCANIAS",
        "METRO MADRID",
        "UBER TRIP",
        "CABIFY",
        "REPSOL",
        "CEPSA",
        "BP EXPRESS",
        "EMT MADRID",
    ],
    "leisure": [
        "NETFLIX",
        "SPOTIFY",
        "CINESA",
        "STEAM PURCHASE",
        "HBO MAX",
        "DISNEY PLUS",
        "AMAZON PRIME",
        "RESTAURANTE LA PLAZA",
        "BAR EL CENTRO",
    ],
    "housing": [
        "ALQUILER VIVIENDA",
        "HIPOTECA BANCO",
        "IKEA",
        "LEROY MERLIN",
        "COMUNIDAD VECINOS",
        "SEGURO HOGAR",
    ],
    "health": [
        "FARMACIA CENTRAL",
        "CLINICA DENTAL",
        "HOSPITAL PRIVADO",
        "GYM BASIC FIT",
        "OPTICA UNIVERSITARIA",
    ],
    "utilities": [
        "ENDESA ENERGIA",
        "IBERDROLA",
        "NATURGY",
        "VODAFONE",
        "MOVISTAR",
        "ORANGE",
        "CANAL ISABEL II",
    ],
    "income": [
        "NOMINA EMPRESA",
        "INGRESO PAYROLL",
        "DEVOLUCION HACIENDA",
    ],
    "transfers": [
        "BIZUM RECIBIDO",
        "BIZUM ENVIADO",
        "PAYPAL TRANSFER",
        "TRANSFERENCIA SEPA",
    ],
    "other": [
        "AMAZON EU",
        "APPLE.COM/BILL",
        "CORREOS",
        "DECATHLON",
        "ZARA",
        "EL CORTE INGLES",
    ],
}

MCC_BY_CATEGORY = {
    "groceries": "5411",
    "transport": "4111",
    "leisure": "5812",
    "housing": "6513",
    "health": "5912",
    "utilities": "4900",
    "income": None,
    "transfers": "4829",
    "other": None,
}

SPEND_RULES = {
    "groceries": (8, 85),
    "transport": (2, 55),
    "leisure": (4, 120),
    "housing": (350, 1150),
    "health": (6, 180),
    "utilities": (25, 180),
    "transfers": (5, 250),
    "other": (5, 220),
}


@dataclass(frozen=True)
class SeedUser:
    user_id: str
    profile_name: str
    salary: Decimal
    rent: Decimal
    tx_count: int


def money(value: float | Decimal) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def random_dt(rng: random.Random, start: datetime, end: datetime) -> datetime:
    seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=rng.randint(0, seconds))


def weighted_category(rng: random.Random) -> str:
    return rng.choices(
        population=[
            "groceries",
            "transport",
            "leisure",
            "health",
            "utilities",
            "transfers",
            "other",
        ],
        weights=[30, 16, 18, 7, 8, 10, 11],
        k=1,
    )[0]


def spend_amount(rng: random.Random, category: str, user: SeedUser) -> Decimal:
    if category == "housing":
        base = float(user.rent)
        value = rng.gauss(base, base * 0.04)
        return Decimal(money(-max(250, value)))

    low, high = SPEND_RULES[category]
    mode = (low + high) / 3
    value = rng.triangular(low, high, mode)

    if rng.random() < 0.015:
        value *= rng.uniform(3.5, 9.0)

    return Decimal(money(-value))


def recurring_transactions(user: SeedUser, start: datetime, end: datetime, prefix: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    month_cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    i = 0

    while month_cursor <= end:
        monthly = [
            ("income", user.salary, "NOMINA EMPRESA", 1),
            ("housing", -user.rent, "ALQUILER VIVIENDA", 3),
            ("utilities", Decimal("-64.90"), "IBERDROLA", 8),
            ("utilities", Decimal("-42.50"), "VODAFONE", 10),
            ("leisure", Decimal("-15.99"), "NETFLIX", 12),
            ("leisure", Decimal("-10.99"), "SPOTIFY", 14),
        ]
        for category, amount, merchant, day in monthly:
            if month_cursor.year == end.year and month_cursor.month == end.month and day > end.day:
                continue
            tx_time = month_cursor.replace(day=min(day, 28), hour=9 + (i % 10), minute=(i * 7) % 60)
            docs.append(
                {
                    "seed_index": i,
                    "id": f"{prefix}-{user.user_id}-{i:05d}",
                    "user_id": user.user_id,
                    "amount": money(amount),
                    "currency": "EUR",
                    "merchant_raw": merchant,
                    "merchant_mcc": MCC_BY_CATEGORY[category],
                    "timestamp": iso(tx_time),
                    "category": category,
                }
            )
            i += 1

        if month_cursor.month == 12:
            month_cursor = datetime(month_cursor.year + 1, 1, 1, tzinfo=UTC)
        else:
            month_cursor = datetime(month_cursor.year, month_cursor.month + 1, 1, tzinfo=UTC)

    return docs


def generate_user_transactions(
    rng: random.Random,
    user: SeedUser,
    start: datetime,
    end: datetime,
    prefix: str,
) -> list[dict[str, Any]]:
    docs = recurring_transactions(user, start, end, prefix)
    next_index = len(docs)

    while len(docs) < user.tx_count:
        category = weighted_category(rng)
        merchant = rng.choice(MERCHANTS[category])
        amount = spend_amount(rng, category, user)
        tx_time = random_dt(rng, start, end)

        docs.append(
            {
                "seed_index": next_index,
                "id": f"{prefix}-{user.user_id}-{next_index:05d}",
                "user_id": user.user_id,
                "amount": money(amount),
                "currency": "EUR",
                "merchant_raw": merchant,
                "merchant_mcc": MCC_BY_CATEGORY[category],
                "timestamp": iso(tx_time),
                "category": category,
            }
        )
        next_index += 1

    docs.sort(key=lambda d: d["timestamp"])
    return docs


def enrich_transactions(docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spends = [abs(float(d["amount"])) for d in docs if Decimal(d["amount"]) < 0]
    mean = sum(spends) / len(spends)
    variance = sum((x - mean) ** 2 for x in spends) / max(1, len(spends) - 1)
    stddev = math.sqrt(variance)
    merchant_counts = Counter(d["merchant_raw"] for d in docs if Decimal(d["amount"]) < 0)
    now = datetime.now(UTC)

    enriched: list[dict[str, Any]] = []
    for doc in docs:
        amount = Decimal(doc["amount"])
        z_score = 0.0
        is_anomaly = False
        reason = None
        if amount < 0 and stddev > 0:
            z_score = (abs(float(amount)) - mean) / stddev
            if z_score > 3:
                is_anomaly = True
                reason = (
                    f"Importe {abs(float(amount)):.2f} supera la media historica "
                    f"({mean:.2f}) en {z_score:.1f} sigma"
                )

        confidence = round(random.Random(doc["id"]).uniform(0.86, 0.99), 6)
        enriched.append(
            {
                "id": doc["id"],
                "user_id": doc["user_id"],
                "amount": doc["amount"],
                "currency": doc["currency"],
                "merchant_raw": doc["merchant_raw"],
                "merchant_mcc": doc["merchant_mcc"],
                "timestamp": doc["timestamp"],
                "category": doc["category"],
                "final_classifier": "l1",
                "confidence": confidence,
                "anomaly": {
                    "is_anomaly": is_anomaly,
                    "z_score": z_score,
                    "reason": reason,
                },
                "processed_at": iso(now),
                "pipeline_latency_ms": random.Random(doc["id"] + "lat").randint(0, 6),
            }
        )

    profile = {
        "id": docs[0]["user_id"],
        "user_id": docs[0]["user_id"],
        "mean_spend": money(mean),
        "stddev_spend": money(stddev),
        "transactions_count": len(docs),
        "top_merchants": [m for m, _ in merchant_counts.most_common(20)],
        "updated_at": iso(now),
    }
    return enriched, profile


def generate_feedback(prefix: str, year_month: str, per_category: int) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    examples = {
        "groceries": ["ULTRAMARINOS NOVA", "BIO MARKET LOCAL", "LA HUERTA ONLINE"],
        "transport": ["PARKING CENTRO", "ELECTRO CHARGE", "BUS INTERURBANO"],
        "leisure": ["ESCAPE ROOM ZONE", "TEATRO REAL", "MUSEO PASS"],
        "housing": ["FONTANERIA RUIZ", "CERRAJERO 24H", "REPARA HOGAR"],
        "health": ["FISIO CLINIC", "PARAFARMACIA ONLINE", "CENTRO MEDICO SUR"],
        "utilities": ["FIBRA LOCAL", "AGUA MUNICIPAL", "ENERGIA VERDE"],
        "income": ["BONUS EMPRESA", "REEMBOLSO NOMINA", "PAGA EXTRA"],
        "transfers": ["BIZUM ANA", "TRANSFER JOINT ACCOUNT", "PAYPAL PAGO"],
        "other": ["XyzXyzXyz.xyz", "SHOP-9QZ-UNKNOWN", "PAGO TPV SIN DATOS"],
    }
    now = datetime.now(UTC)
    i = 0
    for category in CATEGORIES:
        for n in range(per_category):
            merchant = examples[category][n % len(examples[category])]
            docs.append(
                {
                    "id": f"{prefix}-feedback-{category}-{n:03d}",
                    "year_month": year_month,
                    "transaction_id": f"{prefix}-feedback-tx-{i:05d}",
                    "merchant_raw": merchant,
                    "l1_prediction": {
                        "category": "other",
                        "confidence": round(0.18 + (n % 10) * 0.025, 3),
                    },
                    "l2_prediction": {
                        "category": category,
                        "rationale": f"Synthetic feedback label for {category}",
                    },
                    "captured_at": iso(now - timedelta(days=n % 28)),
                }
            )
            i += 1
    return docs


async def upsert_many(container: Any, docs: list[dict[str, Any]], concurrency: int, label: str) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    done = 0

    async def one(doc: dict[str, Any]) -> None:
        nonlocal done
        async with semaphore:
            for attempt in range(12):
                try:
                    await container.upsert_item(doc)
                    break
                except CosmosHttpResponseError as exc:
                    if exc.status_code != 429 or attempt == 11:
                        raise
                    retry_after_ms = 1000
                    if exc.headers:
                        retry_after_ms = int(exc.headers.get("x-ms-retry-after-ms", retry_after_ms))
                    await asyncio.sleep((retry_after_ms / 1000) + random.uniform(0.05, 0.25))
            done += 1
            if done % 500 == 0 or done == len(docs):
                print(f"{label}: {done}/{len(docs)}")

    await asyncio.gather(*(one(doc) for doc in docs))


async def count_query(container: Any, query: str, parameters: list[dict[str, Any]], **kwargs: Any) -> int:
    values = [
        item
        async for item in container.query_items(
            query=query,
            parameters=parameters,
            **kwargs,
        )
    ]
    if not values:
        return 0
    first = values[0]
    return int(first if isinstance(first, int) else first.get("$1", 0))


async def verify_seed(account: str, database: str, prefix: str, users: list[SeedUser]) -> None:
    credential = DefaultAzureCredential()
    url = f"https://{account}.documents.azure.com:443/"
    try:
        async with CosmosClient(url=url, credential=credential) as client:
            db = client.get_database_client(database)
            transactions = db.get_container_client("transactions")
            profiles = db.get_container_client("user_profiles")
            feedback = db.get_container_client("feedback_loop")

            total = await count_query(
                transactions,
                "SELECT VALUE COUNT(1) FROM c WHERE STARTSWITH(c.id, @prefix)",
                [{"name": "@prefix", "value": prefix}],
            )
            may_total = await count_query(
                transactions,
                "SELECT VALUE COUNT(1) FROM c WHERE STARTSWITH(c.id, @prefix) AND STARTSWITH(c.timestamp, @month)",
                [{"name": "@prefix", "value": prefix}, {"name": "@month", "value": "2026-05"}],
            )
            print(f"verify transactions total: {total}")
            print(f"verify transactions in 2026-05: {may_total}")

            for user in users:
                user_total = await count_query(
                    transactions,
                    "SELECT VALUE COUNT(1) FROM c WHERE c.user_id = @user_id AND STARTSWITH(c.id, @prefix)",
                    [{"name": "@user_id", "value": user.user_id}, {"name": "@prefix", "value": prefix}],
                    partition_key=user.user_id,
                )
                profile_total = await count_query(
                    profiles,
                    "SELECT VALUE COUNT(1) FROM c WHERE c.user_id = @user_id",
                    [{"name": "@user_id", "value": user.user_id}],
                    partition_key=user.user_id,
                )
                print(f"verify {user.user_id}: transactions={user_total}, profiles={profile_total}")

            feedback_total = await count_query(
                feedback,
                "SELECT VALUE COUNT(1) FROM c WHERE c.year_month = @year_month AND STARTSWITH(c.id, @prefix)",
                [{"name": "@year_month", "value": "2026-05"}, {"name": "@prefix", "value": f"{prefix}-feedback"}],
                partition_key="2026-05",
            )
            print(f"verify feedback_loop 2026-05: {feedback_total}")
    finally:
        await credential.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="cosmos-banking-dev-xkz")
    parser.add_argument("--database", default="banking")
    parser.add_argument("--prefix", default="seed-normal-year-v1")
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--transactions-per-user", type=int, default=2500)
    parser.add_argument("--feedback-per-category", type=int, default=35)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    end = datetime(2026, 5, 28, 23, 59, tzinfo=UTC)
    start = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
    users = [
        SeedUser("usr_seed_001", "single urban professional", Decimal("2450.00"), Decimal("875.00"), args.transactions_per_user),
        SeedUser("usr_seed_002", "family household", Decimal("3950.00"), Decimal("1250.00"), args.transactions_per_user),
        SeedUser("usr_seed_003", "student part-time", Decimal("980.00"), Decimal("420.00"), args.transactions_per_user),
        SeedUser("usr_seed_004", "remote worker", Decimal("3100.00"), Decimal("980.00"), args.transactions_per_user),
        SeedUser("usr_seed_005", "retired person", Decimal("1650.00"), Decimal("0.00"), args.transactions_per_user),
    ][: args.users]

    if args.verify_only:
        await verify_seed(args.account, args.database, args.prefix, users)
        return

    rng = random.Random(42727142)
    transaction_docs: list[dict[str, Any]] = []
    profile_docs: list[dict[str, Any]] = []

    by_category: defaultdict[str, int] = defaultdict(int)
    for user in users:
        raw_docs = generate_user_transactions(rng, user, start, end, args.prefix)
        enriched, profile = enrich_transactions(raw_docs)
        transaction_docs.extend(enriched)
        profile_docs.append(profile)
        for doc in enriched:
            by_category[doc["category"]] += 1

    rng.shuffle(transaction_docs)
    feedback_docs = generate_feedback(args.prefix, "2026-05", args.feedback_per_category)

    print(
        "Prepared "
        f"{len(transaction_docs)} transactions, "
        f"{len(profile_docs)} profiles, "
        f"{len(feedback_docs)} feedback records"
    )
    print("Transactions by category:", dict(sorted(by_category.items())))

    if args.dry_run:
        return

    credential = DefaultAzureCredential()
    url = f"https://{args.account}.documents.azure.com:443/"
    try:
        async with CosmosClient(url=url, credential=credential) as client:
            db = client.get_database_client(args.database)
            await upsert_many(db.get_container_client("transactions"), transaction_docs, args.concurrency, "transactions")
            await upsert_many(db.get_container_client("user_profiles"), profile_docs, args.concurrency, "user_profiles")
            await upsert_many(db.get_container_client("feedback_loop"), feedback_docs, args.concurrency, "feedback_loop")
    finally:
        await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
