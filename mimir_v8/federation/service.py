"""Mímir v14.0 — 联邦同步协议单元 (CRDT event stream + Fernet envelopes).

See __init__.py for the design narrative. This module is the
implementation: the append-only federation_events ledger lives in the
node's canonical store (additive tables, first-use creation, aligned
with the crystallize.py additive-DDL precedent), peers register
out-of-band key fingerprints, and every exported batch is a Fernet
ciphertext addressed to one peer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..store import CanonicalStore, new_id, utc_now

#: v14.0 additive DDL — append-only CRDT event stream + peer registry.
FEDERATION_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS federation_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        crdt_key TEXT NOT NULL,
        lamport INTEGER NOT NULL,
        node_id TEXT NOT NULL,
        op TEXT NOT NULL CHECK (op IN ('set','delete')),
        value TEXT,
        recorded_at TEXT NOT NULL,
        -- logical identity of one CRDT write: (crdt_key, lamport,
        -- node_id) is unique per write, so re-delivery is a no-op.
        UNIQUE (crdt_key, lamport, node_id)
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_federation_key
       ON federation_events(crdt_key, lamport DESC, node_id DESC)""",
    """CREATE TABLE IF NOT EXISTS federation_peers (
        node_id TEXT PRIMARY KEY,
        public_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        registered_at TEXT NOT NULL
    ) STRICT""",
)


class FederationError(RuntimeError):
    """Protocol/policy failure — Fail-Closed on anything unexpected."""


def generate_key() -> str:
    """Fresh Fernet key (base64 urlsafe, 32 bytes)."""
    return Fernet.generate_key().decode("ascii")


