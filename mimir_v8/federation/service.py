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
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..store import CanonicalStore, new_id, utc_now
from contextlib import closing

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
    """CREATE TABLE IF NOT EXISTS federation_grants (
        grant_id TEXT PRIMARY KEY,
        grantor_node TEXT NOT NULL,
        grantee_node TEXT NOT NULL,
        scope TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('read','sync')),
        expires_at TEXT,
        revoked_at TEXT,
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE INDEX IF NOT EXISTS idx_federation_grants_lookup
       ON federation_grants(grantor_node, grantee_node, action)""",
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


# P0-L（#5）：单 envelope 事件数上限——协议级载荷纪律。
MAX_ENVELOPE_EVENTS = 5000

#: grants scope 合法字符（路径式 key 命名空间 + 结尾通配）。`*` 只许整体
#: 或作为末段一次（"shared/skill/*" 合法；"shared/*x"、"**"、中间 * 均拒）。
_GRANT_SCOPE_RE = re.compile(r"^[A-Za-z0-9_\-./:*]+$")
_GRANT_ACTIONS = ("read", "sync")


def _scope_matches(scope: str, key: str) -> bool:
    """scope ⊇ key 判据：'*' 全量；'前缀/*' 段前缀；否则精确相等。"""
    if scope == "*":
        return True
    if scope.endswith("/*"):
        return key.startswith(scope[:-1])  # 含 '/' 段边界："shared/skill/" 前缀
    return scope == key


def _scope_valid(scope: str) -> bool:
    if not scope or not _GRANT_SCOPE_RE.match(scope):
        return False
    if "*" in scope:
        return scope == "*" or (scope.endswith("/*")
                                and "*" not in scope[:-1])
    return True

