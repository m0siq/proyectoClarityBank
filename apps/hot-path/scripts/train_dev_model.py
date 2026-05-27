"""Script para entrenar un modelo fastText de desarrollo con datos sintéticos.

Ejecutar desde apps/hot-path/:
    python scripts/train_dev_model.py

Genera: src/hot_path/ml_assets/model.bin

NO es el modelo de producción — solo sirve para que el plumbing funcione
sin datos reales. El modelo de producción lo genera el mlops-pipeline mensual.
"""

from __future__ import annotations

import os
import random
import re
import tempfile
import unicodedata
from pathlib import Path

# ── Datos sintéticos de entrenamiento ────────────────────────────────────────

EXAMPLES = {
    "groceries": [
        "MERCADONA BARCELONA", "CARREFOUR MADRID", "LIDL SEVILLA",
        "ALCAMPO ZARAGOZA", "DIA SUPERMERCADO", "EROSKI CENTER",
        "HIPERCOR VALENCIA", "FROIZ VIGO", "CONSUM ALICANTE",
        "SUPERMERCADO BM BILBAO", "ALDI GRANADA", "SUPERCOR",
    ],
    "transport": [
        "RENFE CERCANIAS", "EMT MADRID", "METRO BARCELONA",
        "CABIFY RIDE", "UBER TRIP", "REPSOL GASOLINA",
        "CEPSA COMBUSTIBLE", "BP ESTACION SERVICIO", "PARKING PLAZA",
        "AUTOPISTA AP7 PEAJE", "AENA PARKING AEROPUERTO", "BLABLACAR",
    ],
    "leisure": [
        "NETFLIX SUBSCRIPTION", "SPOTIFY PREMIUM", "CINE ODEON",
        "AMAZON PRIME VIDEO", "STEAM GAMES", "NINTENDO ESHOP",
        "TICKETMASTER CONCIERTO", "HBO MAX", "DISNEY PLUS",
        "APPLE TV PLUS", "FNAC LIBROS", "EL CORTE INGLES OCIO",
    ],
    "housing": [
        "COMUNIDAD PROPIETARIOS", "ALQUILER PISO ENERO",
        "HIPOTECA BANCO SANTANDER", "INMOBILIARIA IDEALISTA",
        "SEGUROS HOGAR MAPFRE", "IKEA MOBILIARIO", "LEROY MERLIN",
        "BRICOMART REFORMA", "SERRANILLOS ADMINISTRACION FINCA",
        "GARAJE MENSUAL RENTA", "CASAS PARTICULARES HABITACION", "AIBNB ALOJAMIENTO",
    ],
    "health": [
        "FARMACIA SAN MIGUEL", "CLINICA DENTAL DR GARCIA",
        "HOSPITAL PRIVADO QUIRON", "OPTICA GENERAL",
        "GYM ANYTIME FITNESS", "FISIOTERAPIA CENTRO MEDICO",
        "LABORATORIO ANALISIS CLINICOS", "SEGUROS MEDICOS SANITAS",
        "FARMACIA DE GUARDIA", "PARAFARMACIA ONLINE", "ORTOPEDIA TECNICA",
        "MEDICO GENERAL PRIVADO",
    ],
    "utilities": [
        "ENDESA ENERGIA FACTURA", "IBERDROLA LUZ",
        "NATURGY GAS NATURAL", "VODAFONE MOVIL",
        "MOVISTAR FIBRA OPTICA", "ORANGE TELEFONIA",
        "AGUAS DE BARCELONA", "CANAL ISABEL II AGUA",
        "TELE2 INTERNET", "JAZZTEL FACTURA", "R CABLE GALICIA",
        "DIGI COMUNICACIONES",
    ],
    "income": [
        "NOMINA EMPRESA SA", "TRANSFERENCIA RECIBIDA EMPRESA",
        "DEVOLUCION HACIENDA IRPF", "PAGO FREELANCE CLIENTE",
        "DIVIDENDOS ACCIONES BOLSA", "INGRESO ALQUILER PISO",
        "SUBSIDIO DESEMPLEO SEPE", "PENSION JUBILACION SEGURIDAD SOCIAL",
        "BECA MINISTERIO EDUCACION", "INGRESO BIZUM AMIGO",
    ],
    "transfers": [
        "TRANSFERENCIA EMITIDA JUAN GARCIA", "BIZUM ENVIADO",
        "PAYPAL TRANSFERENCIA ENVIADA", "WESTERN UNION ENVIO",
        "DEPOSITO CUENTA AHORRO", "TRASPASO ENTRE CUENTAS PROPIAS",
        "INGRESO DEPOSITO PLAZO FIJO",
    ],
    "other": [
        "COMISION BANCARIA MANTENIMIENTO", "INTERESES PRESTAMO PERSONAL",
        "SEGURO VIDA GENERALI", "MULTA TRAFICO DGT",
        "IMPUESTO IBI AYUNTAMIENTO", "TASAS UNIVERSITARIAS",
        "AMAZON MARKETPLACE VENDEDOR", "ALIEXPRESS COMPRA",
    ],
}


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"\d+", "", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def generate_training_file(path: str, samples_per_class: int = 150) -> int:
    """Generate a fastText training file with synthetic data."""
    lines = []
    for label, merchants in EXAMPLES.items():
        for _ in range(samples_per_class):
            merchant = random.choice(merchants)
            # Add slight variations
            if random.random() > 0.5:
                merchant = merchant + f" {random.randint(1, 99)}"
            normalized = normalize(merchant)
            if normalized:
                lines.append(f"__label__{label} {normalized}")

    random.shuffle(lines)

    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")

    return len(lines)


def main() -> None:
    import fasttext  # noqa: PLC0415

    output_dir = Path(__file__).parent.parent / "src" / "hot_path" / "ml_assets"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "model.bin"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        train_path = tmp.name

    n_samples = generate_training_file(train_path)
    print(f"Generated {n_samples} training samples across {len(EXAMPLES)} categories")

    model = fasttext.train_supervised(
        input=train_path,
        lr=0.5,
        epoch=25,
        wordNgrams=2,
        bucket=200000,
        dim=100,
        verbose=2,
    )

    # Quick self-eval
    result = model.test(train_path)
    print(f"Train precision@1: {result[1]:.4f} ({result[0]} samples)")

    model.save_model(str(output_path))
    print(f"✅ Model saved to: {output_path}")
    os.unlink(train_path)


if __name__ == "__main__":
    main()
