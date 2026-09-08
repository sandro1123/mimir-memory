# syntax=docker/dockerfile:1
# Mímir v12 Insight — API + worker image (self-contained, no dashboard).
# The dashboard builds separately from mimir-dashboard/Dockerfile.
# Registry: docker pull ghcr.io/<org>/mimir:v12.0.0

FROM python:3.11-slim AS runtime

# P0-K (v14.2, dudu RED-6): mirror via build arg, not hardcoded.
# Default = official PyPI (supply-chain auditable); CN networks build with
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

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
    /opt/mimir/bin/pip install --no-cache-dir --index-url ${PIP_INDEX_URL} -r requirements.txt && \
    /opt/mimir/bin/pip install --no-cache-dir --index-url ${PIP_INDEX_URL} . && \
    rm -rf /root/.cache/pip

# Env (override at runtime)
# P0-K (dudu RED-4): nonloopback OFF in image default; compose grants it explicitly.
ENV MIMIR_ALLOW_NONLOOPBACK=0 \
    MIMIR_V8_DATA_DIR=/data/canonical.db \
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

# P0-K (dudu Y8): /ready not /health - port alive != memory usable.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8456/ready')"

# Default: run the API server. Override command for workers/migration:
#   docker run ... mimir-worker decay-scan
ENTRYPOINT ["mimir-server", "--bind", "0.0.0.0", "--port", "8456"]