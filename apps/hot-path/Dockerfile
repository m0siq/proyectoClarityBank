# syntax=docker/dockerfile:1
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential g++ && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-binary fasttext --no-cache-dir --prefix=/install fasttext==0.9.3 && \
    pip install --no-cache-dir --prefix=/install .

# ────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# fastText necesita libgomp (OpenMP runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ /app/src/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Non-root user for security
USER 1000

CMD ["python", "-m", "hot_path.main"]
