"""Explicit Mímir canonical schema migration and isolated restore CLI."""

from __future__ import annotations

import argparse
import json

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        import sqlite3
        with sqlite3.connect(args.database) as probe:
            row = probe.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        source_version = int(row[0]) if row else 0
        if source_version == 12 or (args.to_version == 13):
            result = migrate_schema_v13(args.database, args.backup).as_dict()
        else:
            result = migrate_schema(args.database, args.backup).as_dict()
    else:
        result = restore_schema_backup(args.backup, args.destination)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