class FederationService:
    """One node's federation unit: ledger, peer registry, sync protocol."""

    def __init__(self, store: CanonicalStore, *, node_id: str,
                 grants_mode: str | None = None):
        if not node_id or not node_id.strip():
            raise FederationError("node_id is required")
        self.store = store
        self.node_id = node_id.strip()
        # grants 强制开关："auto"（默认，有任何 grant 行才进强制）/
        # "force"（零策略也全拒）/ "off"（永远 legacy 全通）。环境变量
        # MIMIR_FEDERATION_GRANTS 提供部署期默认（参数优先）。
        mode = (grants_mode or os.environ.get("MIMIR_FEDERATION_GRANTS")
                or "auto")
        if mode not in ("auto", "force", "off"):
            raise FederationError(f"invalid grants_mode: {mode!r}")
        self.grants_mode = mode
        # Each node holds one key pair for the federation: the private
        # half encrypts outgoing envelopes, the public half is what peers
        # register. (Symmetric Fernet: the "public" key IS the shared key —
        # registering it with a peer is the out-of-band trust handshake.)
        # P0-L（v14.2，#5 硬化1）：本节点密钥持久化——首次生成落
        # node_key 表，重启复用。旧形态每实例重新生成，服务重启即
        # 全部 peer 注册作废（peer 侧注册的还是旧钥）。
        with self.store.transaction() as connection:
            self._ensure_tables(connection)
            self._key = self._load_or_create_key(connection)

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
        with closing(self.store.connect()) as connection:
            self._ensure_tables(connection)
            rows = connection.execute(
                """SELECT node_id, fingerprint, registered_at
                FROM federation_peers ORDER BY node_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def peer_key_fingerprint(self, node_id: str) -> str | None:
        with closing(self.store.connect()) as connection:
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

    # ── grants（1.1.0 Roadmap 差距#5：授权即策略，配置可审计） ─────────

    def create_grant(self, scope: str, action: str, *, grantee: str | None = None,
                     grantor: str | None = None, ttl_hours: float | None = None,
                     expires_at: str | None = None) -> dict:
        """授予 grantor→grantee 对 scope 的 action 权限（sync=入站收）。

        两个身份参数都缺省本节点，调用方只填**不是自己的那一端**：
        * 出站读授权「让 desktop 读我的 skill/*」：`grantee="desktop"`（grantor 默认本节点）
        * 入站同授权「接受 n100 sync skill/* 进来」：`grantor="n100"`（grantee 默认本节点）
        action 语义：read=允许对方从本节点读（export 端判据）；sync=允许
        本节点接收对方的 sync（ingest 端判据）。grantor==grantee 是退化自
        授权，拒绝——跨节点授权的两端必须是不同节点。
        有效期二选一：ttl_hours（自现在起算）或 expires_at（绝对时刻）；
        都不给=永久（撤销即失效）。
        """
        scope = (scope or "").strip()
        if not _scope_valid(scope):
            raise FederationError(f"invalid grant scope: {scope!r}")
        if action not in _GRANT_ACTIONS:
            raise FederationError(f"invalid grant action: {action!r}"
                                  f" (want one of {_GRANT_ACTIONS})")
        # 空串是「显式传了个空」=调用方失误；None 才是「没传」→默认本节点。
        if grantee is not None and not grantee.strip():
            raise FederationError("grantee is required")
        grantee = (grantee or self.node_id).strip()
        grantor = (grantor or self.node_id).strip()
        if grantor == grantee:
            raise FederationError(
                "grantor and grantee must differ — a cross-node grant to "
                "yourself is a no-op that only hides a wiring mistake")
        if expires_at is None and ttl_hours is not None:
            if ttl_hours <= 0:
                raise FederationError("ttl_hours must be > 0")
            delta = timedelta(hours=float(ttl_hours))
            expires_at = (datetime.now(timezone.utc) + delta).isoformat(
                timespec="seconds")
        grant_id = new_id()
        now = utc_now()
        with self.store.transaction() as connection:
            self._ensure_tables(connection)
            connection.execute(
                """INSERT INTO federation_grants(
                       grant_id, grantor_node, grantee_node, scope, action,
                       expires_at, revoked_at, created_at
                   ) VALUES(?,?,?,?,?,?,NULL,?)""",
                (grant_id, grantor, grantee, scope, action, expires_at, now),
            )
        return {"grant_id": grant_id, "grantor_node": grantor,
                "grantee_node": grantee, "scope": scope, "action": action,
                "expires_at": expires_at, "revoked_at": None,
                "created_at": now}

    def revoke_grant(self, grant_id: str) -> dict:
        """撤销即置 revoked_at（保留行=审计留痕，且库维持强制模式）。"""
        grant_id = (grant_id or "").strip()
        if not grant_id:
            raise FederationError("grant_id is required")
        now = utc_now()
        with self.store.transaction() as connection:
            self._ensure_tables(connection)
            cursor = connection.execute(
                "UPDATE federation_grants SET revoked_at=? "
                "WHERE grant_id=? AND revoked_at IS NULL",
                (now, grant_id),
            )
            if cursor.rowcount == 0:
                raise FederationError(f"grant not found or already revoked: "
                                      f"{grant_id!r}")
        return {"grant_id": grant_id, "revoked_at": now}

    def list_grants(self, *, include_revoked: bool = True) -> list[dict]:
        with closing(self.store.connect()) as connection:
            self._ensure_tables(connection)
            query = ("SELECT grant_id, grantor_node, grantee_node, scope, "
                     "action, expires_at, revoked_at, created_at "
                     "FROM federation_grants")
            if not include_revoked:
                query += " WHERE revoked_at IS NULL"
            rows = connection.execute(query + " ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def check_grant(self, grantor: str, grantee: str, key: str,
                    action: str, *, at: str | None = None) -> bool:
        """(grantor,grantee,action) 下 scope⊇key 且未过期未撤销的 grant 存在性。

        ``at`` 是判定时钟（默认现在）：审计复算/测试可拨回历史时刻。
        """
        with closing(self.store.connect()) as connection:
            self._ensure_tables(connection)
            rows = self._grants_rows(connection, grantor.strip(),
                                     grantee.strip(), action)
        return self._grant_allows(rows, key, at or utc_now())

    @staticmethod
    def _grants_rows(connection: sqlite3.Connection, grantor: str,
                     grantee: str, action: str) -> list[sqlite3.Row]:
        return connection.execute(
            """SELECT scope, expires_at, revoked_at FROM federation_grants
            WHERE grantor_node=? AND grantee_node=? AND action=?""",
            (grantor, grantee, action),
        ).fetchall()

    @staticmethod
    def _grant_allows(rows, key: str, moment: str) -> bool:
        for row in rows:
            if row["revoked_at"]:
                continue
            if row["expires_at"] and row["expires_at"] < moment:
                continue
            if _scope_matches(row["scope"], key):
                return True
        return False

    def _grants_enforced(self, connection: sqlite3.Connection) -> bool:
        """本节点是否进强制模式：force/off 直通；auto=存在任何相关策略行才收紧。

        「本节点相关」= grantor 或 grantee 是本节点（入站登记也算声明）。
        注意「任何行」含过期/已撤销——策略声明过的库不退回全放开时代
        （撤销语义=不再放行，不是=回到无策略）。
        """
        if self.grants_mode == "force":
            return True
        if self.grants_mode == "off":
            return False
        row = connection.execute(
            "SELECT 1 FROM federation_grants "
            "WHERE grantor_node=? OR grantee_node=? LIMIT 1",
            (self.node_id, self.node_id),
        ).fetchone()
        return row is not None

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
        # P0-L（#5 硬化4）：上界 10^8——lamport 是逻辑计数器，真实节
        # 点终生量级远低于此；恶意 peer 用 1e9（#5 实测攻击值）恒久删
        # 除任意键（LWW 高者胜且无 tombstone 回滚路）在协议层拒绝。
        if not isinstance(lamport, int) or lamport < 0 or lamport >= 10**8:
            raise FederationError("event.lamport must be a non-negative int < 10^8")
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
        with closing(self.store.connect()) as connection:
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
        """Serialize the events after cursor `since`, encrypted to one peer.

        1.1.0 grants：强制模式下只发对 to_peer 存在 outbound read grant
        覆盖的 key（发送端独立决定权，与 ingest 端 inbound sync 对称）。
        注：export 不校验 to_peer 是否注册——信封以**发送者自己的 key** 加密
        （对称 Fernet），注册校验本就发生在 ingest（未注册 sender / 收件人
        非本节点均 fail-closed），在这里预检只会抢在接收端拒收之前拦下
        「构造攻击信封」的正当测试路径，且无额外安全收益。
        """
        if not to_peer or not to_peer.strip():
            raise FederationError("to_peer is required")
        to_peer = to_peer.strip()
        with closing(self.store.connect()) as connection:
            self._ensure_tables(connection)
            enforced = self._grants_enforced(connection)
            allowed_rows = (self._grants_rows(
                connection, self.node_id, to_peer, "read")
                if enforced else [])
            rows = connection.execute(
                """SELECT seq, crdt_key, lamport, node_id, op, value
                FROM federation_events WHERE seq > ?
                ORDER BY seq""",
                (since,),
            ).fetchall()
        moment = utc_now()
        events = []
        for row in rows:
            key = row["crdt_key"]
            if enforced and not self._grant_allows(allowed_rows, key, moment):
                continue
            events.append({
                "seq": row["seq"],
                "key": key,
                "lamport": row["lamport"],
                "node_id": row["node_id"],
                "op": row["op"],
                "value": json.loads(row["value"]) if row["value"] is not None else None,
            })
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
            # 运维诚实面：发了哪些 key 范围明文可见（key 名是元数据、
            # 不含 value，且 grant 的 scope 本就是 key 名——本就可见）。
            # 让「我到底同步了什么」不用解密密文就能审计。
            "exported_keys": [e["key"] for e in events],
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
        to_peer = str((envelope or {}).get("to_peer") or "")
        if not from_node or not ciphertext:
            raise FederationError("envelope requires from_node and ciphertext")
        # P0-L（#5 硬化5）：收件人必须是本节点——误投/劫持的 envelope
        # 拒收（fail closed），不再静默入账。
        if to_peer != self.node_id:
            raise FederationError(
                f"envelope addressed to {to_peer!r}, not this node ({self.node_id!r})")
        with closing(self.store.connect()) as connection:
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
        # P0-L（#5 硬化3/4）：载荷纪律——事件数上限防内存面轰炸；
        # 每事件 node_id 必须与 envelope 发送者一致——封死「C 持自己
        # 钥匙伪造 A 署名事件」的通道（解密钥持有者与事件署名解耦）。
        events = payload.get("events") or []
        if len(events) > MAX_ENVELOPE_EVENTS:
            raise FederationError(
                f"envelope carries {len(events)} events; cap is {MAX_ENVELOPE_EVENTS}")
        # P0-L 修订：署名者≠发送者是合法形态（CRDT 中继转发他人事件）。
        # 正确语义：事件署名者必须∈{注册 peers ∪ 本节点}——封死「C 伪造
        # 一个从未存在过的节点署名」的通道，同时不破坏中继/合并路径。
        registered = self._registered_node_ids()
        # P0-L 语义收敛：中继转发他人署名事件是 CRDT 合法形态，但接收方
        # 无法穷知全网节点。三面放行=署名者是本节点已注册 peer / 发送者
        # 本身 / 本节点账本里已见过的节点（此前同步引入的）。第 4 面：
        # 署名者均非以上 → 伪造（#5 的 C 伪造 A 署名攻击在此拦截）。
        known_signers = registered | self._seen_node_ids()
        for event in events:
            signer = str(event.get("node_id") or "")
            if signer and signer not in known_signers and signer != from_node:
                raise FederationError(
                    f"event signer {signer!r} is neither a registered peer,"
                    " a previously seen node, nor the envelope sender"
                    " — forged signature")
        # 1.1.0 grants：接收端独立决定权。本节点进强制模式（存在任何策略
        # 行，或 force）时，入站按 inbound sync grant（grantor=发送者,
        # grantee=本节点）验收；信封内任一事件 key 缺授权 → **整包拒收**
        # ——部分接受会让两节点账本无声发散，账本一致性 > 吞吐。零策略
        # 的 legacy 节点维持全通（B2/B3 实测链不在此断）。
        with closing(self.store.connect()) as connection:
            self._ensure_tables(connection)
            enforced = self._grants_enforced(connection)
            inbound_rows = (self._grants_rows(
                connection, from_node, self.node_id, "sync")
                if enforced else [])
        if enforced:
            moment = utc_now()
            for event in events:
                key = str(event.get("key") or "")
                if not self._grant_allows(inbound_rows, key, moment):
                    raise FederationError(
                        f"grant_denied: no inbound sync grant from "
                        f"{from_node!r} covers key {key!r}")
        applied = 0
        for event in events:
            result = self.append_event(event)
            if not result.get("replayed"):
                applied += 1
        return {"from_node": from_node, "applied": applied,
                "received": len(events)}

    # ── internals ──────────────────────────────────────────────────────

    def _seen_node_ids(self) -> set[str]:
        with closing(self.store.connect()) as connection:
            self._ensure_tables(connection)
            rows = connection.execute(
                "SELECT DISTINCT node_id FROM federation_events").fetchall()
        return {str(r["node_id"]) for r in rows}

    def _registered_node_ids(self) -> set[str]:
        with closing(self.store.connect()) as connection:
            self._ensure_tables(connection)
            rows = connection.execute(
                "SELECT node_id FROM federation_peers").fetchall()
        return {str(r["node_id"]) for r in rows}

    def _ensure_tables(self, connection: sqlite3.Connection) -> None:
        for statement in FEDERATION_STATEMENTS:
            connection.execute(statement)
        # P0-L：本节点密钥持久表（守卫式 IF NOT EXISTS）
        connection.execute(
            """CREATE TABLE IF NOT EXISTS federation_local_key (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                node_id TEXT NOT NULL,
                key TEXT NOT NULL,
                created_at TEXT NOT NULL
            ) STRICT""")

    def _load_or_create_key(self, connection) -> str:
        row = connection.execute(
            "SELECT key FROM federation_local_key WHERE singleton=1"
        ).fetchone()
        if row is not None:
            return str(row["key"])
        key = generate_key()
        from ..store import utc_now
        connection.execute(
            "INSERT INTO federation_local_key(singleton, node_id, key, created_at)"
            " VALUES(1,?,?,?)",
            (self.node_id, key, utc_now()),
        )
        return key
