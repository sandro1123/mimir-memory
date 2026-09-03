"""Rebuildable relational graph projection for Mímir v8."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from .store import CanonicalStore

GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_nodes (
    fact_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    owner_principal TEXT NOT NULL,
    domain TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    project_id TEXT,
    content_hash TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS graph_edges (
    relation_id TEXT PRIMARY KEY,
    source_fact_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    status TEXT NOT NULL,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_fact_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_type, target_id, relation_type);
"""


class GraphProjector:
    name = "graph"

    def __init__(self, store: CanonicalStore, path: str | Path):
        self.store = store
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as connection:
            connection.executescript(GRAPH_SCHEMA)
            # v13 additive columns for pre-v13 graph.db files: CREATE TABLE
            # IF NOT EXISTS is a no-op on a legacy table, so a v8-era
            # graph_edges (6 columns) would keep its old shape and every
            # history() query would fail with "no such column: valid_from".
            # Guarded ALTER mirrors the canonical relations migration
            # (v20): legacy rows keep '' = open interval = always valid.
            existing = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(graph_edges)")
            }
            for column in ("valid_from", "valid_until"):
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE graph_edges ADD COLUMN {column} "
                        "TEXT NOT NULL DEFAULT ''"
                    )
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def apply(self, event: sqlite3.Row, fact: dict) -> None:
        with contextlib.closing(self.store.connect()) as canonical:
            relations = canonical.execute(
                """SELECT relation_id, source_fact_id, target_type, target_id,
                relation_type, status, valid_from, valid_until
                FROM relations WHERE source_fact_id=?""",
                (fact["fact_id"],),
            ).fetchall()
        with contextlib.closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT version, content_hash FROM fact_nodes WHERE fact_id=?",
                    (fact["fact_id"],),
                ).fetchone()
                if existing and existing["version"] > fact["current_version"]:
                    connection.rollback()
                    return
                connection.execute("DELETE FROM graph_edges WHERE source_fact_id=?", (fact["fact_id"],))
                if fact["status"] == "active":
                    connection.execute(
                        """INSERT INTO fact_nodes(
                            fact_id, version, status, owner_principal, domain,
                            fact_type, project_id, content_hash, source_event_seq
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(fact_id) DO UPDATE SET
                            version=excluded.version, status=excluded.status,
                            owner_principal=excluded.owner_principal, domain=excluded.domain,
                            fact_type=excluded.fact_type, project_id=excluded.project_id,
                            content_hash=excluded.content_hash,
                            source_event_seq=excluded.source_event_seq""",
                        (
                            fact["fact_id"], fact["current_version"], fact["status"],
                            fact["owner_principal"], fact["domain"], fact["fact_type"],
                            fact["project_id"], fact["content_hash"], event["event_seq"],
                        ),
                    )
                    for relation in relations:
                        if relation["status"] == "active":
                            connection.execute(
                                """INSERT INTO graph_edges(
                                    relation_id, source_fact_id, target_type, target_id,
                                    relation_type, status, valid_from, valid_until
                                ) VALUES(?,?,?,?,?,?,?,?)""",
                                tuple(relation),
                            )
                else:
                    connection.execute("DELETE FROM fact_nodes WHERE fact_id=?", (fact["fact_id"],))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def counts(self) -> dict:
        with contextlib.closing(self.connect()) as connection:
            return {
                "nodes": connection.execute("SELECT COUNT(*) FROM fact_nodes").fetchone()[0],
                "edges": connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0],
            }

    def history(self, entity_id: str, *, at_timestamp: str | None = None) -> list[dict]:
        """v13.0 TKG: edges touching an entity, optionally filtered to the
        ones valid at a point in time. Empty valid_until = open interval.
        Without at_timestamp, the full history is returned."""
        with contextlib.closing(self.connect()) as connection:
            if at_timestamp is None:
                rows = connection.execute(
                    """SELECT relation_id, source_fact_id, target_type, target_id,
                    relation_type, status, valid_from, valid_until FROM graph_edges
                    WHERE source_fact_id=? OR target_id=? ORDER BY valid_from""",
                    (entity_id, entity_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT relation_id, source_fact_id, target_type, target_id,
                    relation_type, status, valid_from, valid_until FROM graph_edges
                    WHERE (source_fact_id=? OR target_id=?)
                    AND valid_from <= ?
                    AND (valid_until = '' OR valid_until > ?)
                    ORDER BY valid_from""",
                    (entity_id, entity_id, at_timestamp, at_timestamp),
                ).fetchall()
        return [dict(row) for row in rows]
