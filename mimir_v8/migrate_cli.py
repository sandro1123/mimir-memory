"""Explicit Mímir canonical schema migration and isolated restore CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .migration import migrate_schema, migrate_schema_v13, restore_schema_backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mímir explicit schema lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate", help="migrate a canonical database with mandatory backup")
    migrate.add_argument("--database", required=True)
    migrate.add_argument("--backup", required=True)
    migrate.add_argument("--to-version", type=int, default=None,
                         help="target schema version (auto-detect: 12->13, legacy 9/10->11)")
    restore = sub.add_parser("restore-isolated", help="restore a backup to a new isolated path")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--destination", required=True)
    # audit 2026-09-07 P1-1: offline repair of projection drift (status-only
    # changes skipped by the old FTS guard). Run with the API stopped.
    reproject = sub.add_parser("reproject", help="re-apply facts' canonical state to fts/graph(/vector) projections")
    reproject.add_argument("--data-dir", required=True, help="directory holding canonical.db, fts.db, graph.db, chroma/")
    reproject.add_argument("--fact-ids", required=True, help="comma-separated fact ids")
    reproject.add_argument("--with-vector", action="store_true", help="also repair the chroma vector projection")
    reproject.add_argument("--collection", default=os.environ.get("MIMIR_V8_COLLECTION", ""),
                           help="chroma collection name (required with --with-vector)")
    return parser


def _no_embed(_text: str):
    raise RuntimeError("reproject refuses to embed; only status/deletion repairs are supported offline")


def _reproject(args) -> tuple[dict, int]:
    from .graph_projector import GraphProjector
    from .operations import reproject_facts
    from .projector import FTSProjector
    from .store import CanonicalStore

    root = Path(args.data_dir)
    store = CanonicalStore(root / "canonical.db")
    projectors: list = [FTSProjector(root / "fts.db"), GraphProjector(store, root / "graph.db")]
    if args.with_vector:
        if not args.collection:
            raise SystemExit("--collection (or MIMIR_V8_COLLECTION) is required with --with-vector")
        import chromadb
        from chromadb.config import Settings
        from .vector_projector import VectorProjector
        client = chromadb.PersistentClient(path=str(root / "chroma"), settings=Settings(anonymized_telemetry=False))
        collection = client.get_collection(args.collection)
        projectors.append(VectorProjector(collection, _no_embed, collection_name=args.collection))
    fact_ids = [f.strip() for f in args.fact_ids.split(",") if f.strip()]
    report = reproject_facts(store, projectors, fact_ids)
    report["projectors"] = [getattr(p, "name", type(p).__name__) for p in projectors]
    return report, (1 if report["missing"] else 0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        import sqlite3
        with sqlite3.connect(args.database) as probe:
            row = probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        source_version = int(row[0]) if row else 0
        # P0-J（v14.2，issue #2）：12 不再分流单腿 v13——旧路由只应用
        # v13 DDL 就盖章 SCHEMA_VERSION，v14~v18 结构缺失（审计实证：
        # "migrate stamps schema_version=18 while only applying the v13
        # DDL"）。12 与其他版本一律走全链 migrate_schema()：源 12 的
        # additive 链从 V13 起步顺序补齐至当前 schema。
        # to_version=13 的显式降级请求保留单腿（有留档用途）。
        if args.to_version == 13:
            result = migrate_schema_v13(args.database, args.backup).as_dict()
        else:
            result = migrate_schema(args.database, args.backup).as_dict()
    elif args.command == "reproject":
        result, code = _reproject(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return code
    else:
        result = restore_schema_backup(args.backup, args.destination)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
