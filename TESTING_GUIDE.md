# 🧪 Guía de Pruebas y Plan de Ejecución del Sistema

Esta guía detalla el plan de ejecución paso a paso para arrancar, probar y validar de manera local y en Azure los componentes del **Sistema de Categorización Bancaria**.

---

## 📋 Resumen del Plan de Ejecución

Para probar el sistema completo de forma segura y progresiva, seguiremos 5 fases ordenadas:

```
┌────────────────────────┐
│  Fase 1: Preparación   │ ──► Generar el modelo fastText binario de desarrollo local.
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│   Fase 2: Unit Tests   │ ──► Ejecutar pytest para validar algoritmos y fallback resipiente.
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│ Fase 3: Integración L1 │ ──► Levantar API síncrona local y clasificar transacciones.
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│ Fase 4: Cold-Path Dev  │ ──► Probar Durable Functions con Azurite localmente.
└───────────┬────────────┘
            ▼
┌────────────────────────┐
│  Fase 5: Load Testing  │ ──► Ejecutar pruebas de estrés de alta concurrencia con Locust.
└────────────────────────┘
```

---

## 🛠️ Fase 1: Preparación del Entorno y Modelo L1

El clasificador rápido L1 (fastText) requiere un archivo `model.bin` compilado en local para poder arrancar. Hemos preparado un script que genera un modelo de desarrollo con transacciones sintéticas.

### 1. Crear el entorno virtual e instalar dependencias
Abre tu terminal de PowerShell en la raíz del proyecto y ejecuta:

```powershell
# Acceder a la app del hot-path
cd apps/hot-path

# Crear entorno virtual de python
python -m venv .venv

# Activar el entorno virtual
.venv\Scripts\Activate.ps1

# Instalar dependencias necesarias (incluyendo dependencias de desarrollo y test)
pip install -e ".[dev]"
```

> [!NOTE]
> La instalación del paquete `fasttext` puede requerir compiladores de C++ (MSVC) en Windows. Si experimentas dificultades, asegúrate de tener instalado el paquete "Desarrollo de escritorio con C++" en Visual Studio Build Tools.

### 2. Entrenar el Modelo fastText Sintético
Ejecuta el script de entrenamiento para generar la primera versión local del modelo clasificador:

```powershell
python scripts/train_dev_model.py
```

Esto generará el archivo binario en la ruta:
`apps/hot-path/src/hot_path/ml_assets/model.bin`

---

## 🧪 Fase 2: Ejecución de la Suite de Pruebas Unitarias

Con el entorno virtual activo y el modelo generado, podemos validar la lógica matemática e inmutabilidad de los datos ejecutando `pytest`:

```powershell
# Ejecutar todas las pruebas unitarias con cobertura detallada
pytest tests/unit/
```

### Qué estamos validando aquí:
- **`test_anomaly.py`**: El detector de anomalías matemático (Z-Score) detecta compras por encima de $3\sigma$, ignora ingresos y previene la división por cero cuando la desviación estándar es nula.
- **`test_classifier_l1.py`**: El pre-procesamiento del merchant normaliza caracteres Unicode y limpia strings extraños.
- **`test_pipeline.py`**: El enrutador condicional realiza un desvío transparente a L2 (OpenAI) en bajas confianzas y almacena en segundo plano los casos difíciles en `feedback_loop` sin añadir latencia.

---

## ⚡ Fase 3: Pruebas de Integración HTTP (API Local)

Hemos activado el endpoint síncrono `/v1/classify` en el archivo `.env` (`HOTPATH_ENABLE_SYNC_API=true`). Esto permite validar todo el procesamiento de transacciones localmente mediante HTTP simple sin necesidad de conectarse a Event Hubs.

### 1. Iniciar el Servidor FastAPI
Con el entorno virtual activo, arranca la API del hot-path:

```powershell
python src/hot_path/main.py
```

El servidor levantará en `http://localhost:8000`.

### 2. Enviar Transacciones de Prueba
Abre otra terminal y realiza llamadas POST utilizando PowerShell o `curl`:

#### Caso A: Transacción Normal (Clasificación L1 rápida)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/v1/classify" -Method Post -ContentType "application/json" -Body '{
    "transaction_id": "8e3c4b1d-c580-496a-bb94-6d9bcf9d242a",
    "user_id": "usr_9921",
    "amount": -12.50,
    "merchant_raw": "MERCADONA SUPERMERC",
    "timestamp": "2026-05-27T12:00:00Z"
}'
```
* **Resultado esperado:** Categorización instantánea (`GROCERIES` o `FOOD`), clasificador final `"l1"`, confianza elevada y latencia inferior a 5ms.

#### Caso B: Anomalía de Gasto (Z-Score > 3.0)
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/v1/classify" -Method Post -ContentType "application/json" -Body '{
    "transaction_id": "4a12c8b5-3129-4fef-932f-a9cbef42129c",
    "user_id": "usr_9921",
    "amount": -2500.00,
    "merchant_raw": "MERCADONA SUPERMERC",
    "timestamp": "2026-05-27T12:05:00Z"
}'
```
* **Resultado esperado:** `is_anomaly: true`, junto con un desglose descriptivo de la desviación estándar en la respuesta.

---

## ❄️ Fase 4: Pruebas Locales del Cold-Path (Durable Functions)

El cold-path se ejecuta como una Azure Function. Para probarlo localmente:

1. **Instalar Azure Functions Core Tools:**
   Asegúrate de tener instalado `azure-functions-core-tools` a través de npm o chocolatey:
   ```powershell
   npm install -g azure-functions-core-tools@4
   ```

2. **Levantar Azurite (Emulador de Storage local):**
   Las Durable Functions requieren almacenamiento para mantener el estado de la orquestación. Levanta el emulador Azurite en tu PC.

3. **Ejecutar la Función:**
   ```powershell
   cd apps/cold-path
   func start
   ```

4. **Trigger Manual:**
   Puedes disparar el trigger de orquestación mensual llamando al endpoint local que se expone en la consola.

---

## 📈 Fase 5: Pruebas de Carga con Locust (SLA check)

Para comprobar si nuestra FastAPI es capaz de sostener el pico transitorio de **2000 transacciones por segundo** exigido en las especificaciones técnicos, ejecutaremos un test de estrés con Locust.

1. Arranca la API en una terminal (`python src/hot_path/main.py`).
2. En otra terminal con el entorno `.venv` activo, arranca Locust:
   ```powershell
   locust -f tests/load/locustfile.py
   ```
3. Abre tu navegador en `http://localhost:8089`, introduce el número de usuarios concurrentes (ej. `100`), la tasa de generación (ej. `10`) y la dirección destino `http://localhost:8000`.
4. ¡Observa los gráficos de latencia p95 e identifica cuellos de botella!
