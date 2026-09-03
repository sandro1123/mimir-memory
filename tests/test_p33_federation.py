"""Mímir v14.0 — 跨节点去中心化加密联邦 (spec 阶段四任务2).

多台家庭服务器（N100、台式机、云端节点）基于 CRDT 事件流加密同步：

- federation_events 是 append-only CRDT 事件流：每次 fact 变更一行，
  携带 lamport 时钟与 node_id。同 key 冲突按 LWW（last-writer-wins）
  语义合并：lamport 高者胜，同刻比 node_id（全序，无分叉）。
- 离线容灾：节点断线期间各自写入，重连后交换事件流，按 lamport
  序重放，最终一致。
- 加密信封：事件出节点前 Fernet 加密，入节点解密验签；未注册 peer
  的密文拒收（Fail-Closed）。
- peer 注册表：federation_peers 记录 node_id、端点、共享密钥指纹。

工程四严律：TDD 先行 / 不可变事件流 / 绝对路径安全 / 全量回归。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.federation import (
    FederationError,
    FederationService,
    decrypt_envelope,
    encrypt_envelope,
)
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _fact(store, content, *, owner="mentor", fact_type="pattern"):
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain="knowledge",
            fact_type=fact_type,
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status="confirmed",
        ),
        actor_principal=owner,
    )["fact_id"]


class _TwoNodes:
    """Two independent stores simulating N100 + desktop nodes."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.n1 = FederationService(
            CanonicalStore(root / "n1.db"), node_id="n100"
        )
        self.n2 = FederationService(
            CanonicalStore(root / "n2.db"), node_id="desktop"
        )
        # pre-shared key handshake: n1 registers n2's key and vice versa
        self.n1.register_peer("desktop", self.n2.public_key)
        self.n2.register_peer("n100", self.n1.public_key)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()


class TestEnvelopeCrypto(unittest.TestCase):
    def test_roundtrip(self):
        key = FederationService.generate_key()
        token = encrypt_envelope({"a": 1}, key)
        self.assertNotIn('"a"', token[:120])
        self.assertEqual(decrypt_envelope(token, key), {"a": 1})

    def test_wrong_key_rejected(self):
        token = encrypt_envelope({"a": 1}, FederationService.generate_key())
        with self.assertRaises(FederationError):
            decrypt_envelope(token, FederationService.generate_key())


class TestFederationLWW(unittest.TestCase):
    def test_higher_lamport_wins(self):
        with _TwoNodes() as t:
            t.n1.append_event({"key": "x", "lamport": 5, "node_id": "n100",
                               "op": "set", "value": "n1-writes"})
            t.n1.append_event({"key": "x", "lamport": 9, "node_id": "n100",
                               "op": "set", "value": "later"})
            state = t.n1.crdt_state("x")
            self.assertEqual(state["value"], "later")
            self.assertEqual(state["lamport"], 9)

    def test_same_lamport_tiebreak_by_node_id(self):
        with _TwoNodes() as t:
            # n100 (sorted) beats desktop at equal lamport — deterministic
            t.n1.append_event({"key": "x", "lamport": 7, "node_id": "desktop",
                               "op": "set", "value": "from-desktop"})
            t.n1.append_event({"key": "x", "lamport": 7, "node_id": "n100",
                               "op": "set", "value": "from-n100"})
            state = t.n1.crdt_state("x")
            self.assertEqual(state["value"], "from-n100")

    def test_merge_is_commutative(self):
        """CRDT 合并可交换：A∘B == B∘A（最终一致的无分叉保证）。"""
        with _TwoNodes() as t:
            t.n1.append_event({"key": "x", "lamport": 3, "node_id": "n100",
                               "op": "set", "value": "a"})
            t.n2.append_event({"key": "x", "lamport": 5, "node_id": "desktop",
                               "op": "set", "value": "b"})
            # sync both directions
            t.n2.ingest_envelope(t.n1.export_events(since=0, to_peer="desktop"))
            env2 = t.n2.export_events(since=0, to_peer="n100")
            t.n1.ingest_envelope(env2)
            self.assertEqual(t.n1.crdt_state("x")["value"], "b")
            self.assertEqual(t.n2.crdt_state("x")["value"], "b")


