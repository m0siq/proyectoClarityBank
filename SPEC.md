# Especificación de desarrollo — Sistema de categorización de transacciones bancarias

> **Audiencia de este documento:** una IA programadora (Claude Code, Cursor, GitHub Copilot Workspace o similar) que recibirá este archivo como única fuente de verdad y deberá generar el repositorio completo. Por eso es deliberadamente exhaustivo y prescriptivo: nombres de archivo, nombres de clase, firmas de función, versiones de librerías, variables de entorno, esquemas de datos y orden de implementación.
>
> **Si una decisión no aparece en este documento, asume el comportamiento más conservador (más logs, más tests, menos optimización prematura) y documéntalo en un comentario `# DECISION:` en el código.**

---

## 0. TL;DR (lee esto primero)

Construye un sistema en Azure que clasifica transacciones bancarias en categorías financieras y detecta anomalías de gasto, en menos de 3 segundos por transacción, para 340.000 usuarios.

El sistema se compone de **tres procesos independientes** (no microservicios — ver §2):

1. **`hot-path`** — servicio Python/FastAPI que corre en Azure Container Apps. Consume transacciones de Azure Event Hubs, las pasa por un pipeline de 4 etapas en memoria (anomalías → fastText L1 → router de confianza → fallback opcional a Azure OpenAI), y persiste en Cosmos DB. Es un **monolito modular**, no se descompone en microservicios.
2. **`cold-path`** — Azure Durable Functions que se ejecuta mensualmente. Agrega gasto por usuario, lo anonimiza, lo envía a la Batch API de Azure OpenAI y guarda un resumen en lenguaje natural en Cosmos DB.
3. **`mlops-pipeline`** — Azure ML pipeline que se ejecuta mensualmente. Lee la colección `feedback_loop` de Cosmos DB, reentrena fastText con los casos difíciles y publica una nueva imagen de contenedor al ACR.

Todo vive dentro de una Azure VNet con private endpoints y residencia UE. Sin connection strings: solo Managed Identities.

**Orden de construcción obligatorio:** §14. No saltes sprints.

---

## 1. Contexto del producto

### 1.1 Problema

Una entidad bancaria con 340.000 clientes activos quiere ofrecer en su app dos features:
- Clasificación automática de cada transacción en categorías (alimentación, transporte, ocio, vivienda, salud, etc.).
- Detección de anomalías ("este cargo es muy alto comparado con lo que sueles gastar").

Ambas cosas deben aparecer en la app en menos de 3 segundos desde que la transacción llega al sistema central del banco.

### 1.2 KPIs y SLAs

| Métrica | Objetivo |
|---|---|
| Latencia p95 end-to-end (Event Hubs in → Cosmos write) | < 3.000 ms |
| Latencia p95 del clasificador L1 (fastText) | < 5 ms |
| Tasa de transacciones que requieren fallback L2 | < 10 % |
| Coste por 1M transacciones procesadas | minimizar; objetivo blando ~5–10 € |
| Disponibilidad del hot path | 99,9 % mensual |
| RPO | 1 hora (replicación geo de Cosmos DB asíncrona) |
| Residencia del dato | UE únicamente |

### 1.3 Carga esperada

- Pico sostenido: 500 tx/s (cobro de nóminas a fin de mes).
- Pico transitorio: 2.000 tx/s (Black Friday, primer día del mes).
- Media diaria: 50–80 tx/s.

El consumidor de Event Hubs debe estar dimensionado para el pico transitorio y escalar a cero (o a un mínimo bajo) en horas valle.

### 1.4 Fuera de alcance (no implementar)

- Frontend de usuario final (otra app del banco lo consume).
- Autenticación de usuarios finales (el banco la hace antes).
- Ingesta directa desde el core bancario (lo da hecho otro equipo; este proyecto **solo** consume de Event Hubs).
- Multi-tenancy (un solo tenant: el banco contratante).

---

## 2. Decisión arquitectónica

### 2.1 Decisión

**Monolito modular para el hot path + servicios independientes por dominio temporal para los flujos asíncronos.**

Concretamente:
- Un solo proceso Python (`hot-path`) maneja consumo de Event Hubs, las 4 etapas del pipeline síncrono y la escritura en Cosmos. **No se separa en microservicios.**
- El `cold-path` (Durable Functions) es un proceso distinto porque su ciclo de vida es distinto (corre 1 vez al mes), no porque sea otra "capa".
- El `mlops-pipeline` (Azure ML) es un proceso distinto por la misma razón.

### 2.2 Por qué monolito modular y no microservicios

Cuatro razones, en orden de importancia:

**1. Latencia.** El SLA de 3 s end-to-end deja muy poco margen. Cada salto de red entre microservicios añade entre 5 y 50 ms de baseline (DNS, TLS handshake reusado, serialización JSON, deserialización). Si las 4 etapas del pipeline fueran 4 microservicios, son 3 saltos extra, hasta 150 ms quemados sin hacer ningún trabajo útil. En memoria, esos saltos cuestan microsegundos.

**2. fastText debe vivir en memoria una sola vez.** El modelo entrenado pesa entre 50 y 500 MB. Si el clasificador L1 fuera un microservicio aparte del consumidor de Event Hubs, necesitarías N réplicas del clasificador con su propia copia del modelo, y N réplicas del consumidor, escalando independientemente. KEDA escalaría dos cosas en lugar de una, con el riesgo de desincronización. Con monolito, una réplica = un modelo cargado = una unidad de escalado.

**3. Bounded context único.** "Categorizar una transacción en tiempo real" es un dominio cohesivo en el sentido de DDD. Las 4 etapas no son features independientes que evolucionarán por separado: o todas funcionan o ninguna sirve. Separarlas como microservicios sería separación por *capa técnica*, no por *dominio*, que es el antipatrón clásico.

**4. Coste operacional.** Microservicios exigen un service mesh o equivalente (Dapr, Linkerd), tracing distribuido obligatorio, contratos versionados entre servicios, despliegues coordinados, observabilidad correlada. Un proyecto de clase con 1–3 personas no tiene presupuesto humano para eso. El monolito modular permite refactorizar a microservicios *después* si llega el caso (poco probable a 340k usuarios).

### 2.3 Por qué sí se separa cold-path y mlops-pipeline

Los tres procesos comparten el almacén de datos (Cosmos DB) pero **no** comparten ciclo de vida:

| Proceso | Frecuencia | Trigger | Lifetime |
|---|---|---|---|
| `hot-path` | Continuo | Mensaje en Event Hubs | Always-on (auto-escalado por KEDA) |
| `cold-path` | Mensual | Timer (1 de cada mes a las 02:00 UTC) | Minutos (job) |
| `mlops-pipeline` | Mensual | Timer (5 de cada mes a las 03:00 UTC) | Horas (job) |

Meter un timer mensual dentro del hot path significaría que el código del job batch viaja en cada imagen del hot path, comparte memoria, y un bug en el job mensual podría tumbar el sistema síncrono. Separación correcta.

### 2.4 Diagrama de componentes lógicos

```
                       ┌─────────────────────────────────────┐
                       │       Azure VNet (residencia UE)     │
                       │                                       │
[Banca Core] ──tx──> ──┼─> [Event Hubs] ──> [hot-path ACA] ──┼─> [Cosmos DB]
                       │                          │            │      ▲
                       │                          └─> [Az OpenAI L2] │
                       │                                              │
                       │   [cold-path Durable Fns] ─────> [Az OpenAI Batch]
                       │              │                              │
                       │              └──────────────────────────────┘
                       │                                              │
                       │   [mlops-pipeline AML] ◄─reads feedback──────┘
                       │              │
                       │              └─publishes─> [ACR] ─pulled by─> [hot-path]
                       └─────────────────────────────────────────────┘
```

