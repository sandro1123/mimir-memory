# syntax=docker/dockerfile:1
# Mímir v12 Insight — API + worker image (self-contained, no dashboard).
# The dashboard builds separately from mimir-dashboard/Dockerfile.
# Registry: docker pull ghcr.io/<org>/mimir:v12.0.0

FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime deps for the API/worker (embedding model is optional at runtime).
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-venv && \
    rm -rf /var/lib/apt/lists/*

# Install package (wheel) — mirrors the PyPI artifact.
COPY pyproject.toml README.md requirements.txt ./
COPY mimir_v8 ./mimir_v8
COPY hermes-plugin ./hermes-plugin
RUN python3 -m venv /opt/mimir && \
    /opt/mimir/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/mimir/bin/pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt && \
    /opt/mimir/bin/pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple . && \
    rm -rf /root/.cache/pip

# Env (override at runtime)
ENV MIMIR_V8_DATA_DIR=/data/canonical.db \
    MIMIR_V8_TOKEN_FILE=/data/api_tokens.json \
    MIMIR_V8_COLLECTION=mimir_v12_prod \
    MIMIR_V8_MODEL=BAAI/bge-m3 \
    MIMIR_V9_KNOWLEDGE_LAYERS=memory,learning,wiki \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    PATH=/opt/mimir/bin:$PATH

VOLUME ["/data"]
EXPOSE 8456

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8456/health')"

# Default: run the API server. Override command for workers/migration:
#   docker run ... mimir-worker decay-scan
ENTRYPOINT ["mimir-server"]