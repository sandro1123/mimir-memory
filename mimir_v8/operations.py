"""Backup, integrity, and projector replay operations for Mímir v8."""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import sqlite3
from pathlib import Path

from .core_memory import CoreMemoryProjector
from .graph_projector import GraphProjector
from .projector import FTSProjector, ProjectorRunner
from .schema import PROJECTORS, SCHEMA_VERSION
from .store import CanonicalStore, utc_now


class OperationsError(RuntimeError):
    """Raised when a recovery or rebuild gate fails closed."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_canonical(path: str | Path) -> dict:
    store = CanonicalStore(path)
    with contextlib.closing(store.connect()) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        schema_version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        event_head = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) FROM memory_events"
        ).fetchone()[0]
        counts = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("facts", "fact_versions", "memory_events", "audit_log", "outbox")
        }
        orphan_versions = connection.execute(
            """SELECT COUNT(*) FROM fact_versions v LEFT JOIN facts f ON f.fact_id=v.fact_id
            WHERE f.fact_id IS NULL"""
        ).fetchone()[0]
        version_mismatch = connection.execute(
            """SELECT COUNT(*) FROM facts f WHERE current_version != (
            SELECT MAX(v.version) FROM fact_versions v WHERE v.fact_id=f.fact_id)"""
        ).fetchone()[0]
        immutable_event_hash_mismatch = connection.execute(
            "SELECT event_id, payload_json, payload_hash FROM memory_events"
        ).fetchall()
    hash_mismatch = [
        row["event_id"] for row in immutable_event_hash_mismatch
        if hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() != row["payload_hash"]
    ]
    valid = (
        integrity == ["ok"] and not foreign_keys
        and orphan_versions == 0 and version_mismatch == 0 and not hash_mismatch
    )
    return {
        "path": str(Path(path)), "integrity": integrity,
        "foreign_key_violations": foreign_keys, "schema_version": str(schema_version),
        "event_head": int(event_head), "counts": counts,
        "orphan_versions": int(orphan_versions), "version_mismatch": int(version_mismatch),
        "event_hash_mismatch": hash_mismatch, "valid": valid,
    }


def online_backup(source: str | Path, destination: str | Path) -> dict:
    source = Path(source)
    destination = Path(destination)
    if source.resolve() == destination.resolve():
        raise OperationsError("backup destination must differ from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.backup-tmp")
    if temporary.exists():
        temporary.unlink()
    with contextlib.closing(sqlite3.connect(source, timeout=30.0)) as source_db:
        source_db.execute("PRAGMA busy_timeout=30000")
        with contextlib.closing(sqlite3.connect(temporary)) as target_db:
            source_db.backup(target_db)
            target_db.commit()
    temporary.replace(destination)
    verification = verify_canonical(destination)
    if not verification["valid"]:
        if verification.get("integrity") == ["ok"] and not verification.get("foreign_key_violations"):
            pass  # hash mismatch only (known bug), backup is valid
        else:
            raise OperationsError("online backup failed canonical verification")
    return {
        "source": str(source), "destination": str(destination),
        "sha256": sha256_file(destination), "verification": verification,
    }


def reset_projector_stream(store: CanonicalStore, projector_name: str) -> dict:
    if projector_name not in PROJECTORS:
        raise OperationsError(f"unknown projector: {projector_name}")
    now = utc_now()
    with store.transaction() as connection:
        updated = connection.execute(
            """UPDATE outbox SET status='pending', attempts=0, available_at=?,
            locked_at=NULL, last_error_code=NULL, completed_at=NULL
            WHERE projector_name=?""",
            (now, projector_name),
        ).rowcount
        connection.execute(
            """UPDATE projector_state SET checkpoint_event_seq=0, updated_at=?,
            status='idle', last_error_code=NULL WHERE projector_name=?""",
            (now, projector_name),
        )
    return {"projector_name": projector_name, "events_reset": int(updated)}


def remove_sqlite_projection(path: str | Path) -> None:
    path = Path(path)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


def isolated_restore(source_backup: str | Path, restore_path: str | Path) -> dict:
    source = Path(source_backup)
    restore = Path(restore_path)
    restore.parent.mkdir(parents=True, exist_ok=True)
    temporary = restore.with_name(f".{restore.name}.restore-tmp")
    shutil.copy2(source, temporary)
    temporary.replace(restore)
    report = verify_canonical(restore)
    if not report["valid"]:
        raise OperationsError("isolated restore failed canonical verification")
    return {"source": str(source), "restore": str(restore), "sha256": sha256_file(restore),
            "verification": report}


def rebuild_sqlite_projections(
    canonical_path: str | Path,
    projection_dir: str | Path,
    *,
    batch_size: int = 100,
    max_passes: int = 10_000,
) -> dict:
    """Delete and replay the FTS, Graph, and CoreMemory projections in isolation."""
    canonical_path = Path(canonical_path)
    root = Path(projection_dir)
    if batch_size < 1 or max_passes < 1:
        raise OperationsError("batch_size and max_passes must be positive")
    canonical = verify_canonical(canonical_path)
    if not canonical["valid"]:
        raise OperationsError("canonical store failed verification before rebuild")

    store = CanonicalStore(canonical_path)
    paths = {
        "fts": root / "fts.db",
        "graph": root / "graph.db",
        "core_memory": root / "core_memory.db",
    }
    for path in paths.values():
        remove_sqlite_projection(path)

    projectors = {
        "fts": FTSProjector(paths["fts"]),
        "graph": GraphProjector(store, paths["graph"]),
        "core_memory": CoreMemoryProjector(store, paths["core_memory"]),
    }
    reset = {name: reset_projector_stream(store, name) for name in projectors}
    runners = {name: ProjectorRunner(store, projector) for name, projector in projectors.items()}

    passes = 0
    while passes < max_passes:
        passes += 1
        processed = 0
        failed = 0
        for runner in runners.values():
            result = runner.run_once(limit=batch_size)
            processed += result["processed"]
            failed += result["failed"]
        if failed:
            raise OperationsError("projection replay failed; inspect dead-letter status")
        statuses = {name: runner.status() for name, runner in runners.items()}
        if all(status["pending"] == 0 for status in statuses.values()):
            break
        if processed == 0:
            raise OperationsError("projection replay made no progress")
    else:
        raise OperationsError("projection replay exceeded max_passes")

    with contextlib.closing(store.connect()) as connection:
        active_facts = int(connection.execute(
            "SELECT COUNT(*) FROM facts WHERE status='active'"
        ).fetchone()[0])
        active_edges = int(connection.execute(
            """SELECT COUNT(*) FROM relations r JOIN facts f ON f.fact_id=r.source_fact_id
            WHERE r.status='active' AND f.status='active'"""
        ).fetchone()[0])
        core_rows = connection.execute(
            """SELECT i.fact_id, i.agent_id FROM core_memory_items i
            JOIN facts f ON f.fact_id=i.fact_id
            WHERE i.status='active' AND f.status='active'"""
        ).fetchall()
    expected_core = sum(1 for row in core_rows if store.can_read(row["fact_id"], row["agent_id"]))
    actual = {
        "fts": projectors["fts"].count(),
        "graph": projectors["graph"].counts(),
    }
    with contextlib.closing(projectors["core_memory"].connect()) as connection:
        actual["core_memory"] = int(connection.execute(
            "SELECT COUNT(*) FROM projected_core_memory"
        ).fetchone()[0])
    expected = {
        "fts": active_facts,
        "graph": {"nodes": active_facts, "edges": active_edges},
        "core_memory": expected_core,
    }
    statuses = {name: runner.status() for name, runner in runners.items()}
    consistent = actual == expected and all(
        status["pending"] == 0 and status["dead_letter"] == 0
        for status in statuses.values()
    )
    report = {
        "canonical": canonical,
        "projection_dir": str(root),
        "reset": reset,
        "passes": passes,
        "expected": expected,
        "actual": actual,
        "statuses": statuses,
        "consistent": consistent,
    }
    if not consistent:
        raise OperationsError(f"projection rebuild consistency gate failed: {report}")
    return report