---

## 3. Stack tecnológico (versiones fijadas)

> **Importante:** usa exactamente estas versiones. Si una no existe en el momento del desarrollo, usa la **inmediatamente anterior estable**, no la siguiente. Documenta el cambio.

### 3.1 `hot-path` (Python)

```
python                      = 3.11.*
fastapi                     = 0.115.*
uvicorn[standard]           = 0.32.*
pydantic                    = 2.9.*
pydantic-settings           = 2.6.*
azure-eventhub              = 5.12.*
azure-eventhub-checkpointstoreblob-aio = 1.2.*
azure-cosmos                = 4.7.*
azure-identity              = 1.19.*
azure-keyvault-secrets      = 4.8.*
openai                      = 1.54.*       # cliente para Azure OpenAI
fasttext                    = 0.9.2        # entrenamiento e inferencia (ver §5.8)
numpy                       = 1.26.*
orjson                      = 3.10.*       # serialización rápida
opentelemetry-api           = 1.27.*
opentelemetry-sdk           = 1.27.*
opentelemetry-instrumentation-fastapi = 0.48b0
azure-monitor-opentelemetry = 1.6.*
tenacity                    = 9.0.*        # retries con backoff
structlog                   = 24.4.*
```

Dev only:
```
pytest                      = 8.3.*
pytest-asyncio              = 0.24.*
pytest-cov                  = 5.0.*
httpx                       = 0.27.*       # tests del API
ruff                        = 0.7.*        # linter + formatter (sustituye black + flake8 + isort)
mypy                        = 1.13.*
pre-commit                  = 4.0.*
locust                      = 2.32.*       # tests de carga
```

### 3.2 `cold-path` (Python en Azure Functions)

```
azure-functions             = 1.21.*
azure-functions-durable     = 1.2.*
azure-cosmos                = 4.7.*
openai                      = 1.54.*
azure-identity              = 1.19.*
pydantic                    = 2.9.*
```

### 3.3 `mlops-pipeline`

```
azure-ai-ml                 = 1.21.*
azure-identity              = 1.19.*
fasttext                    = 0.9.2
scikit-learn                = 1.5.*        # para split y métricas
pandas                      = 2.2.*
mlflow                      = 2.18.*       # tracking de experimentos (lo trae AML)
```

### 3.4 Infraestructura

- **Bicep** (no Terraform) para IaC. Es nativo de Azure, menos boilerplate y mejor IntelliSense.
- **GitHub Actions** para CI/CD.
- **Azure Container Registry** (ACR) para imágenes Docker.
- **Application Insights** para observabilidad.
- **Azure Key Vault** para secretos (aunque preferimos Managed Identities).

---

## 4. Estructura del monorepo

Usa un único repositorio Git con esta estructura. **No crees repositorios separados por servicio.**

```
banking-tx-categorizer/
├── README.md                         # Quickstart, ver §15
├── SPEC.md                           # Este archivo
├── .gitignore                        # Python + IDE + Azure
├── .editorconfig
├── .pre-commit-config.yaml
├── pyproject.toml                    # raíz: solo herramientas (ruff, mypy)
│
├── apps/
│   ├── hot-path/                     # §5
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   ├── .dockerignore
│   │   ├── src/
│   │   │   └── hot_path/
│   │   │       ├── __init__.py
│   │   │       ├── main.py           # entry point: arranca API + consumer
│   │   │       ├── core/
│   │   │       │   ├── __init__.py
│   │   │       │   ├── config.py     # §5.4
│   │   │       │   ├── logging.py    # §5.13
│   │   │       │   └── telemetry.py  # §5.13
│   │   │       ├── domain/
│   │   │       │   ├── __init__.py
│   │   │       │   ├── models.py     # §5.5
│   │   │       │   └── errors.py
│   │   │       ├── repositories/
│   │   │       │   ├── __init__.py
│   │   │       │   ├── cosmos.py     # cliente base
│   │   │       │   ├── transactions.py
│   │   │       │   ├── profiles.py
│   │   │       │   └── feedback.py
│   │   │       ├── services/
│   │   │       │   ├── __init__.py
│   │   │       │   ├── anomaly.py    # §5.7
│   │   │       │   ├── classifier_l1.py  # §5.8
│   │   │       │   ├── classifier_l2.py  # §5.9
│   │   │       │   └── pipeline.py   # §5.10  (orquesta las 4 etapas)
│   │   │       ├── consumers/
│   │   │       │   ├── __init__.py
│   │   │       │   └── event_hub.py  # §5.11
│   │   │       ├── api/
│   │   │       │   ├── __init__.py
│   │   │       │   ├── app.py        # construye la app FastAPI
│   │   │       │   └── routes/
│   │   │       │       ├── __init__.py
│   │   │       │       ├── health.py
│   │   │       │       └── classify.py  # endpoint síncrono opcional
│   │   │       └── ml_assets/
│   │   │           └── .gitkeep      # aquí se baja el modelo en runtime
│   │   └── tests/
│   │       ├── unit/
│   │       ├── integration/
│   │       └── load/
│   │
│   ├── cold-path/                    # §6
│   │   ├── host.json
│   │   ├── requirements.txt
│   │   ├── local.settings.json.example
│   │   ├── function_app.py           # entrypoint Durable Functions
│   │   ├── activities/
│   │   │   ├── __init__.py
│   │   │   ├── aggregate_user.py
│   │   │   ├── submit_batch.py
│   │   │   └── persist_insight.py
│   │   ├── orchestrators/
│   │   │   ├── __init__.py
│   │   │   └── monthly_insights.py
│   │   ├── shared/
│   │   │   └── prompts.py
│   │   └── tests/
│   │
│   └── mlops-pipeline/               # §7
│       ├── pyproject.toml
│       ├── pipeline.py               # define el AML pipeline
│       ├── components/
│       │   ├── extract_feedback/
│       │   │   ├── component.yaml
│       │   │   └── extract.py
│       │   ├── train_fasttext/
│       │   │   ├── component.yaml
│       │   │   └── train.py
│       │   ├── evaluate/
│       │   │   ├── component.yaml
│       │   │   └── evaluate.py
│       │   └── publish_model/
│       │       ├── component.yaml
│       │       └── publish.py
│       └── tests/
│
├── shared/
│   └── schemas/                      # JSON Schemas / Pydantic compartidos
│       ├── transaction.py
│       └── README.md
│
├── infra/                            # §9
│   ├── main.bicep                    # punto de entrada
│   ├── modules/
│   │   ├── network.bicep
│   │   ├── cosmos.bicep
│   │   ├── event_hubs.bicep
│   │   ├── container_apps.bicep
│   │   ├── openai.bicep
│   │   ├── functions.bicep
│   │   ├── aml.bicep
│   │   ├── observability.bicep
│   │   └── identity.bicep
│   ├── parameters/
│   │   ├── dev.bicepparam
│   │   ├── staging.bicepparam
│   │   └── prod.bicepparam
│   └── README.md
│
├── docs/
│   ├── architecture.md               # copia del ADD original
│   ├── runbook.md                    # qué hacer cuando algo falla
│   ├── data-model.md                 # §8
│   └── diagrams/
│
└── .github/
    └── workflows/
        ├── ci-hot-path.yml
        ├── ci-cold-path.yml
        ├── ci-mlops.yml
        ├── cd-hot-path.yml
        ├── cd-cold-path.yml
        └── infra-deploy.yml
```

