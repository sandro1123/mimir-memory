"""Canonical promotion workflow and rebuildable CoreMemory projection for Mímir v8."""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .schema import AGENT_IDS
from .store import CanonicalStore, ConflictError, NotFoundError, canonical_json, new_id, sha256_text, utc_now

CORE_MEMORY_BLOCKS = ("user_profile", "project_context", "key_decisions")
CORE_MEMORY_LABELS = {
    "user_profile": "用户画像",
    "project_context": "项目上下文",
    "key_decisions": "关键决策",
}


class CoreMemoryPolicyError(ValueError):
    """Raised when CoreMemory policy rejects an operation."""


@dataclass(frozen=True)
class PromoteCoreMemory:
    agent_id: str
    block_name: str
    fact_id: str
    reason: str
    idempotency_key: str
    position: int = 0


@dataclass(frozen=True)
class RetireCoreMemory:
    item_id: str
    reason: str
    idempotency_key: str


class CoreMemoryService:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def promote(self, command: PromoteCoreMemory, actor_principal: str, *, is_admin: bool = False) -> dict:
        self._authorize(command.agent_id, actor_principal, is_admin)
        if command.agent_id not in AGENT_IDS:
            raise CoreMemoryPolicyError(f"unknown agent: {command.agent_id}")
        if command.block_name not in CORE_MEMORY_BLOCKS:
            raise CoreMemoryPolicyError(f"invalid block: {command.block_name}")
        reason = command.reason.strip()
        key = command.idempotency_key.strip()
        if not reason or not key or command.position < 0:
            raise CoreMemoryPolicyError("reason, idempotency_key and non-negative position are required")
        fingerprint = sha256_text(canonical_json({
            "agent_id": command.agent_id,
            "block_name": command.block_name,
            "fact_id": command.fact_id,
            "reason": reason,
            "position": command.position,
        }))
        now = utc_now()
        with self.store.transaction() as connection:
            replay = self._replay(connection, key, fingerprint)
            if replay:
                item = connection.execute(
                    "SELECT * FROM core_memory_items WHERE item_id=?", (replay["aggregate_id"],)
                ).fetchone()
                return {"item_id": item["item_id"], "fact_id": item["fact_id"],
                        "status": item["status"], "event_seq": replay["event_seq"],
                        "idempotent_replay": True}
            fact = connection.execute("SELECT * FROM facts WHERE fact_id=?", (command.fact_id,)).fetchone()
            if not fact:
                raise NotFoundError(command.fact_id)
            if fact["status"] != "active":
                raise CoreMemoryPolicyError("only active facts can be promoted")
            if fact["owner_principal"] != command.agent_id:
                raise CoreMemoryPolicyError("CoreMemory fact must be owned by the target agent")
            if fact["fact_type"] == "ephemeral":
                raise CoreMemoryPolicyError("ephemeral facts cannot be promoted")
            existing = connection.execute(
                "SELECT item_id, status FROM core_memory_items WHERE agent_id=? AND block_name=? AND fact_id=?",
                (command.agent_id, command.block_name, command.fact_id),
            ).fetchone()
            if existing:
                raise ConflictError(f"fact is already registered in CoreMemory: {existing['status']}")
            item_id = new_id()
            event_id = new_id()
            payload = {"item_id": item_id, "agent_id": command.agent_id,
                       "block_name": command.block_name, "fact_id": command.fact_id,
                       "fact_version": fact["current_version"], "position": command.position,
                       "request_fingerprint": fingerprint}
            event_seq = self._insert_event(connection, event_id, item_id, 1,
                                           "core_memory.promoted", actor_principal, key, now, payload)
            connection.execute(
                """INSERT INTO core_memory_items(
                    item_id, agent_id, block_name, fact_id, fact_version, position,
                    status, promoted_by, promotion_reason, promoted_event_seq,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, command.agent_id, command.block_name, command.fact_id,
                 fact["current_version"], command.position, "active", actor_principal,
                 reason, event_seq, now, now),
            )
            self._queue_projection(connection, event_seq, now)
            self._audit(connection, actor_principal, "core_memory.promoted", item_id,
                        event_id, payload, now)
        return {"item_id": item_id, "fact_id": command.fact_id, "status": "active",
                "event_seq": event_seq, "idempotent_replay": False}

    def retire(self, command: RetireCoreMemory, actor_principal: str, *, is_admin: bool = False) -> dict:
        reason = command.reason.strip()
        key = command.idempotency_key.strip()
        if not reason or not key:
            raise CoreMemoryPolicyError("reason and idempotency_key are required")
        fingerprint = sha256_text(canonical_json({"item_id": command.item_id, "reason": reason}))
        now = utc_now()
        with self.store.transaction() as connection:
            replay = self._replay(connection, key, fingerprint)
            if replay:
                return {"item_id": command.item_id, "status": "retired",
                        "event_seq": replay["event_seq"], "idempotent_replay": True}
            item = connection.execute(
                "SELECT * FROM core_memory_items WHERE item_id=?", (command.item_id,)
            ).fetchone()
            if not item:
                raise NotFoundError(command.item_id)
            self._authorize(item["agent_id"], actor_principal, is_admin)
            if item["status"] != "active":
                raise ConflictError("CoreMemory item is not active")
            event_id = new_id()
            payload = {"item_id": command.item_id, "agent_id": item["agent_id"],
                       "block_name": item["block_name"], "fact_id": item["fact_id"],
                       "request_fingerprint": fingerprint}
            event_seq = self._insert_event(connection, event_id, command.item_id, 2,
                                           "core_memory.retired", actor_principal, key, now, payload)
            connection.execute(
                """UPDATE core_memory_items SET status='retired', retired_by=?,
                retirement_reason=?, retired_event_seq=?, updated_at=? WHERE item_id=?""",
                (actor_principal, reason, event_seq, now, command.item_id),
            )
            self._queue_projection(connection, event_seq, now)
            self._audit(connection, actor_principal, "core_memory.retired", command.item_id,
                        event_id, payload, now)
        return {"item_id": command.item_id, "status": "retired",
                "event_seq": event_seq, "idempotent_replay": False}

    @staticmethod
    def _authorize(agent_id: str, actor: str, is_admin: bool) -> None:
        if not is_admin and actor != agent_id:
            raise CoreMemoryPolicyError("CoreMemory is owner-only unless actor is admin")

    @staticmethod
    def _replay(connection, key: str, fingerprint: str):
        row = connection.execute(
            "SELECT event_seq, aggregate_id, payload_json FROM memory_events WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row and __import__("json").loads(row["payload_json"]).get("request_fingerprint") != fingerprint:
            raise ConflictError("CoreMemory idempotency key was reused with different content")
        return row

    @staticmethod
    def _insert_event(connection, event_id, item_id, version, event_type, actor, key, now, payload):
        payload_json = canonical_json(payload)
        cursor = connection.execute(
            """INSERT INTO memory_events(
                event_id, aggregate_type, aggregate_id, aggregate_version, event_type,
                actor_principal, request_id, correlation_id, occurred_at, payload_json,
                payload_hash, idempotency_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, "core_memory", item_id, version, event_type, actor, event_id,
             event_id, now, payload_json, sha256_text(payload_json), key),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _queue_projection(connection, event_seq, now):
        connection.execute(
            """INSERT INTO outbox(
                outbox_id, event_seq, projector_name, status, available_at
            ) VALUES(?,?,?,?,?)""",
            (new_id(), event_seq, "core_memory", "pending", now),
        )

    @staticmethod
    def _audit(connection, actor, action, item_id, event_id, payload, now):
        connection.execute(
            """INSERT INTO audit_log(
                audit_id, occurred_at, actor_principal, action, resource_type,
                resource_id, request_id, outcome, detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (new_id(), now, actor, action, "core_memory", item_id, event_id,
             "success", canonical_json(payload)),
        )


CORE_MEMORY_PROJECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS projected_core_memory (
    item_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    block_name TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    fact_version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_projected_core_memory_order
ON projected_core_memory(agent_id, block_name, position, item_id);
"""


class CoreMemoryProjector:
    name = "core_memory"

    def __init__(self, store: CanonicalStore, path: str | Path):
        self.store = store
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as connection:
            connection.executescript(CORE_MEMORY_PROJECTION_SCHEMA)
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def resolve_event(self, event: sqlite3.Row) -> dict:
        if event["aggregate_type"] == "fact":
            return self.store.get_fact(event["aggregate_id"])
        if event["aggregate_type"] == "core_memory":
            with contextlib.closing(self.store.connect()) as connection:
                row = connection.execute(
                    "SELECT fact_id FROM core_memory_items WHERE item_id=?",
                    (event["aggregate_id"],),
                ).fetchone()
            if not row:
                raise NotFoundError(event["aggregate_id"])
            return self.store.get_fact(row["fact_id"])
        raise CoreMemoryPolicyError(f"unsupported aggregate type: {event['aggregate_type']}")

    def apply(self, event: sqlite3.Row, fact: dict) -> None:
        with contextlib.closing(self.store.connect()) as canonical:
            items = canonical.execute(
                """SELECT item_id, agent_id, block_name, fact_id, fact_version,
                position, promoted_event_seq FROM core_memory_items
                WHERE fact_id=? AND status='active'""", (fact["fact_id"],)
            ).fetchall()
        visible_items = [
            item for item in items
            if self.store.can_read(item["fact_id"], item["agent_id"])
        ]
        with contextlib.closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM projected_core_memory WHERE fact_id=?", (fact["fact_id"],))
                if fact["status"] == "active":
                    for item in visible_items:
                        connection.execute(
                            """INSERT INTO projected_core_memory(
                                item_id, agent_id, block_name, fact_id, fact_version,
                                position, content, content_hash, source_event_seq
                            ) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (item["item_id"], item["agent_id"], item["block_name"],
                             item["fact_id"], fact["current_version"], item["position"],
                             fact["content"], fact["content_hash"], event["event_seq"]),
                        )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def rebuild(self) -> int:
        with contextlib.closing(self.store.connect()) as canonical:
            rows = canonical.execute(
                """SELECT i.item_id, i.agent_id, i.block_name, i.fact_id, i.position,
                i.promoted_event_seq, f.current_version, f.content, f.content_hash
                FROM core_memory_items i JOIN facts f ON f.fact_id=i.fact_id
                WHERE i.status='active' AND f.status='active'
                ORDER BY i.agent_id, i.block_name, i.position, i.item_id"""
            ).fetchall()
        rows = [row for row in rows if self.store.can_read(row["fact_id"], row["agent_id"])]
        with contextlib.closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM projected_core_memory")
            for row in rows:
                connection.execute(
                    """INSERT INTO projected_core_memory(
                        item_id, agent_id, block_name, fact_id, fact_version,
                        position, content, content_hash, source_event_seq
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (row["item_id"], row["agent_id"], row["block_name"], row["fact_id"],
                     row["current_version"], row["position"], row["content"],
                     row["content_hash"], row["promoted_event_seq"]),
                )
            connection.commit()
        return len(rows)

    def injection_text(self, agent_id: str, max_chars: int = 2000) -> str:
        if max_chars < 64:
            raise CoreMemoryPolicyError("max_chars must be at least 64")
        with contextlib.closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT block_name, content FROM projected_core_memory WHERE agent_id=?
                ORDER BY CASE block_name WHEN 'user_profile' THEN 1
                WHEN 'project_context' THEN 2 ELSE 3 END, position, item_id""",
                (agent_id,),
            ).fetchall()
        lines = [f"=== CoreMemory: {agent_id} ==="]
        grouped = {block: [] for block in CORE_MEMORY_BLOCKS}
        for row in rows:
            grouped[row["block_name"]].append(row["content"])
        for block in CORE_MEMORY_BLOCKS:
            if grouped[block]:
                lines.append(f"{CORE_MEMORY_LABELS[block]}: " + "\n".join(grouped[block]))
        lines.append("=" * len(lines[0]))
        text = "\n".join(lines)
        if len(text) > max_chars:
            suffix = "\n...(CoreMemory 截断)"
            text = text[: max_chars - len(suffix)] + suffix
        return text
