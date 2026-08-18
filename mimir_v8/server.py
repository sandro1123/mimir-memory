"""Loopback-only Mímir v9 service bootstrap."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .runtime import RuntimeConfigurationError, build_runtime, normalize_knowledge_layers


def build_app(
    data_dir: str | Path,
    token_file: str | Path,
    *,
    vector_enabled: bool = True,
    collection_name: str = "mimir_v8_prod_facts",
    model_name: str = "BAAI/bge-m3",
    enabled_knowledge_layers: tuple[str, ...] = ("memory", "learning", "wiki"),
):
    app, _ = build_runtime(
        data_dir,
        token_file,
        vector_enabled=vector_enabled,
        collection_name=collection_name,
        model_name=model_name,
        enabled_knowledge_layers=enabled_knowledge_layers,
    )
    return app


def parse_knowledge_layers(raw: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeConfigurationError("knowledge layer configuration cannot be empty")
    return normalize_knowledge_layers(tuple(part.strip() for part in raw.split(",")))


def main() -> None:
    parser = argparse.ArgumentParser(description="Mímir v9 loopback-only REST server")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18456)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("MIMIR_V8_DATA_DIR", "./var/mimir-v8"),
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("MIMIR_V8_TOKEN_FILE", "./var/mimir-v8/api_tokens.json"),
    )
    parser.add_argument(
        "--collection",
        default=os.environ.get("MIMIR_V8_COLLECTION", "mimir_v8_prod_facts"),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MIMIR_V8_MODEL", "BAAI/bge-m3"),
    )
    parser.add_argument(
        "--knowledge-layers",
        default=os.environ.get("MIMIR_V9_KNOWLEDGE_LAYERS", "memory,learning,wiki"),
        help="comma-separated enabled layers: memory,learning,wiki",
    )
    parser.add_argument("--disable-vector", action="store_true")
    args = parser.parse_args()
    if args.bind not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("Mímir v9 server may only bind to loopback")
    try:
        enabled_knowledge_layers = parse_knowledge_layers(args.knowledge_layers)
    except RuntimeConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
    import uvicorn

    uvicorn.run(
        build_app(
            args.data_dir,
            args.token_file,
            vector_enabled=not args.disable_vector,
            collection_name=args.collection,
            model_name=args.model,
            enabled_knowledge_layers=enabled_knowledge_layers,
        ),
        host=args.bind,
        port=args.port,
        access_log=True,
        server_header=False,
    )


if __name__ == "__main__":
    main()