---

## 5. Servicio `hot-path` (núcleo del sistema)

### 5.1 Resumen

Proceso Python que arranca dos cosas en paralelo dentro del mismo contenedor:
1. Un servidor HTTP FastAPI (puerto 8000) que expone `/health`, `/ready` y opcionalmente `/v1/classify` para uso síncrono de pruebas.
2. Un consumer asíncrono de Azure Event Hubs que es el **driver principal**: lee mensajes, los pasa por el pipeline, escribe en Cosmos.

Ambas comparten el mismo event loop de asyncio, las mismas instancias de servicios (anomaly, L1, L2) y el mismo modelo fastText cargado en memoria. Esa unicidad es la razón de ser del monolito.

### 5.2 Flujo end-to-end de una transacción

Cuando llega un mensaje a Event Hubs, el consumer ejecuta esta secuencia:

```
1. Deserializa el mensaje → Transaction (Pydantic)
2. Lee el perfil del usuario desde Cosmos (con caché en memoria por TTL=5 min)
3. Etapa A: anomaly.detect(transaction, profile)         → AnomalyResult
4. Etapa B: classifier_l1.classify(transaction)          → ClassificationL1
5. Etapa C: si l1.confidence < 0.85:
              classifier_l2.classify(transaction)        → ClassificationL2
              feedback_repo.record(transaction, l1, l2)
            sino: final = l1
6. Construye TransactionProcessed con category + anomaly_flag
7. Escribe en Cosmos: transactions_repo.save(processed)
8. Actualiza estadísticas del perfil asincrónicamente (fire-and-forget):
   profiles_repo.update_stats_async(user_id, transaction.amount)
9. Emite métricas a App Insights
10. Acknowledge el mensaje en Event Hubs (checkpoint)
```

Si cualquier paso falla, ver §5.15 (manejo de errores).

### 5.3 Módulo `core/config.py`

Usa `pydantic-settings`. Toda la configuración llega por variables de entorno. **Cero hardcoded.** Cero archivos `.env` en producción (esos solo en local).

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AnyUrl

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HOTPATH_")

    # Azure identity
    azure_client_id: str | None = None  # solo si usas user-assigned MI

    # Event Hubs
    event_hub_namespace: str = Field(..., description="ej. ehns-banking-prod")
    event_hub_name: str = Field(default="transactions")
    event_hub_consumer_group: str = Field(default="$Default")
    checkpoint_storage_account: str
    checkpoint_container: str = "checkpoints"

    # Cosmos DB
    cosmos_account: str  # ej. cosmos-banking-prod
    cosmos_database: str = "banking"
    cosmos_container_transactions: str = "transactions"
    cosmos_container_profiles: str = "user_profiles"
    cosmos_container_feedback: str = "feedback_loop"

    # Azure OpenAI
    openai_endpoint: AnyUrl
    openai_deployment_l2: str = "gpt-4o-mini"
    openai_api_version: str = "2024-10-01-preview"
    openai_timeout_seconds: float = 2.0

    # Classifier
    fasttext_model_path: str = "/app/ml_assets/model.bin"
    fasttext_model_uri: str | None = None  # blob URI desde donde se descarga al boot
    confidence_threshold: float = 0.85

    # Anomaly
    zscore_threshold: float = 3.0
    profile_cache_ttl_seconds: int = 300

    # Telemetry
    applicationinsights_connection_string: str
    log_level: str = "INFO"

    # Runtime
    api_port: int = 8000
    enable_sync_api: bool = False  # solo true en dev
```

### 5.4 Módulo `domain/models.py`

Pydantic v2. Todos los modelos son **inmutables** (`model_config = {"frozen": True}`).

```python
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID
from pydantic import BaseModel, Field

class Category(StrEnum):
    GROCERIES = "groceries"
    TRANSPORT = "transport"
    LEISURE = "leisure"
    HOUSING = "housing"
    HEALTH = "health"
    UTILITIES = "utilities"
    INCOME = "income"
    TRANSFERS = "transfers"
    OTHER = "other"
    # añadir las que decida producto; bloquearlas con esta enum es la única manera de impedir que el LLM invente categorías nuevas

class Transaction(BaseModel):
    model_config = {"frozen": True}
    transaction_id: UUID
    user_id: str
    amount: Decimal               # negativo = gasto, positivo = ingreso
    currency: str = "EUR"
    merchant_raw: str             # texto crudo del comercio, lo que clasifica fastText
    merchant_mcc: str | None = None  # Merchant Category Code si existe
    timestamp: datetime           # cuándo ocurrió en el banco, no cuando llegó

class UserProfile(BaseModel):
    model_config = {"frozen": True}
    user_id: str
    mean_spend: Decimal
    stddev_spend: Decimal
    transactions_count: int
    top_merchants: list[str] = Field(default_factory=list, max_length=20)
    updated_at: datetime

class AnomalyResult(BaseModel):
    model_config = {"frozen": True}
    is_anomaly: bool
    z_score: float
    reason: str | None = None     # human-readable, para mostrar en app

class ClassificationL1(BaseModel):
    model_config = {"frozen": True}
    category: Category
    confidence: float = Field(..., ge=0.0, le=1.0)
    model_version: str            # ej. "fasttext-2026-03-15"

class ClassificationL2(BaseModel):
    model_config = {"frozen": True}
    category: Category
    rationale: str                # explicación del LLM, para auditar
    model_version: str            # ej. "gpt-4o-mini-2024-07-18"
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int

class TransactionProcessed(BaseModel):
    model_config = {"frozen": True}
    transaction: Transaction
    category: Category
    final_classifier: str         # "l1" o "l2"
    confidence: float
    anomaly: AnomalyResult
    processed_at: datetime
    pipeline_latency_ms: int
```

### 5.5 Módulo `repositories/cosmos.py`

Patrón: un único `CosmosClient` se inicializa al arranque con `DefaultAzureCredential`. Los repositorios de cada colección son clases delgadas que reciben el cliente.

Reglas obligatorias:
- Conexión por **endpoint AAD**, no por master key. `CosmosClient(url=f"https://{account}.documents.azure.com:443/", credential=DefaultAzureCredential())`.
- Partition key de `transactions`: `/user_id` (todas las transacciones de un usuario en la misma partición).
- Partition key de `user_profiles`: `/user_id`.
- Partition key de `feedback_loop`: `/year_month` (ej. `"2026-05"`) — porque se lee en bloque mensualmente.
- **Reintentos:** la SDK ya hace retries con backoff exponencial; no añadas otra capa por encima. Sí captura `CosmosHttpResponseError` y log.
- **No uses `read_all_items`** nunca. Siempre query con partition key.

### 5.6 Módulo `services/anomaly.py`

Cálculo Z-Score. Determinístico, sin dependencias externas. Es el módulo más fácil de testear: hazlo TDD.

```python
from decimal import Decimal
from hot_path.domain.models import Transaction, UserProfile, AnomalyResult

