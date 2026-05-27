# 🏦 Banking Transaction Categorizer

Sistema en Azure que clasifica transacciones bancarias en tiempo real y detecta anomalías de gasto.

## TL;DR

| Proceso | Tecnología | Trigger | Propósito |
|---|---|---|---|
| `hot-path` | FastAPI + asyncio en Azure Container Apps | Event Hubs | Clasifica cada transacción en < 3 s |
| `cold-path` | Durable Functions Python | Timer mensual | Resume financiero por usuario con Batch API |
| `mlops-pipeline` | Azure ML Pipeline | Timer mensual (día 5) | Reentrena fastText con casos difíciles |

**Autenticación:** 100 % Managed Identity (`DefaultAzureCredential`). Cero connection strings, cero API keys en código.

---

## Estructura del repositorio

```
banking-tx-categorizer/
├── apps/
│   ├── hot-path/        # Servicio Python FastAPI + consumidor Event Hubs
│   ├── cold-path/       # Azure Durable Functions (resúmenes mensuales)
│   └── mlops-pipeline/  # Azure ML Pipeline (reentrenamiento fastText)
├── shared/
│   └── schemas/         # Modelos Pydantic compartidos
└── docs/                # Arquitectura, runbook, modelo de datos
```

---

## Arranque local (< 1 hora)

### 1. Requisitos previos

- Python 3.11+
- [Azure CLI](https://docs.microsoft.com/cli/azure/install-azure-cli)
- Acceso a una suscripción Azure con los recursos desplegados
- (Opcional) Docker para builds locales

### 2. Login en Azure

```bash
az login
az account set --subscription <SUBSCRIPTION_ID>
```

`DefaultAzureCredential` usará tus credenciales de `az login` automáticamente.

### 3. Instalar el hot-path

```bash
cd apps/hot-path
pip install -e ".[dev]"
```

### 4. Configuración (variables de entorno)

Copia el ejemplo y edita con tus recursos:

```bash
cp .env.example .env
# edita .env con los nombres de tus recursos Azure
```

Variables mínimas para dev (ver `.env.example` para la lista completa):

| Variable | Ejemplo |
|---|---|
| `HOTPATH_EVENT_HUB_NAMESPACE` | `ehns-banking-dev` |
| `HOTPATH_CHECKPOINT_STORAGE_ACCOUNT` | `stbankingdev` |
| `HOTPATH_COSMOS_ACCOUNT` | `cosmos-banking-dev` |
| `HOTPATH_OPENAI_ENDPOINT` | `https://openai-banking-dev.openai.azure.com/` |
| `HOTPATH_FASTTEXT_MODEL_PATH` | `./ml_assets/model.bin` |
| `HOTPATH_APPLICATIONINSIGHTS_CONNECTION_STRING` | `InstrumentationKey=...` |
| `HOTPATH_ENABLE_SYNC_API` | `true` (solo dev) |

### 5. Modelo fastText (dev)

Para desarrollo, usa el script de entrenamiento con datos sintéticos:

```bash
cd apps/hot-path
python scripts/train_dev_model.py   # genera ml_assets/model.bin
```

O descarga uno pre-entrenado desde el Blob indicando `HOTPATH_FASTTEXT_MODEL_URI`.

### 6. Arrancar el servicio

```bash
python -m hot_path.main
# → servidor en http://localhost:8000
# → consumidor Event Hubs activo
```

Con sync API habilitado (`HOTPATH_ENABLE_SYNC_API=true`), puedes probar sin Event Hubs:

```bash
curl -X POST http://localhost:8000/v1/classify \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_id": "00000000-0000-0000-0000-000000000001",
    "user_id": "u_test",
    "amount": "-42.50",
    "currency": "EUR",
    "merchant_raw": "MERCADONA BARCELONA",
    "timestamp": "2026-05-27T10:00:00Z"
  }'
```

### 7. Tests

```bash
cd apps/hot-path
pytest tests/unit/ -v --cov=src/hot_path --cov-report=term-missing
```

---

## Autenticación con Azure (Managed Identity)

Todos los clientes Azure se crean en `apps/hot-path/src/hot_path/core/azure_clients.py`.
El patrón es siempre el mismo:

```python
from azure.identity.aio import DefaultAzureCredential

credential = DefaultAzureCredential()
# Usar 'credential' al construir cualquier SDK client
```

**En Azure (Container Apps / Functions / AML):**
- La Managed Identity del recurso actúa como identidad automáticamente.
- Asigna los roles RBAC necesarios a la MI (ver `docs/architecture.md`).

**En local:**
- `az login` provee las credenciales.
- Asigna los mismos roles RBAC a tu cuenta personal para dev.

Para user-assigned MI: establece `AZURE_CLIENT_ID` con el Client ID de la MI.

---

## RBAC mínimo requerido

| Identidad | Rol | Scope |
|---|---|---|
| MI hot-path | `Azure Event Hubs Data Receiver` | Event Hub `transactions` |
| MI hot-path | `Cosmos DB Built-in Data Contributor` | DB `banking` |
| MI hot-path | `Cognitive Services OpenAI User` | OpenAI account |
| MI hot-path | `Storage Blob Data Contributor` | Container `checkpoints` |
| MI cold-path | `Cosmos DB Built-in Data Contributor` | DB `banking` |
| MI cold-path | `Cognitive Services OpenAI Contributor` | OpenAI account |
| MI cold-path | `Storage Blob Data Contributor` | Container `batch-io` |
| MI AML compute | `Cosmos DB Built-in Data Reader` | Container `feedback_loop` |
| MI AML compute | `AcrPush` | Container Registry |

---

## TODO diferidos (fuera de alcance actual)

- [ ] Conversión fastText → ONNX para inferencia sin la lib nativa
- [ ] Multi-región activo-activo (Cosmos multi-write)
- [ ] Streaming insights en tiempo real (actualmente batch mensual)
- [ ] Reranking con embeddings antes del fallback L2
- [ ] Detección de fraude (diferente sistema, fuera del scope)
- [ ] A/B testing del modelo fastText

---

## Documentación adicional

- [`docs/architecture.md`](docs/architecture.md) — ADD completo con decisiones de diseño
- [`docs/data-model.md`](docs/data-model.md) — Esquema de documentos Cosmos DB
- [`docs/runbook.md`](docs/runbook.md) — Qué hacer cuando algo falla
- [`SPEC.md`](SPEC.md) — Especificación completa del sistema
