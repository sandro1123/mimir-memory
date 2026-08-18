# syntax=docker/dockerfile:1
# Mímir v12 Insight — lean runtime image (no embeddings, no chromadb).
# For full image with vector search, use the canonical Dockerfile.
# Registry: docker pull ghcr.io/<org>/mimir:v12.0.0

FROM python:3.11-slim AS runtime

WORKDIR /app

COPY pyproject.toml README.md requirements-lean.txt ./
COPY mimir_v8 ./mimir_v8
COPY hermes-plugin ./hermes-plugin

RUN python3 -m venv /opt/mimir && \
    /opt/mimir/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/mimir/bin/pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-lean.txt && \
    /opt/mimir/bin/pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple . && \
    rm -rf /root/.cache/pip

ENV MIMIR_V8_DATA_DIR=/data/canonical.db \
    MIMIR_V8_TOKEN_FILE=/data/api_tokens.json \
    MIMIR_V8_COLLECTION=mimir_v12_prod \
    MIMIR_V9_KNOWLEDGE_LAYERS=memory,learning,wiki \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    PATH=/opt/mimir/bin:$PATH

VOLUME ["/data"]
EXPOSE 8456

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8456/health')"

ENTRYPOINT ["mimir-server"]
CMD ["--disable-vector"]