class AnomalyDetector:
    def __init__(self, threshold: float = 3.0):
        self._threshold = threshold

    def detect(self, tx: Transaction, profile: UserProfile) -> AnomalyResult:
        # Solo aplicamos a gastos (importes negativos)
        if tx.amount >= 0:
            return AnomalyResult(is_anomaly=False, z_score=0.0)

        spend = abs(float(tx.amount))
        mean = float(profile.mean_spend)
        std = float(profile.stddev_spend)

        if std == 0:
            return AnomalyResult(is_anomaly=False, z_score=0.0)

        z = (spend - mean) / std

        if z > self._threshold:
            reason = f"Importe {spend:.2f}€ supera tu media habitual ({mean:.2f}€) en {z:.1f}σ"
            return AnomalyResult(is_anomaly=True, z_score=z, reason=reason)

        return AnomalyResult(is_anomaly=False, z_score=z)
```

Decisiones explícitas:
- Si la desviación típica es 0 (usuario sin histórico suficiente) → no es anomalía. **Nunca** dividir por cero.
- Solo se evalúa gasto, no ingreso. Una nómina alta no es anomalía.
- El threshold por defecto es 3σ. Ajustable por config.

### 5.7 Módulo `services/classifier_l1.py`

Esta es la decisión técnica con más matices del proyecto. Léela entera antes de codear.

**fastText vs ONNX.** El ADD original menciona "fastText en formato ONNX". La conversión real fastText → ONNX no es trivial (no hay exportador oficial mantenido por Facebook; existen forks comunitarios inconsistentes). Decisión: usa la librería oficial `fasttext` para inferencia. El binario `.bin` se carga en memoria y la inferencia es < 1 ms igualmente. **Anota en el README que ONNX queda como optimización futura**. No es premature optimization aceptable.

```python
import fasttext
from hot_path.domain.models import Transaction, Category, ClassificationL1

