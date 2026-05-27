# Modelo de datos — Cosmos DB

Todos los contenedores están en la base de datos `banking` de la cuenta Cosmos DB.

## Contenedor `transactions`

- **Partition key:** `/user_id`
- **TTL:** 13 meses

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "u_12345",
  "amount": "-42.50",
  "currency": "EUR",
  "merchant_raw": "MERCADONA BARCELONA",
  "merchant_mcc": "5411",
  "timestamp": "2026-05-26T10:23:00Z",
  "category": "groceries",
  "final_classifier": "l1",
  "confidence": 0.94,
  "anomaly": {
    "is_anomaly": false,
    "z_score": 1.2,
    "reason": null
  },
  "processed_at": "2026-05-26T10:23:01.245Z",
  "pipeline_latency_ms": 1245
}
```

## Contenedor `user_profiles`

- **Partition key:** `/user_id`
- **TTL:** desactivado

```json
{
  "id": "u_12345",
  "user_id": "u_12345",
  "mean_spend": "65.30",
  "stddev_spend": "23.10",
  "transactions_count": 1247,
  "top_merchants": ["MERCADONA", "RENFE", "AMAZON"],
  "updated_at": "2026-05-26T10:23:01Z"
}
```

## Contenedor `feedback_loop`

- **Partition key:** `/year_month`
- **TTL:** 6 meses

```json
{
  "id": "uuid",
  "year_month": "2026-05",
  "transaction_id": "uuid",
  "merchant_raw": "FARMACIA SAN MIGUEL S.L.",
  "l1_prediction": {"category": "other", "confidence": 0.42},
  "l2_prediction": {"category": "health", "rationale": "Farmacia identificada"},
  "captured_at": "2026-05-26T10:23:01Z"
}
```

## Contenedor `insights`

- **Partition key:** `/user_id`
- **TTL:** 12 meses

```json
{
  "id": "u_12345_2026-05",
  "user_id": "u_12345",
  "year_month": "2026-05",
  "summary_text": "Este mes has gastado 1.234€...",
  "breakdown": {"groceries": "423.10", "transport": "89.50"},
  "generated_at": "2026-06-01T02:34:00Z"
}
```

## Contenedor `dlq` (Dead Letter Queue)

- **Partition key:** `/captured_at` (auto-created, no TTL needed)
- Contiene mensajes de Event Hubs que fallaron procesamiento

```json
{
  "id": "uuid",
  "raw_body": "...",
  "error": "ValueError: ...",
  "traceback": "...",
  "captured_at": "2026-05-26T10:23:01Z"
}
```