def encrypt_envelope(payload: dict, key: str) -> str:
    """Serialize+encrypt one payload dict into a Fernet token string."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        return Fernet(key.encode("ascii")).encrypt(raw.encode("utf-8")).decode("ascii")
    except (ValueError, TypeError) as exc:
        raise FederationError(f"envelope encryption failed: {exc}") from exc


def decrypt_envelope(token: str, key: str) -> dict:
    """Decrypt a Fernet token back to its payload dict (Fail-Closed)."""
    try:
        raw = Fernet(key.encode("ascii")).decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise FederationError("envelope authentication failed") from exc
    except (ValueError, TypeError) as exc:
        raise FederationError(f"envelope decryption failed: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FederationError("envelope payload is not valid JSON") from exc


class FederationService:
    """One node's federation unit: ledger, peer registry, sync protocol."""

    def __init__(self, store: CanonicalStore, *, node_id: str):
        if not node_id or not node_id.strip():
            raise FederationError("node_id is required")
        self.store = store
        self.node_id = node_id.strip()
        # Each node holds one key pair for the federation: the private
        # half encrypts outgoing envelopes, the public half is what peers
        # register. (Symmetric Fernet: the "public" key IS the shared key —
        # registering it with a peer is the out-of-band trust handshake.)
        self._key = generate_key()
        with self.store.transaction() as connection:
            self._ensure_tables(connection)

    # ── identity ──────────────────────────────────────────────────────

    @property
    def public_key(self) -> str:
        return self._key

    @staticmethod
    def generate_key() -> str:
        return generate_key()

    def register_peer(self, node_id: str, public_key: str) -> dict:
        """Trust handshake: register a peer's shared key (idempotent)."""
        if not node_id or not node_id.strip():
            raise FederationError("node_id is required")
        if not public_key or not public_key.strip():
            raise FederationError("public_key is required")
        node_id = node_id.strip()
        fingerprint = self.key_fingerprint(public_key)
        now = utc_now()
        with self.store.transaction() as connection:
            self._ensure_tables(connection)
            connection.execute(
                """INSERT INTO federation_peers(
                       node_id, public_key, fingerprint, registered_at
                   ) VALUES(?,?,?,?)
                   ON CONFLICT(node_id) DO UPDATE SET
                       public_key=excluded.public_key,
                       fingerprint=excluded.fingerprint""",
                (node_id, public_key.strip(), fingerprint, now),
            )
        return {"node_id": node_id, "fingerprint": fingerprint}

    def list_peers(self) -> list[dict]:
        with self.store.connect() as connection:
            self._ensure_tables(connection)
            rows = connection.execute(
                """SELECT node_id, fingerprint, registered_at
                FROM federation_peers ORDER BY node_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def peer_key_fingerprint(self, node_id: str) -> str | None:
        with self.store.connect() as connection:
            self._ensure_tables(connection)
            row = connection.execute(
                "SELECT fingerprint FROM federation_peers WHERE node_id=?",
                (node_id,),
            ).fetchone()
        return row["fingerprint"] if row else None

    @staticmethod
    def key_fingerprint(public_key: str) -> str:
        """Stable human-verifiable fingerprint of a shared key."""
        digest = hashlib.sha256(public_key.encode("ascii")).digest()
        return base64.b64encode(digest[:9]).decode("ascii")

    # ── CRDT ledger ───────────────────────────────────────────────────

    def append_event(self, event: dict) -> dict:
        """Append one CRDT event (local or ingested) to the stream.

        The ledger is append-only; crdt_state() folds it on demand.
        """
        key = str(event.get("key") or "")
        op = str(event.get("op") or "")
        lamport = event.get("lamport")
        node_id = str(event.get("node_id") or "")
        if not key:
            raise FederationError("event.key is required")
        if op not in ("set", "delete"):
            raise FederationError(f"unsupported op: {op}")
        if not isinstance(lamport, int) or lamport < 0:
            raise FederationError("event.lamport must be a non-negative int")
        if not node_id:
            raise FederationError("event.node_id is required")
        value = event.get("value")
        if op == "set" and value is None:
            raise FederationError("op=set requires a value")
        if op == "delete" and value is not None:
            raise FederationError("op=delete must not carry a value")
        now = utc_now()
        with self.store.transaction() as connection:
            self._ensure_tables(connection)
            event_id = new_id()
            cursor = connection.execute(
                """INSERT INTO federation_events(
                       event_id, crdt_key, lamport, node_id, op, value,
                       recorded_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(crdt_key, lamport, node_id) DO NOTHING""",
                (event_id, key, lamport, node_id, op,
                 json.dumps(value, ensure_ascii=False) if value is not None else None,
                 now),
            )
            fresh = cursor.rowcount == 1
        return {"event_id": event_id, "key": key, "lamport": lamport,
                "replayed": not fresh}

    def crdt_state(self, key: str) -> dict | None:
        """Fold the stream for one key under LWW: highest lamport wins,
        ties broken by node_id (descending) — a deterministic total
        order, so every node folds the same winner regardless of
        arrival order."""
        with self.store.connect() as connection:
            self._ensure_tables(connection)
            row = connection.execute(
                """SELECT lamport, node_id, op, value FROM federation_events
                WHERE crdt_key=?
                ORDER BY lamport DESC, node_id DESC
                LIMIT 1""",
                (key,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value"]) if row["value"] is not None else None
        return {
            "key": key,
            "lamport": row["lamport"],
            "node_id": row["node_id"],
            "op": row["op"],
            "value": value,
        }

    # ── sync protocol ──────────────────────────────────────────────────

    def export_events(self, *, since: int = 0, to_peer: str) -> dict:
        """Serialize the events after cursor `since`, encrypted to one peer."""
        if not to_peer or not to_peer.strip():
            raise FederationError("to_peer is required")
        to_peer = to_peer.strip()
        with self.store.connect() as connection:
            self._ensure_tables(connection)
            rows = connection.execute(
                """SELECT seq, crdt_key, lamport, node_id, op, value
                FROM federation_events WHERE seq > ?
                ORDER BY seq""",
                (since,),
            ).fetchall()
        events = [
            {
                "seq": row["seq"],
                "key": row["crdt_key"],
                "lamport": row["lamport"],
                "node_id": row["node_id"],
                "op": row["op"],
                "value": json.loads(row["value"]) if row["value"] is not None else None,
            }
            for row in rows
        ]
        payload = {
            "from_node": self.node_id,
            "to_peer": to_peer,
            "since": since,
            "events": events,
        }
        # Envelope is encrypted with the SENDER's key; the receiving peer
        # must have registered the sender (it holds the same key), which is
        # the trust handshake — unknown senders cannot even be decrypted.
        ciphertext = encrypt_envelope(payload, self._key)
        return {
            "from_node": self.node_id,
            "to_peer": to_peer,
            "since": since,
            "count": len(events),
            "ciphertext": ciphertext,
        }

    def ingest_envelope(self, envelope: dict) -> dict:
        """Verify + decrypt + apply an incoming envelope (Fail-Closed).

        The sender must be a registered peer; its registered key is the
        decryption key (shared-key handshake). Events re-apply idempotently
        — the ledger is append-only and folds deterministically, so
        duplicates are harmless.
        """
        from_node = str((envelope or {}).get("from_node") or "")
        ciphertext = str((envelope or {}).get("ciphertext") or "")
        if not from_node or not ciphertext:
            raise FederationError("envelope requires from_node and ciphertext")
        with self.store.connect() as connection:
            self._ensure_tables(connection)
            row = connection.execute(
                "SELECT public_key FROM federation_peers WHERE node_id=?",
                (from_node,),
            ).fetchone()
        if row is None:
            raise FederationError(
                f"sender {from_node} is not a registered peer — refusing"
            )
        payload = decrypt_envelope(ciphertext, row["public_key"])
        if payload.get("from_node") != from_node:
            raise FederationError("envelope from_node mismatch")
        applied = 0
        for event in payload.get("events") or []:
            result = self.append_event(event)
            if not result.get("replayed"):
                applied += 1
        return {"from_node": from_node, "applied": applied,
                "received": len(payload.get("events") or [])}

    # ── internals ──────────────────────────────────────────────────────

    def _ensure_tables(self, connection: sqlite3.Connection) -> None:
        for statement in FEDERATION_STATEMENTS:
            connection.execute(statement)