class TestFederationSync(unittest.TestCase):
    def test_export_import_roundtrip(self):
        with _TwoNodes() as t:
            t.n1.append_event({"key": "k1", "lamport": 1, "node_id": "n100",
                               "op": "set", "value": "v1"})
            env = t.n1.export_events(since=0, to_peer="desktop")
            result = t.n2.ingest_envelope(env)
            self.assertEqual(result["applied"], 1)
            self.assertEqual(t.n2.crdt_state("k1")["value"], "v1")

    def test_offline_then_reconnect_converges(self):
        """离线容灾：两节点各自演进，重连后最终一致。"""
        with _TwoNodes() as t:
            t.n1.append_event({"key": "x", "lamport": 2, "node_id": "n100",
                               "op": "set", "value": "offline-n1"})
            t.n2.append_event({"key": "x", "lamport": 4, "node_id": "desktop",
                               "op": "set", "value": "offline-n2"})
            # reconnect: bidirectional exchange
            t.n2.ingest_envelope(t.n1.export_events(since=0, to_peer="desktop"))
            t.n1.ingest_envelope(t.n2.export_events(since=0, to_peer="n100"))
            self.assertEqual(t.n1.crdt_state("x")["value"], "offline-n2")
            self.assertEqual(t.n2.crdt_state("x")["value"], "offline-n2")
            # a second exchange converges to no-op
            r1 = t.n1.ingest_envelope(t.n2.export_events(since=0, to_peer="n100"))
            r2 = t.n2.ingest_envelope(t.n1.export_events(since=0, to_peer="desktop"))
            self.assertEqual(r1["applied"], 0)
            self.assertEqual(r2["applied"], 0)

    def test_since_is_incremental(self):
        with _TwoNodes() as t:
            for i in range(3):
                t.n1.append_event({"key": f"k{i}", "lamport": i + 1,
                                   "node_id": "n100", "op": "set",
                                   "value": f"v{i}"})
            # a fresh node only receives the suffix after cursor 2 —
            # events 1/2 never ride the envelope, verified on the
            # receiving side (wire format is ciphertext)
            fresh = FederationService(
                CanonicalStore(Path(t._tmp.name) / "fresh.db"),
                node_id="cloud",
            )
            fresh.register_peer("n100", t.n1.public_key)
            t.n1.register_peer("cloud", fresh.public_key)
            later = t.n1.export_events(since=2, to_peer="cloud")
            result = fresh.ingest_envelope(later)
            self.assertEqual(result["applied"], 1)
            self.assertEqual(fresh.crdt_state("k2")["value"], "v2")
            self.assertIsNone(fresh.crdt_state("k0"))
            self.assertIsNone(fresh.crdt_state("k1"))

    def test_ingest_rejects_unregistered_sender(self):
        """未注册 peer 的信封拒收（Fail-Closed）。"""
        with _TwoNodes() as t:
            rogue = FederationService(
                CanonicalStore(Path(self._rogue_db())), node_id="rogue"
            )
            rogue.append_event({"key": "x", "lamport": 99, "node_id": "rogue",
                                "op": "set", "value": "evil"})
            env = rogue.export_events(since=0, to_peer="desktop")
            with self.assertRaises(FederationError):
                t.n1.ingest_envelope(env)

    def test_ingest_rejects_tampered_envelope(self):
        with _TwoNodes() as t:
            t.n1.append_event({"key": "x", "lamport": 1, "node_id": "n100",
                               "op": "set", "value": "v"})
            env = t.n1.export_events(since=0, to_peer="desktop")
            tampered = dict(env)
            tampered["ciphertext"] = env["ciphertext"][:-8] + "AAAAAAAA"
            with self.assertRaises(FederationError):
                t.n2.ingest_envelope(tampered)

    def _rogue_db(self):
        import tempfile as tf

        self._rogue_tmp = tf.TemporaryDirectory()
        return str(Path(self._rogue_tmp.name) / "rogue.db")


class TestFederationPeerRegistry(unittest.TestCase):
    def test_register_and_list_peers(self):
        with _TwoNodes() as t:
            peers = t.n1.list_peers()
            self.assertEqual([p["node_id"] for p in peers], ["desktop"])

    def test_key_fingerprint_is_stable(self):
        with _TwoNodes() as t:
            fp1 = t.n1.peer_key_fingerprint("desktop")
            fp2 = t.n1.peer_key_fingerprint("desktop")
            self.assertEqual(fp1, fp2)
            self.assertTrue(fp1)


if __name__ == "__main__":
    unittest.main()