class FastTextClassifier:
    def __init__(self, model_path: str, model_version: str):
        # fasttext.load_model es síncrono y bloqueante; se hace 1 vez al boot
        self._model = fasttext.load_model(model_path)
        self._version = model_version

    def classify(self, tx: Transaction) -> ClassificationL1:
        # Normalización: minúsculas, sin tildes, sin números, sin signos
        text = self._normalize(tx.merchant_raw)
        labels, probs = self._model.predict(text, k=1)
        # fasttext devuelve labels como "__label__groceries"
        raw_label = labels[0].replace("__label__", "")
        return ClassificationL1(
            category=Category(raw_label),
            confidence=float(probs[0]),
            model_version=self._version,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        import unicodedata, re
        text = text.lower().strip()
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        text = re.sub(r"\d+", "", text)
        text = re.sub(r"[^a-z\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
```

Reglas:
- El modelo se carga **una sola vez** al arranque del proceso. No por cada petición.
- Si `Category(raw_label)` falla (label desconocida), captura `ValueError` y devuelve `Category.OTHER` con `confidence=0.0` para forzar el L2. Esto pasa si el modelo se entrena con categorías nuevas pero el código no se actualiza.
- El `model_version` se inyecta desde config (no se hardcodea).

**¿Cómo se descarga el modelo al boot?** Si `fasttext_model_path` ya existe en disco, úsalo. Si no, descarga desde `fasttext_model_uri` (un Blob) con Managed Identity. Esto permite rotar el modelo sin rebuild de imagen.

### 5.8 Módulo `services/classifier_l2.py`

Cliente Azure OpenAI con `openai` SDK (no el viejo `openai.AzureOpenAI` deprecado). Aut con Managed Identity (sin API key).

```python
from openai import AsyncAzureOpenAI
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
import time
from hot_path.domain.models import Transaction, ClassificationL2, Category

CATEGORIES_LIST = ", ".join(c.value for c in Category)

SYSTEM_PROMPT = f"""Eres un clasificador determinístico de transacciones bancarias.
Recibirás el texto crudo del comercio y debes devolver UNA categoría de esta lista, exactamente con esa cadena:
{CATEGORIES_LIST}

Devuelve un JSON: {{"category": "<categoria>", "rationale": "<breve explicación>"}}
No inventes categorías. Si dudas, usa "other"."""

class OpenAIClassifier:
    def __init__(self, endpoint: str, deployment: str, api_version: str, timeout: float, model_version: str):
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        self._client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token_provider=token_provider,
            api_version=api_version,
            timeout=timeout,
        )
        self._deployment = deployment
        self._version = model_version

    async def classify(self, tx: Transaction) -> ClassificationL2:
        t0 = time.perf_counter()
        response = await self._client.chat.completions.create(
            model=self._deployment,
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=120,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Comercio: {tx.merchant_raw}\nImporte: {tx.amount} {tx.currency}"},
            ],
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        import json
        payload = json.loads(response.choices[0].message.content)
        return ClassificationL2(
            category=Category(payload["category"]),
            rationale=payload["rationale"],
            model_version=self._version,
            latency_ms=latency_ms,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )
```

Reglas:
- `temperature=0.0` y `response_format=json_object` para determinismo.
- **Timeout duro a 2 s.** Si OpenAI tarda más, fail fast.
- Si el LLM devuelve una categoría no válida (no está en la enum), captura `ValueError` y degrada a `Category.OTHER`. **Logueas warning**, no error: es comportamiento esperado raro.
- Si la llamada falla del todo (timeout, 429, 500), captura la excepción y devuelve `Category.OTHER` con rationale `"fallback failed"`. **No bloquees el hot path por un fallo de OpenAI.**

### 5.9 Módulo `services/pipeline.py`

El orquestador. Es la única clase que conoce a las otras tres y compone el flujo. **No hace I/O directamente** — recibe los servicios por inyección.

```python
import time
from datetime import datetime, UTC
from hot_path.domain.models import (
    Transaction, TransactionProcessed, ClassificationL1
)
from hot_path.services.anomaly import AnomalyDetector
from hot_path.services.classifier_l1 import FastTextClassifier
from hot_path.services.classifier_l2 import OpenAIClassifier
from hot_path.repositories.profiles import ProfileRepository
from hot_path.repositories.feedback import FeedbackRepository
from hot_path.core.logging import logger

class TransactionPipeline:
    def __init__(
        self,
        anomaly: AnomalyDetector,
        l1: FastTextClassifier,
        l2: OpenAIClassifier,
        profiles: ProfileRepository,
        feedback: FeedbackRepository,
        confidence_threshold: float,
    ):
        self._anomaly = anomaly
        self._l1 = l1
        self._l2 = l2
        self._profiles = profiles
        self._feedback = feedback
        self._threshold = confidence_threshold

    async def process(self, tx: Transaction) -> TransactionProcessed:
        t0 = time.perf_counter()

        profile = await self._profiles.get(tx.user_id)
        anomaly_result = self._anomaly.detect(tx, profile)

        l1_result = self._l1.classify(tx)

        final_category = l1_result.category
        final_classifier = "l1"
        final_confidence = l1_result.confidence

        if l1_result.confidence < self._threshold:
            l2_result = await self._l2.classify(tx)
            final_category = l2_result.category
            final_classifier = "l2"
            final_confidence = 1.0  # por convención L2 no expone confianza
            # fire-and-forget el feedback; no bloquea respuesta
            await self._feedback.record(tx, l1_result, l2_result)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        return TransactionProcessed(
            transaction=tx,
            category=final_category,
            final_classifier=final_classifier,
            confidence=final_confidence,
            anomaly=anomaly_result,
            processed_at=datetime.now(UTC),
            pipeline_latency_ms=latency_ms,
        )
```

### 5.10 Módulo `consumers/event_hub.py`

Patrón: `EventHubConsumerClient` con `BlobCheckpointStore`. Lee en streaming, llama al pipeline, escribe en Cosmos, hace checkpoint.

Decisiones críticas:
- **Concurrencia por partición:** cada partición de Event Hubs se procesa en su propia tarea asyncio. Dentro de una partición los mensajes se procesan **en orden** (no reordenar).
- **Checkpoint cada N=50 mensajes o cada T=5 s, lo que ocurra antes.** Demasiado frecuente quema RU/s de blob storage; demasiado infrecuente pierde trabajo en reinicio.
- **Si el pipeline falla** en un mensaje, hay dos modos:
  - `DECISION: fail-loud` → no hacer checkpoint, dejar que se reprocese en el siguiente lease. Loggear como `error`.
  - `DECISION: dead-letter` → escribir el mensaje crudo + traceback a una colección `dlq` en Cosmos y avanzar el checkpoint.
  - **Implementa `fail-loud` por defecto, con un switch de config para `dead-letter`.** En clase, fail-loud es más visible y educativo.

Esqueleto:

```python
import asyncio
from azure.eventhub.aio import EventHubConsumerClient
from azure.eventhub.extensions.checkpointstoreblobaio import BlobCheckpointStore
from azure.identity.aio import DefaultAzureCredential

class TransactionConsumer:
    def __init__(self, config, pipeline, transactions_repo, ...):
        ...

    async def run(self) -> None:
        credential = DefaultAzureCredential()
        checkpoint_store = BlobCheckpointStore(
            blob_account_url=f"https://{self._cfg.checkpoint_storage_account}.blob.core.windows.net",
            container_name=self._cfg.checkpoint_container,
            credential=credential,
        )
        client = EventHubConsumerClient(
            fully_qualified_namespace=f"{self._cfg.event_hub_namespace}.servicebus.windows.net",
            eventhub_name=self._cfg.event_hub_name,
            consumer_group=self._cfg.event_hub_consumer_group,
            checkpoint_store=checkpoint_store,
            credential=credential,
        )
        async with client:
            await client.receive_batch(
                on_event_batch=self._on_batch,
                max_batch_size=50,
                max_wait_time=5,
                starting_position="-1",  # Lee desde el más reciente al arrancar fresh
            )

    async def _on_batch(self, partition_context, events) -> None:
        for event in events:
            tx = Transaction.model_validate_json(event.body_as_str())
            processed = await self._pipeline.process(tx)
            await self._transactions_repo.save(processed)
            # update profile stats fire-and-forget
            asyncio.create_task(self._profiles.update_stats(tx.user_id, tx.amount))
        await partition_context.update_checkpoint()
```

### 5.11 Módulo `api/app.py` y `api/routes/`

FastAPI mínimo. Tres endpoints:

- `GET /health` → 200 siempre que el proceso esté arriba.
- `GET /ready` → 200 cuando: modelo fastText cargado + conexión Cosmos OK + último checkpoint < 60 s atrás. 503 si no.
- `POST /v1/classify` → recibe un `Transaction`, devuelve un `TransactionProcessed`. **Solo se habilita si `enable_sync_api=True`** (típicamente en dev para probar sin Event Hubs).

KEDA escalará basándose en la métrica de Event Hub (cola), **no** en CPU ni en peticiones HTTP. Pero el endpoint de health es necesario para los liveness probes de Container Apps.

### 5.12 Módulo `core/telemetry.py`

Usa `azure-monitor-opentelemetry` (paquete distro). Una sola llamada al inicio:

```python
from azure.monitor.opentelemetry import configure_azure_monitor

def setup_telemetry(connection_string: str, service_name: str) -> None:
    configure_azure_monitor(
        connection_string=connection_string,
        resource_attributes={"service.name": service_name},
    )
```

Métricas obligatorias a emitir (usa OTel Meter):
- `tx_processed_total` (counter, labels: `final_classifier`, `is_anomaly`)
- `tx_pipeline_latency_ms` (histogram)
- `l1_confidence` (histogram)
- `l2_invocations_total` (counter)
- `l2_failures_total` (counter)
- `cosmos_write_latency_ms` (histogram)

Logs: `structlog` configurado con JSON output. Cada log debe incluir `transaction_id`, `user_id` (hashed si quieres ser estricto con GDPR), `partition_id`.

### 5.13 Arranque (`main.py`)

```python
import asyncio
import uvicorn
from hot_path.core.config import Settings
from hot_path.core.telemetry import setup_telemetry
from hot_path.core.logging import setup_logging
# ... imports

async def main():
    settings = Settings()
    setup_logging(settings.log_level)
    setup_telemetry(settings.applicationinsights_connection_string, "hot-path")

    # 1. Cargar modelo fastText (bloqueante; sin él no arrancamos)
    l1 = FastTextClassifier(settings.fasttext_model_path, model_version="...")

    # 2. Inicializar clientes Azure
    cosmos_client = CosmosClient(...)
    profiles_repo = ProfileRepository(cosmos_client, ...)
    transactions_repo = TransactionRepository(cosmos_client, ...)
    feedback_repo = FeedbackRepository(cosmos_client, ...)

    l2 = OpenAIClassifier(...)
    anomaly = AnomalyDetector(threshold=settings.zscore_threshold)
    pipeline = TransactionPipeline(anomaly, l1, l2, profiles_repo, feedback_repo, settings.confidence_threshold)

    consumer = TransactionConsumer(settings, pipeline, transactions_repo, profiles_repo)
    api = build_api(settings, pipeline)

    # Lanzar consumer + servidor HTTP en paralelo
    api_server = uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=settings.api_port, log_config=None))
    await asyncio.gather(consumer.run(), api_server.serve())

if __name__ == "__main__":
    asyncio.run(main())
```

### 5.14 Manejo de errores y reintentos

| Fallo | Manejo |
|---|---|
| Cosmos transient (429, 503) | SDK reintenta solo; capturar tras N intentos → log error y dead-letter |
| OpenAI timeout/429/500 | Devolver `Category.OTHER` con rationale `"fallback failed"`, métrica `l2_failures_total++` |
| Mensaje de Event Hub deserializa mal | Log warning, dead-letter, checkpoint sí |
| fastText predice label inexistente | `Category.OTHER` con `confidence=0` → fuerza L2 |
| Modelo fastText no encontrado al boot | **Fail fast**: el proceso muere y Container Apps lo reinicia |
| Cosmos no conecta al boot | **Fail fast** igual |
| Perfil del usuario no existe | Crear perfil vacío con `mean=0, std=0`. La anomalía devolverá `is_anomaly=False` por la lógica de `std==0` |

### 5.15 Dockerfile

Multi-stage. Imagen final mínima.

```dockerfile
FROM python:3.11-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install -e .

FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 && rm -rf /var/lib/apt/lists/*  # fastText necesita libgomp
WORKDIR /app
COPY --from=builder /install /usr/local
COPY src/ /app/src/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
USER 1000
CMD ["python", "-m", "hot_path.main"]
```

---

## 6. Servicio `cold-path` (Durable Functions)

### 6.1 Propósito

Una vez al mes, generar para cada usuario un resumen financiero en lenguaje natural usando la Batch API de Azure OpenAI (descuento del 50% vs API síncrona).

### 6.2 Patrón

Patrón **fan-out / fan-in** de Durable Functions:

```
Orchestrator (monthly_insights)
  ├─ activity: list_active_users()                  → ["u1", "u2", ..., "u340000"]
  ├─ fan-out: for each user → aggregate_user(user_id) (paralelo)
  │     → cada uno produce un AggregatedSpend con totales por categoría
  ├─ activity: build_batch_file(aggregated_list)    → genera un JSONL
  ├─ activity: submit_openai_batch(jsonl_uri)       → devuelve batch_id
  ├─ activity: wait_for_batch(batch_id)             → poll cada 5 min, timeout 24h
  ├─ activity: download_results(batch_id)           → JSONL con respuestas
  └─ activity: persist_insights(results)            → escribe en Cosmos colección insights
```

### 6.3 Trigger

`TimerTrigger`, CRON `0 0 2 1 * *` (las 02:00 UTC del primer día de cada mes).

### 6.4 Anonimización

Antes de enviar a OpenAI Batch, cada `AggregatedSpend` se anonimiza:
- `user_id` → hash SHA-256 truncado a 16 chars (suficiente para evitar colisiones a 340k).
- Sin nombre, sin email, sin localización exacta (solo país, que en este caso siempre es España).
- Solo importes agregados por categoría y mes.

### 6.5 Prompt para Batch API

```
SYSTEM: Eres un asistente financiero que escribe resúmenes mensuales personalizados.
Tono cercano pero profesional. Máximo 5 frases. Sin moralizar el gasto.

USER: Resumen mensual del usuario {hash}, mes {YYYY-MM}, EUR:
- Ingresos: {income}
- Alimentación: {groceries}
- Transporte: {transport}
- ...
Escribe un párrafo breve destacando la categoría con más gasto y comparándolo con el mes anterior si está disponible.
```

---

## 7. Pipeline MLOps (`mlops-pipeline`)

### 7.1 Propósito

Mensualmente, reentrenar fastText con los casos que cayeron al L2 (donde fastText falló) y publicar el nuevo modelo si supera al actual en el set de validación.

### 7.2 Componentes del pipeline AML

```
extract_feedback  →  prepare_dataset  →  train_fasttext  →  evaluate  →  publish_model
```

1. **`extract_feedback`** — query a Cosmos `feedback_loop` del último mes. Output: parquet con `(merchant_raw, label_correcto)`.
2. **`prepare_dataset`** — combina con el dataset histórico, split 80/10/10 train/val/test, normalización igual a la del servicio.
3. **`train_fasttext`** — `fasttext.train_supervised(...)` con hyperparámetros: `lr=0.5, epoch=25, wordNgrams=2, bucket=200000, dim=100`.
4. **`evaluate`** — calcula precision@1 sobre el test set. Si es < precision@1 del modelo actual, **falla el pipeline** (no se publica).
5. **`publish_model`** — sube el `.bin` a un Blob, registra el modelo en Azure ML Model Registry con tag `version=<fecha>`, dispara un workflow de GitHub Actions vía webhook que rebuilds la imagen del hot path con la nueva ruta del modelo y la despliega a Container Apps con estrategia blue/green.

### 7.3 Trigger

Schedule mensual en AML. CRON `0 3 5 * *` (las 03:00 UTC del día 5 de cada mes; deja un margen sobre el cold-path).

---

## 8. Modelo de datos Cosmos DB

### 8.1 Container `transactions`

- **Partition key:** `/user_id`
- **TTL:** 13 meses (cumplimiento + queries de "año anterior").

Documento ejemplo:
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
  "pipeline_latency_ms": 1245,
  "_etag": "...",
  "_ts": 1748254981
}
```

### 8.2 Container `user_profiles`

- **Partition key:** `/user_id`
- **TTL:** desactivado (perfil persistente).

Documento ejemplo:
```json
{
  "id": "u_12345",
  "user_id": "u_12345",
  "mean_spend": "65.30",
  "stddev_spend": "23.10",
  "transactions_count": 1247,
  "top_merchants": ["MERCADONA", "RENFE", "AMAZON", "..."],
  "updated_at": "2026-05-26T10:23:01Z"
}
```

Actualización: cada N=10 nuevas transacciones, recalcular `mean`, `stddev`, `top_merchants`. **No actualizar tras cada transacción** (carga inútil). El recálculo es fire-and-forget asyncio.

### 8.3 Container `feedback_loop`

- **Partition key:** `/year_month` (ej. `"2026-05"`) — porque se barre mensualmente.
- **TTL:** 6 meses (suficiente para reentreno + auditoría).

Documento ejemplo:
```json
{
  "id": "...uuid...",
  "year_month": "2026-05",
  "transaction_id": "...",
  "merchant_raw": "FARMACIA SAN MIGUEL S.L.",
  "l1_prediction": {"category": "other", "confidence": 0.42},
  "l2_prediction": {"category": "health", "rationale": "Farmacia identificada por la palabra 'FARMACIA'"},
  "captured_at": "2026-05-26T10:23:01Z"
}
```

### 8.4 Container `insights` (lo escribe el cold-path)

- **Partition key:** `/user_id`
- **TTL:** 12 meses.

```json
{
  "id": "u_12345_2026-05",
  "user_id": "u_12345",
  "year_month": "2026-05",
  "summary_text": "Este mes has gastado 1.234€...",
  "breakdown": {"groceries": "423.10", ...},
  "generated_at": "2026-06-01T02:34:00Z"
}
```

---

## 9. Infraestructura como código (Bicep)

### 9.1 Recursos a crear

| Recurso | Nombre lógico | Notas |
|---|---|---|
| Resource Group | `rg-banking-{env}` | Uno por entorno |
| Virtual Network | `vnet-banking-{env}` | `/22`, subnets: aca, cosmos, openai, functions |
| Container Apps Environment | `cae-banking-{env}` | Workload profile `Consumption` + `D4` para hot-path |
| Container App | `aca-hotpath-{env}` | KEDA scaler sobre Event Hubs |
| Event Hubs Namespace | `ehns-banking-{env}` | Standard tier, capture desactivado |
| Event Hub | `transactions` | 4 particiones en dev, 16 en prod |
| Cosmos DB account | `cosmos-banking-{env}` | API NoSQL, multi-region en prod |
| Cosmos databases + containers | ver §8 | RU/s autoscale 1000–10000 prod |
| Azure OpenAI account | `openai-banking-{env}` | Sweden Central. Despliegues: `gpt-4o-mini` (síncrono) + `gpt-4o-mini-batch` |
| Storage Account | `stbanking{env}` | Para checkpoints + Batch API input/output |
| Function App | `func-coldpath-{env}` | Linux, Python 3.11, Premium plan EP1 |
| Azure ML Workspace | `aml-banking-{env}` | Con compute cluster CPU `cpu-cluster-low` |
| Container Registry | `crbanking{env}` | Premium SKU (private endpoint) |
| Key Vault | `kv-banking-{env}` | Premium, RBAC, network ACLs cerradas |
| Application Insights | `appi-banking-{env}` | Workspace-based, ligado a Log Analytics |
| Log Analytics Workspace | `log-banking-{env}` | Retención 90 días |
| Managed Identities (user-assigned) | una por servicio | rolas IAM minimal |
| Private DNS Zones | una por servicio Azure | resolución privada de los endpoints |

### 9.2 Estructura recomendada de Bicep

`main.bicep` solo orquesta módulos. Cada módulo es self-contained.

```bicep
// main.bicep
param env string
param location string = 'swedencentral'

module identity 'modules/identity.bicep' = { ... }
module network  'modules/network.bicep'  = { ... }
module observability 'modules/observability.bicep' = { ... }
module cosmos   'modules/cosmos.bicep'   = { dependsOn: [network] }
module eventHubs 'modules/event_hubs.bicep' = { dependsOn: [network] }
module openai   'modules/openai.bicep'   = { dependsOn: [network] }
module aca      'modules/container_apps.bicep' = { dependsOn: [cosmos, eventHubs, openai] }
module fns      'modules/functions.bicep' = { dependsOn: [cosmos, openai] }
module aml      'modules/aml.bicep'      = { dependsOn: [cosmos] }
```

### 9.3 RBAC mínimo (ejemplos)

| Identity | Rol | Scope |
|---|---|---|
| MI del hot-path | `Azure Event Hubs Data Receiver` | Event Hub `transactions` |
| MI del hot-path | `Cosmos DB Built-in Data Contributor` | DB `banking` |
| MI del hot-path | `Cognitive Services OpenAI User` | OpenAI account |
| MI del hot-path | `Storage Blob Data Contributor` | container `checkpoints` |
| MI del cold-path | `Cosmos DB Built-in Data Contributor` | DB `banking` |
| MI del cold-path | `Cognitive Services OpenAI Contributor` | OpenAI account (Batch necesita Contributor) |
| MI del cold-path | `Storage Blob Data Contributor` | container `batch-io` |
| MI del AML compute | `Cosmos DB Built-in Data Reader` | container `feedback_loop` |
| MI del AML compute | `AcrPush` | ACR |

---

## 10. Variables de entorno y secretos

### 10.1 Filosofía

- **Endpoints** y configuración no sensible → variables de entorno directas (settings de Container App).
- **Nada de connection strings o API keys**: todo via Managed Identity.
- Si hubiera un secreto residual (un webhook externo, etc.) → Key Vault + referencias en Container App con `secretref`.

### 10.2 Lista completa (`hot-path`)

| Variable | Origen | Ejemplo |
|---|---|---|
| `HOTPATH_EVENT_HUB_NAMESPACE` | env | `ehns-banking-prod` |
| `HOTPATH_EVENT_HUB_NAME` | env | `transactions` |
| `HOTPATH_CHECKPOINT_STORAGE_ACCOUNT` | env | `stbankingprod` |
| `HOTPATH_COSMOS_ACCOUNT` | env | `cosmos-banking-prod` |
| `HOTPATH_OPENAI_ENDPOINT` | env | `https://openai-banking-prod.openai.azure.com/` |
| `HOTPATH_OPENAI_DEPLOYMENT_L2` | env | `gpt-4o-mini` |
| `HOTPATH_FASTTEXT_MODEL_URI` | env | `https://stbankingprod.blob.core.windows.net/models/fasttext-2026-05-05.bin` |
| `HOTPATH_APPLICATIONINSIGHTS_CONNECTION_STRING` | env | `InstrumentationKey=...` |
| `HOTPATH_CONFIDENCE_THRESHOLD` | env | `0.85` |
| `HOTPATH_ZSCORE_THRESHOLD` | env | `3.0` |
| `AZURE_CLIENT_ID` | env (managed identity) | UUID de la user-assigned MI |

---

## 11. Testing

### 11.1 Pirámide

| Tipo | % objetivo | Cobertura mínima | Tooling |
|---|---|---|---|
| Unitarios | 70 % | 80 % líneas | pytest |
| Integración | 25 % | flujos críticos | pytest + Azure SDK contra emuladores (Cosmos local) |
| Carga | 5 % | escenarios pico | locust |

### 11.2 Tests unitarios obligatorios

- `test_anomaly.py`: 8+ casos. Gasto normal, gasto extremo, ingreso (no debe ser anomalía), `std=0`, threshold ajustable, importes positivos, importes 0.
- `test_classifier_l1.py`: con un modelo fake (mockear `fasttext.load_model`), 5+ casos. Label válida, label inválida → `OTHER`, normalización de texto, confianza baja.
- `test_classifier_l2.py`: con `respx` o mock del `AsyncAzureOpenAI`, 6+ casos. Happy path, timeout, 429, JSON inválido, categoría inventada.
- `test_pipeline.py`: 10+ casos. Confianza alta → no llama L2; confianza baja → llama L2; L2 falla → degrada a OTHER; anomalía detectada se propaga; mide latency.
- `test_models.py`: validación pydantic, frozen, edge cases.

### 11.3 Integración

- Usa el **emulador de Cosmos DB en linux** (`mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator`) en docker-compose. No mockes Cosmos a nivel de unit en estos tests.
- Para Event Hubs no hay emulador oficial bueno; **usa una namespace de Event Hubs dedicada `ehns-banking-tests`** con su propia partición y consumer group `tests`.

### 11.4 Carga

Escenario locust:
- 500 usuarios virtuales, ramp-up 60s, hold 5 min.
- Cada VU produce 1 tx/s al endpoint síncrono (`/v1/classify`, solo en entorno test).
- Asserts: p95 < 3000 ms, error rate < 0.1%.

---

## 12. CI/CD

Tres workflows de CI (uno por servicio), tres de CD, uno de infra.

### 12.1 `ci-hot-path.yml`

Triggers: PR a `main` que toca `apps/hot-path/**` o `shared/**`.

Steps:
1. Checkout
2. Setup Python 3.11
3. Instalar dependencias
4. `ruff check` + `ruff format --check`
5. `mypy`
6. `pytest --cov=src/hot_path --cov-fail-under=80`
7. Build Docker image (no push)
8. Trivy scan de la imagen

### 12.2 `cd-hot-path.yml`

Triggers: push a `main` que toca `apps/hot-path/**`.

Steps:
1. Checkout
2. Login a ACR vía OIDC (no passwords; `azure/login@v2` con federated credential)
3. Build y push imagen con tags `:latest` y `:<git-sha>`
4. `az containerapp update` con nueva imagen, **estrategia revision = multiple**, traffic split 100% nueva tras smoke test
5. Smoke test: poll `/ready` durante 60s
6. Si OK, promover; si KO, rollback automático

### 12.3 `infra-deploy.yml`

Manual trigger. Permite seleccionar `env` (dev/staging/prod). Hace `az deployment sub create` con el bicep.

---

## 13. Observabilidad

### 13.1 Dashboards en Azure Workbook (provisiona por Bicep)

Cuatro paneles:
1. **Health del hot-path**: p50/p95/p99 de `tx_pipeline_latency_ms`, tx procesadas por minuto, error rate.
2. **Calidad del L1**: distribución de `l1_confidence`, % de tx que necesitan L2 desglosado por categoría predicha por L1.
3. **Coste de OpenAI**: tokens consumidos (síncrono + batch), invocaciones L2/hora, fallos.
4. **Cosmos**: RU/s consumidas vs aprovisionadas, latencia p95 de escritura, 429s.

### 13.2 Alertas (Action Groups → email + Teams webhook)

| Alerta | Condición | Severidad |
|---|---|---|
| Latencia p95 alta | `tx_pipeline_latency_ms` p95 > 2500ms durante 5 min | 2 |
| Tasa L2 anómala | `l2_invocations_total / tx_processed_total` > 0.2 durante 15 min | 3 |
| Errores L2 | `l2_failures_total` > 50/min | 2 |
| Cosmos 429s | Métrica de Cosmos `TotalRequestUnits` ratio > 0.9 | 2 |
| Hot-path caído | `/ready` 503 durante 2 min | 1 |
| Lag de Event Hub | KEDA reporta queue length > 10000 durante 5 min | 2 |

---

## 14. Roadmap de desarrollo (sprints de 1 semana)

> **Construye en este orden. No saltes pasos.** Cada sprint termina con un demo funcional.

### Sprint 0 — Setup (1 semana)
- Crear el repositorio con la estructura de §4.
- `pre-commit`, `ruff`, `mypy` configurados.
- Bicep que despliega: RG, VNet, Log Analytics, Cosmos (1 container con datos dummy), Application Insights.
- CI básico (lint + test stub).
- Demo: `terraform plan`-equivalente en dev funciona y los recursos existen.

### Sprint 1 — Pipeline en memoria (1 semana)
- Implementar `domain/models.py`.
- Implementar `services/anomaly.py` con tests.
- Implementar `services/classifier_l1.py` con un **modelo fastText entrenado a mano sobre 1000 ejemplos sintéticos** (no se trata de tener un buen modelo aún; se trata de tener el plumbing).
- Implementar `services/pipeline.py` sin L2 todavía. El threshold no se aplica aún.
- Tests unit + cobertura.
- Demo: un script que toma 100 transacciones JSON de un fichero, las pasa por el pipeline y escribe el resultado en stdout.

### Sprint 2 — Cosmos integrado (1 semana)
- Implementar repositorios `transactions`, `profiles`.
- Bicep crea los containers reales.
- Integration tests contra emulador.
- Pipeline ahora lee perfil y escribe transacción procesada.
- Demo: el script del sprint anterior persiste en Cosmos local; queries manuales validan los datos.

### Sprint 3 — Event Hubs (1 semana)
- Bicep crea Event Hub + Storage Account + checkpoint container.
- Implementar `consumers/event_hub.py`.
- Implementar `api/app.py` con `/health` y `/ready`.
- Empaquetar Dockerfile, build local, smoke test contra Event Hub real (dev).
- Demo: un productor manual mete 100 tx en Event Hub, el contenedor las procesa y persiste.

### Sprint 4 — L2 + KEDA + despliegue real (1 semana)
- Implementar `services/classifier_l2.py` contra Azure OpenAI real.
- Aplicar el threshold de confianza en el pipeline.
- Bicep crea Container App con KEDA scaler.
- CD a Container Apps.
- Demo: el sistema entero funciona en Azure, recibe transacciones, escala a 0–5 réplicas según carga.

### Sprint 5 — Observabilidad y hardening (1 semana)
- `core/telemetry.py` con métricas custom.
- Workbook + alertas en Bicep.
- Tests de carga con locust (objetivo p95 < 3s).
- Tunear KEDA y RU/s de Cosmos hasta cumplir SLA.
- Demo: dashboard en vivo con carga simulada.

### Sprint 6 — Cold-path (1 semana)
- Implementar Durable Function completo.
- Bicep crea Function App.
- Demo: trigger manual del orchestrator sobre 1000 usuarios de prueba, ver insights en Cosmos.

### Sprint 7 — MLOps (1 semana)
- Implementar AML pipeline.
- Workflow de GitHub Actions que se dispara tras publish_model y redeploya el hot-path con la nueva imagen.
- Demo: end-to-end retraining con datos sintéticos de feedback, modelo nuevo desplegado, verificación de que la versión cambió en `model_version` de los logs.

### Sprint 8 — Pulido (1 semana)
- Documentación final: runbook, ADRs, README pulido.
- Threat model ligero (STRIDE).
- Penalty review de coste con Azure Cost Management.
- Demo: presentación + Q&A.

---

## 15. Criterios de aceptación (Definition of Done global)

El proyecto está terminado cuando **todas** estas cosas son verdad:

- [ ] El bicep despliega los tres entornos (dev/staging/prod) sin intervención manual, idempotente.
- [ ] Una transacción nueva en Event Hub aparece como documento clasificado en Cosmos `transactions` en menos de 3 s (p95 medido en 10.000 muestras).
- [ ] Tasa de fallback L2 medida sobre 100.000 transacciones reales < 15 % (puede ser > 10 % al inicio; el reentreno la baja).
- [ ] El cold-path corre el día 1, produce insights para todos los usuarios activos, sin fallos.
- [ ] El mlops-pipeline corre el día 5, publica un nuevo modelo si mejora, no lo publica si empeora.
- [ ] Cobertura de tests unitarios ≥ 80 % en `hot-path`, ≥ 70 % en `cold-path`.
- [ ] Ningún secret en el repo (gitleaks como pre-commit lo verifica).
- [ ] Ningún recurso de Azure tiene endpoint público accesible desde internet salvo los explícitamente públicos (no aplica a ninguno en este proyecto).
- [ ] Documentación en `docs/` permite a un ingeniero nuevo levantar el sistema en local en < 1 hora.

---

## 16. Decisiones explícitamente diferidas (no implementar ahora)

Estas son cosas que se han considerado y se han **decidido fuera de alcance**. No las implementes, pero deja un `TODO:` o issue documentado.

1. **Conversión real fastText → ONNX.** Servimos con la lib oficial. Optimización futura si fastText puro deja de cumplir el SLA (no se espera).
2. **Multi-región activo-activo.** Cosmos en multi-write y dos regiones de Container Apps. Solo si el banco contrata un SLA superior a 99,9 %.
3. **Streaming insights al usuario en tiempo real.** Los insights ahora son mensuales batch. Versión real-time requiere otro pipeline.
4. **Reranking con embeddings.** Antes de saltar a L2, se podría consultar un vector store con embeddings de comercios conocidos. Lo dejamos para v2.
5. **Detección de fraude.** Z-Score detecta anomalías de gasto, no fraude. El fraude lo cubre otro sistema del banco.
6. **A/B testing del modelo.** Habría que serving dos versiones de fastText con traffic split. Container Apps lo soporta nativamente, pero no es prioritario.

---

## 17. Glosario rápido

- **Hot path:** flujo síncrono que debe cumplir el SLA por transacción.
- **Cold path:** flujo asíncrono, batch, sin SLA estricto.
- **L1 / L2:** primer y segundo nivel de clasificación. L1 es rápido (fastText), L2 es lento pero más preciso (LLM).
- **KEDA:** Kubernetes Event-Driven Autoscaling. Lo que escala los contenedores según la cola de Event Hub.
- **Managed Identity:** identidad de Azure asociada a un recurso para que se autentique a otros sin secretos.
- **Private Endpoint:** entrada de red privada a un servicio PaaS, sin exposición pública.
- **Feedback loop:** transacciones donde L1 cayó por debajo del threshold y L2 tuvo que decidir; alimentan el reentreno.
- **Bounded context:** en DDD, una frontera dentro de la cual los términos tienen un significado consistente.

---

## 18. Una sola regla final

Si te encuentras con una decisión técnica que este documento no responde:

1. Elige la opción **más simple y reversible**.
2. Implementa la cosa más pequeña que cierre el ticket.
3. Deja un comentario `# DECISION: <fecha> elegí X sobre Y porque Z. Reversible en <archivo>.`
4. Sigue.

No esperes a tener todas las respuestas para empezar. Empieza por el Sprint 0.

— Fin del documento —
