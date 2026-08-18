"""Idempotent projector runtime and the first FTS projection for Mímir v8."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Callable

from .store import CanonicalStore, utc_now


class ProjectionError(RuntimeError):
    """Raised when a projector cannot safely advance its checkpoint."""


FTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projected_facts (
    fact_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL,
    owner_principal TEXT NOT NULL,
    domain TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    project_id TEXT,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL
) STRICT;

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED,
    content,
    summary,
    domain,
    fact_type,
    tokenize='trigram'
);
"""


class FTSProjector:
    name = "fts"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as connection:
            connection.executescript(FTS_SCHEMA)
            connection.commit()
        self._ensure_trigram_index()

    def _ensure_trigram_index(self) -> None:
        """Rebuild the FTS index with the trigram tokenizer when a legacy
        unicode61 index is detected. unicode61 cannot tokenize CJK text, so
        substring queries in Chinese miss entirely; trigram fixes that for
        both CJK and ASCII. projected_facts keeps its content, so the index
        is rebuilt from it without touching the canonical outbox."""
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='facts_fts'"
            ).fetchone()
            if row is None or "trigram" in (row[0] or ""):
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DROP TABLE facts_fts")
                connection.execute(
                    """CREATE VIRTUAL TABLE facts_fts USING fts5(
                        fact_id UNINDEXED, content, summary, domain, fact_type,
                        tokenize='trigram')"""
                )
                connection.execute(
                    """INSERT INTO facts_fts(fact_id, content, summary, domain, fact_type)
                    SELECT fact_id, content, summary, domain, fact_type
                    FROM projected_facts WHERE status='active'"""
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def apply(self, event: sqlite3.Row, fact: dict) -> None:
        with contextlib.closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT version, content_hash FROM projected_facts WHERE fact_id=?",
                    (fact["fact_id"],),
                ).fetchone()
                if existing and existing["version"] > fact["current_version"]:
                    connection.rollback()
                    return
                if (
                    existing
                    and existing["version"] == fact["current_version"]
                    and existing["content_hash"] == fact["content_hash"]
                ):
                    connection.rollback()
                    return
                connection.execute("DELETE FROM facts_fts WHERE fact_id=?", (fact["fact_id"],))
                if fact["status"] == "active":
                    connection.execute(
                        """INSERT INTO facts_fts(fact_id, content, summary, domain, fact_type)
                        VALUES(?,?,?,?,?)""",
                        (
                            fact["fact_id"],
                            fact["content"],
                            fact["summary"],
                            fact["domain"],
                            fact["fact_type"],
                        ),
                    )
                connection.execute(
                    """INSERT INTO projected_facts(
                        fact_id, version, content, summary, owner_principal, domain,
                        fact_type, project_id, status, content_hash, source_event_seq
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(fact_id) DO UPDATE SET
                        version=excluded.version,
                        content=excluded.content,
                        summary=excluded.summary,
                        owner_principal=excluded.owner_principal,
                        domain=excluded.domain,
                        fact_type=excluded.fact_type,
                        project_id=excluded.project_id,
                        status=excluded.status,
                        content_hash=excluded.content_hash,
                        source_event_seq=excluded.source_event_seq""",
                    (
                        fact["fact_id"],
                        fact["current_version"],
                        fact["content"],
                        fact["summary"],
                        fact["owner_principal"],
                        fact["domain"],
                        fact["fact_type"],
                        fact["project_id"],
                        fact["status"],
                        fact["content_hash"],
                        event["event_seq"],
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def count(self) -> int:
        with contextlib.closing(self.connect()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM projected_facts WHERE status='active'").fetchone()[0])

    def search_ids(self, query: str, limit: int = 10) -> list[str]:
        # Treat API input as natural language, not raw FTS5 query syntax.
        # Quoted tokens avoid operators such as /, :, -, or NEAR being
        # interpreted as executable MATCH grammar. The index uses the
        # trigram tokenizer (CJK + ASCII substring search); tokens shorter
        # than 3 chars cannot match trigrams, so fall back to LIKE.
        tokens = re.findall(r"[\w\u3400-\u9fff]+", query, flags=re.UNICODE)
        if not tokens:
            return []
        match_tokens: list[str] = []
        for token in tokens[:64]:
            if len(token) < 3:
                continue
            if len(token) >= 5 and re.search(r"[\u3400-\u9fff]", token):
                # Long CJK strings rarely appear verbatim in content (word
                # boundaries differ), so expand into overlapping trigram
                # fragments; rank ordering still favours denser matches.
                match_tokens.extend(
                    token[i:i + 3] for i in range(0, len(token) - 2)
                )
            else:
                match_tokens.append(token)
        results: list[str] = []
        with contextlib.closing(self.connect()) as connection:
            if match_tokens:
                safe_query = " OR ".join(
                    f'"{token.replace(chr(34), chr(34) * 2)}"' for token in match_tokens
                )
                rows = connection.execute(
                    "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (safe_query, limit),
                ).fetchall()
                results = [row["fact_id"] for row in rows]
            if len(results) < limit:
                seen = set(results)
                short_tokens = [t for t in tokens if len(t) < 3][:16]
                for token in short_tokens:
                    if len(results) >= limit:
                        break
                    like = f"%{token}%"
                    rows = connection.execute(
                        """SELECT fact_id FROM projected_facts
                        WHERE status='active' AND (content LIKE ? OR summary LIKE ?)
                        LIMIT ?""",
                        (like, like, limit),
                    ).fetchall()
                    for row in rows:
                        if row["fact_id"] not in seen:
                            seen.add(row["fact_id"])
                            results.append(row["fact_id"])
        return results[:limit]


class ProjectorRunner:
    def __init__(self, store: CanonicalStore, projector: FTSProjector):
        self.store = store
        self.projector = projector

    def run_once(
        self,
        limit: int = 100,
        failure_hook: Callable[[str, int], None] | None = None,
    ) -> dict:
        processed = 0
        failed = 0
        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute(
                """SELECT o.outbox_id, o.event_seq, o.attempts, e.*
                FROM outbox o
                JOIN memory_events e ON e.event_seq=o.event_seq
                WHERE o.projector_name=? AND o.status IN ('pending','retry')
                ORDER BY o.event_seq
                LIMIT ?""",
                (self.projector.name, limit),
            ).fetchall()

        for row in rows:
            try:
                # 投影器不处理对话事件（conversation.*），直接跳过推进 checkpoint
                event_type = row["event_type"]
                if event_type.startswith("conversation."):
                    with self.store.transaction() as connection:
                        current_checkpoint = int(
                            connection.execute(
                                "SELECT checkpoint_event_seq FROM projector_state WHERE projector_name=?",
                                (self.projector.name,),
                            ).fetchone()[0]
                        )
                        if row["event_seq"] < current_checkpoint:
                            raise ProjectionError("projector checkpoint cannot move backwards")
                        connection.execute(
                            """UPDATE outbox SET status='done', attempts=attempts+1,
                            completed_at=?, locked_at=NULL, last_error_code=NULL
                            WHERE outbox_id=?""",
                            (utc_now(), row["outbox_id"]),
                        )
                        connection.execute(
                            """UPDATE projector_state SET checkpoint_event_seq=?,
                            updated_at=?, status='idle', last_error_code=NULL
                            WHERE projector_name=?""",
                            (row["event_seq"], utc_now(), self.projector.name),
                        )
                    processed += 1
                    continue

                resolver = getattr(self.projector, "resolve_event", None)
                fact = resolver(row) if resolver else self.store.get_fact(row["aggregate_id"])
                self.projector.apply(row, fact)
                if failure_hook:
                    failure_hook("after_apply", int(row["event_seq"]))
                with self.store.transaction() as connection:
                    current_checkpoint = int(
                        connection.execute(
                            "SELECT checkpoint_event_seq FROM projector_state WHERE projector_name=?",
                            (self.projector.name,),
                        ).fetchone()[0]
                    )
                    if row["event_seq"] < current_checkpoint:
                        raise ProjectionError("projector checkpoint cannot move backwards")
                    connection.execute(
                        """UPDATE outbox SET status='done', attempts=attempts+1,
                        completed_at=?, locked_at=NULL, last_error_code=NULL
                        WHERE outbox_id=?""",
                        (utc_now(), row["outbox_id"]),
                    )
                    connection.execute(
                        """UPDATE projector_state SET checkpoint_event_seq=?,
                        updated_at=?, status='idle', last_error_code=NULL
                        WHERE projector_name=?""",
                        (row["event_seq"], utc_now(), self.projector.name),
                    )
                processed += 1
            except Exception as exc:
                failed += 1
                with self.store.transaction() as connection:
                    attempts = int(row["attempts"]) + 1
                    status = "dead_letter" if attempts >= 5 else "retry"
                    connection.execute(
                        """UPDATE outbox SET status=?, attempts=?, available_at=?,
                        locked_at=NULL, last_error_code=? WHERE outbox_id=?""",
                        (status, attempts, utc_now(), type(exc).__name__, row["outbox_id"]),
                    )
                    connection.execute(
                        """UPDATE projector_state SET status=?, updated_at=?, last_error_code=?
                        WHERE projector_name=?""",
                        (status, utc_now(), type(exc).__name__, self.projector.name),
                    )
                # Projectors are ordered streams. Never advance a later event
                # while an earlier event is retrying or dead-lettered.
                break
        return {"processed": processed, "failed": failed}

    def status(self) -> dict:
        with contextlib.closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM projector_state WHERE projector_name=?",
                (self.projector.name,),
            ).fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE projector_name=? AND status IN ('pending','retry')",
                (self.projector.name,),
            ).fetchone()[0]
            dead_letter = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE projector_name=? AND status='dead_letter'",
                (self.projector.name,),
            ).fetchone()[0]
            return {**dict(row), "pending": pending, "dead_letter": dead_letter}
