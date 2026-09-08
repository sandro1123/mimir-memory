# -*- coding: utf-8 -*-
"""P0-L 联邦协议硬化（GitHub issue #5 硬化四点）。

#5 实测四病：
1. 密钥每实例重新生成——服务重启全 peer 注册作废；
2. peer 公钥明文落表（登记面）；
3. 任一注册 peer 可伪造 from_node=A 实际持 B 钥（节点 C 实证）；
4. lamport 无上界——恶意 peer 可用 1e9 永久删除任意键（无回滚路）；
5. to_peer 未对本地节点校验。

修面：密钥持久化（首次生成落库、重启复用）、ingest 验证
to_peer=本节点+from_node 与解密钥持有者一致、lamport 合法界、
envelope 事件数与载荷上限。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.federation.service import (
    FederationError, FederationService, encrypt_envelope,
)
from mimir_v8.store import CanonicalStore


def _store(tmp):
    return CanonicalStore(Path(tmp) / "canon.db")


class TestFederationHardening(unittest.TestCase):

    def _service(self, store):
        return FederationService(store, node_id="node-a")

    def test_key_persists_across_restarts(self):
        """硬化1：同一库上两个实例（模拟重启）拿到同一密钥。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            s1 = self._service(store)
            key1 = s1.public_key
            s2 = FederationService(store, node_id="node-a")  # 重启
            self.assertEqual(s2.public_key, key1)

    def test_ingest_rejects_misaddressed_envelope(self):
        """硬化5：to_peer 非 本节点 → 拒收。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            sa = self._service(store)
            sb = FederationService(store, node_id="node-b")
            sa.register_peer("node-b", sb.public_key)
            sb.register_peer("node-a", sa.public_key)
            export = sb.export_events(since=0, to_peer="node-a")
            tampered = dict(export)
            # 伪造：声明发给 node-x 但内容是发给 node-a 的
            tampered["to_peer"] = "node-x"
            with self.assertRaises(FederationError):
                sa.ingest_envelope(tampered)

    def test_ingest_rejects_cross_peer_forgery(self):
        """硬化3：C 持自己钥匙伪造 from_node=A 的 envelope → 拒收。

        #5 实证：节点 C 用自己的钥加密、声明 from_node=A，旧代码
        用 A 的注册钥解密失败才侥幸挡住——但若 A/C 同钥（登记错误）
        则穿透。修后解密失败与 from_node≠解密钥持有者两条都显式拒绝。
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            sa = self._service(store)
            sc = FederationService(store, node_id="node-c")
            sa.register_peer("node-c", sc.public_key)
            forged = encrypt_envelope(
                {"from_node": "node-c", "to_peer": "node-a", "events": []},
                sc.public_key)
            # C 声明自己是 C —— 合法；但事件里若再伪造 A 的署名呢：
            # append_event 的 node_id 必须与 envelope sender 一致，见下例
            ok = sa.ingest_envelope(
                {"from_node": "node-c", "to_peer": "node-a",
                 "ciphertext": forged})
            self.assertEqual(ok["applied"], 0)
            # 伪造署名：envelope from_node=A 但内容签名持有者是 C
            forged2 = encrypt_envelope(
                {"from_node": "node-a", "to_peer": "node-a",
                 "events": [{"key": "k", "lamport": 1, "node_id": "node-a",
                             "op": "set", "value": "x"}]},
                sc.public_key)
            with self.assertRaises(FederationError):
                sa.ingest_envelope(
                    {"from_node": "node-a", "to_peer": "node-a",
                     "ciphertext": forged2})

    def test_lamport_bounds(self):
        """硬化4：lamport 上界——防 1e9 恒久删除攻击。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            sa = self._service(store)
            with self.assertRaises(FederationError):
                sa.append_event({"key": "k", "lamport": 10**9,
                                 "node_id": "node-a", "op": "delete"})

    def test_envelope_size_cap(self):
        """载荷上限：单 envelope 事件数超限拒收（防内存面轰炸）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            sa = self._service(store)
            sb = FederationService(store, node_id="node-b")
            sa.register_peer("node-b", sb.public_key)
            sb.register_peer("node-a", sa.public_key)
            big_payload = {"from_node": "node-b", "to_peer": "node-a",
                           "events": [
                               {"key": f"k{i}", "lamport": 1,
                                "node_id": "node-b", "op": "set", "value": "x"}
                               for i in range(6000)]}
            env = {"from_node": "node-b",
                   "ciphertext": encrypt_envelope(big_payload, sb.public_key)}
            with self.assertRaises(FederationError):
                sa.ingest_envelope(env)


if __name__ == "__main__":
    unittest.main()
