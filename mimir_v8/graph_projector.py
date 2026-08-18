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
    status TEXT NOT NULL
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
                relation_type, status FROM relations WHERE source_fact_id=?""",
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
                                    relation_type, status
                                ) VALUES(?,?,?,?,?,?)""",
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
