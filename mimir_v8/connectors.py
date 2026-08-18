"""Read-only incremental connectors for Mímir v8.1 conversation ingestion."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

from .learning import ConversationEnvelope, ConversationMessage, LearningService
from .schema import ValidationError
from .store import CanonicalStore, canonical_json, sha256_text, utc_now


class ConnectorError(RuntimeError):
    """Raised when an external connector cannot be read safely."""


class HermesStateCDC:
    """Incrementally ingest Hermes sessions/messages without modifying state.db."""

    def __init__(
        self,
        store: CanonicalStore,
        learning: LearningService,
        state_path: str | Path,
        *,
        connector_id: str,
        owner_principal: str,
        memory_mode: str = "observe",
        retention_class: str = "short",
    ):
        self.store = store
        self.learning = learning
        self.state_path = Path(state_path)
        self.connector_id = connector_id.strip()
        self.owner_principal = owner_principal.strip()
        self.memory_mode = memory_mode
        self.retention_class = retention_class
        if not self.connector_id:
            raise ValidationError("connector_id is required")

    def _connect_read_only(self):
        if not self.state_path.is_file():
            raise ConnectorError(f"Hermes state database does not exist: {self.state_path}")
        # Do not use immutable=1 for a live Hermes database: immutable mode may
        # ignore WAL changes that have not yet been checkpointed into state.db.
        # mode=ro plus query_only preserves the read-only boundary while letting
        # SQLite read the active WAL consistently.
        uri = self.state_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _columns(connection, table: str) -> set[str]:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _pick(columns: set[str], names: tuple[str, ...], *, required: bool = False) -> str | None:
        for name in names:
            if name in columns:
                return name
        if required:
            raise ConnectorError(f"required Hermes column is missing: one of {names}")
        return None

    def _checkpoint(self) -> dict:
        with contextlib.closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT cursor_json,status FROM connector_checkpoints WHERE connector_id=?",
                (self.connector_id,),
            ).fetchone()
        if not row:
            return {"last_message_rowid": 0}
        if row["status"] == "paused":
            raise ConnectorError("connector is paused")
        try:
            cursor = json.loads(row["cursor_json"])
        except (TypeError, ValueError) as exc:
            raise ConnectorError("connector checkpoint is invalid") from exc
        return {"last_message_rowid": int(cursor.get("last_message_rowid", 0))}

    def collect_once(self, *, actor_principal: str, limit: int = 500) -> dict:
        if limit < 1 or limit > 5000:
            raise ValidationError("limit must be between 1 and 5000")
        checkpoint = self._checkpoint()
        last_rowid = checkpoint["last_message_rowid"]
        with contextlib.closing(self._connect_read_only()) as source:
            tables = {row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "messages" not in tables:
                raise ConnectorError("Hermes state database has no messages table")
            columns = self._columns(source, "messages")
            session_col = self._pick(columns, ("session_id", "session_uuid", "conversation_id"), required=True)
            role_col = self._pick(columns, ("role", "message_role", "author_role"), required=True)
            content_col = self._pick(columns, ("content", "text", "message", "body"), required=True)
            created_col = self._pick(columns, ("created_at", "timestamp", "created", "time"))
            principal_col = self._pick(columns, ("principal_id", "agent_id", "author", "sender"))
            select = ["rowid AS _rowid", f"{session_col} AS _session", f"{role_col} AS _role", f"{content_col} AS _content"]
            select.append(f"{created_col} AS _created" if created_col else "NULL AS _created")
            select.append(f"{principal_col} AS _principal" if principal_col else "NULL AS _principal")
            rows = source.execute(
                f"SELECT {','.join(select)} FROM messages WHERE rowid>? ORDER BY rowid LIMIT ?",
                (last_rowid, limit),
            ).fetchall()
        if not rows:
            return {"connector_id": self.connector_id, "messages_seen": 0, "sessions_ingested": 0, "last_message_rowid": last_rowid}

        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            session_id = str(row["_session"] or "unknown")
            groups.setdefault(session_id, []).append(row)
        ingested = []
        for session_id, messages in groups.items():
            first_rowid = int(messages[0]["_rowid"])
            final_rowid = int(messages[-1]["_rowid"])
            envelope = ConversationEnvelope(
                connector_type="hermes_cdc",
                connector_id=self.connector_id,
                session_id=session_id,
                owner_principal=self.owner_principal,
                memory_mode=self.memory_mode,
                retention_class=self.retention_class,
                source_uri=f"hermes-state://{self.connector_id}/sessions/{session_id}",
                messages=tuple(
                    ConversationMessage(
                        role=str(item["_role"] or "unknown") if str(item["_role"] or "unknown") in {"system", "user", "assistant", "tool", "developer", "unknown"} else "unknown",
                        content=str(item["_content"] or "").strip(),
                        principal_id=str(item["_principal"]).strip() if item["_principal"] else None,
                        created_at=str(item["_created"]) if item["_created"] else None,
                        metadata={"hermes_rowid": int(item["_rowid"])},
                    )
                    for item in messages
                    if str(item["_content"] or "").strip()
                ),
                metadata={"first_message_rowid": first_rowid, "last_message_rowid": final_rowid},
                idempotency_key=f"hermes-cdc:{self.connector_id}:{session_id}:{first_rowid}:{final_rowid}",
            )
            if not envelope.messages:
                continue
            ingested.append(self.learning.ingest_conversation(envelope, actor_principal))
        new_last = int(rows[-1]["_rowid"])
        cursor = {"last_message_rowid": new_last}
        now = utc_now()
        source_hash = sha256_text(canonical_json({"path": str(self.state_path.resolve()), **cursor}))
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO connector_checkpoints(connector_id,connector_type,cursor_json,source_hash,status,updated_at,last_error_code) VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(connector_id) DO UPDATE SET connector_type=excluded.connector_type,cursor_json=excluded.cursor_json,source_hash=excluded.source_hash,status='active',updated_at=excluded.updated_at,last_error_code=NULL",
                (self.connector_id, "hermes_cdc", canonical_json(cursor), source_hash, "active", now),
            )
        return {
            "connector_id": self.connector_id,
            "messages_seen": len(rows),
            "sessions_ingested": len(ingested),
            "last_message_rowid": new_last,
            "ingestion_runs": ingested,
        }
