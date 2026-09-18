# -*- coding: utf-8 -*-
"""1.1.0 Task 4 — 联邦授权 Grants（scope/action/有效期/撤销）。

Roadmap 差距#5。语义模型（双端对称，各自决定权）：
* 发送端 export_events 按 **outbound grant**（grantor=本节点,
  grantee=peer, action=read, scope⊇key, 未过期未撤销）过滤事件。
* 接收端 ingest_envelope 按 **inbound grant**（grantor=from_peer,
  grantee=本节点, action=sync）验收入；信封内任一事件缺 grant →
  **整包拒收**（部分接受会让两节点账本无声发散——账本一致性>吞吐）。
* 强制开关（向后兼容）：本节点存在任何 grant 行、或 grants_mode=force、
  或 MIMIR_FEDERATION_GRANTS=force → 进强制模式；零策略+未 force 维持
  legacy 全通（B2/B3 实测链不断）。撤销后行仍在 → 保持强制（撤销的
  语义是「该授权不再放行」，绝不能是「库退回全放开时代」）。
* scope 语法：`*` 全量 / `前缀/*` / 精确 key；`*` 只许整体或结尾一次。

TDD RED → GREEN.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.federation import FederationError, FederationService
from mimir_v8.store import CanonicalStore


class _TwoNodes:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.n1 = FederationService(
            CanonicalStore(root / "n1.db"), node_id="n100")
        self.n2 = FederationService(
            CanonicalStore(root / "n2.db"), node_id="desktop")
        self.n1.register_peer("desktop", self.n2.public_key)
        self.n2.register_peer("n100", self.n1.public_key)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()


SKILL_KEY = "shared/skill/k8s-drain"
PREF_KEY = "shared/pref/theme"


def _publish(node, key, *, lamport=1, value="v1"):
    node.append_event({"key": key, "lamport": lamport,
                       "node_id": node.node_id, "op": "set", "value": value})


class TestGrantLifecycle(unittest.TestCase):

    def test_check_grant_matrix(self):
        """scope/action/revoke 三轴判据。"""
        with _TwoNodes() as t:
            g = t.n1.create_grant(SKILL_KEY, "read", grantee="desktop",
                                  ttl_hours=24)
            self.assertTrue(t.n1.check_grant("n100", "desktop",
                                             SKILL_KEY, "read"))
            self.assertFalse(t.n1.check_grant("n100", "desktop",
                                              PREF_KEY, "read"))
            self.assertFalse(t.n1.check_grant("n100", "desktop",
                                              SKILL_KEY, "sync"))
            t.n1.revoke_grant(g["grant_id"])
            self.assertFalse(t.n1.check_grant("n100", "desktop",
                                              SKILL_KEY, "read"))

    def test_expiry_denied_but_recomputable_at_earlier_clock(self):
        with _TwoNodes() as t:
            t.n1.create_grant(SKILL_KEY, "read", grantee="desktop",
                              expires_at="2020-01-01T00:00:00+00:00")
            self.assertFalse(t.n1.check_grant("n100", "desktop",
                                              SKILL_KEY, "read"))
            self.assertTrue(t.n1.check_grant("n100", "desktop",
                                             SKILL_KEY, "read",
                                             at="2019-06-01T00:00:00+00:00"))

    def test_prefix_and_wildcard_scope(self):
        with _TwoNodes() as t:
            t.n1.create_grant("shared/skill/*", "read", grantee="desktop",
                              ttl_hours=24)
            self.assertTrue(t.n1.check_grant("n100", "desktop",
                                             SKILL_KEY, "read"))
            self.assertTrue(t.n1.check_grant("n100", "desktop",
                                             "shared/skill/other", "read"))
            self.assertFalse(t.n1.check_grant("n100", "desktop",
                                              PREF_KEY, "read"))
            t.n1.create_grant("*", "read", grantee="jarvis", ttl_hours=24)
            self.assertTrue(t.n1.check_grant("n100", "jarvis",
                                             "anything/else", "read"))

    def test_invalid_scope_action_grantee_rejected(self):
        with _TwoNodes() as t:
            for bad in ("", "shared/*x", "a/b?c", "mid/*/x", "shared/**"):
                with self.subTest(scope=bad):
                    with self.assertRaises(FederationError):
                        t.n1.create_grant(bad, "read", grantee="desktop",
                                          ttl_hours=1)
            with self.assertRaises(FederationError):
                t.n1.create_grant(SKILL_KEY, "delete", grantee="desktop",
                                  ttl_hours=1)
            with self.assertRaises(FederationError):
                t.n1.create_grant(SKILL_KEY, "read", grantee="  ")


class TestExportEnforcement(unittest.TestCase):

    def test_legacy_no_grant_full_export(self):
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            _publish(t.n1, PREF_KEY)
            env = t.n1.export_events(since=0, to_peer="desktop")
            self.assertEqual(env["count"], 2)

    def test_first_grant_enables_filter(self):
        """装第一个 grant 即进强制模式：未覆盖 key 不发。"""
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            _publish(t.n1, PREF_KEY)
            t.n1.create_grant("shared/skill/*", "read", grantee="desktop",
                              ttl_hours=24)
            env = t.n1.export_events(since=0, to_peer="desktop")
            self.assertEqual(env["count"], 1)
            self.assertEqual(env["exported_keys"], [SKILL_KEY])

    def test_revoked_grant_keeps_mode_strict(self):
        """撤销 ≠ 退回全放开：撤销后仍强制，该 peer 什么都收不到。"""
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            g = t.n1.create_grant(SKILL_KEY, "read", grantee="desktop",
                                  ttl_hours=24)
            t.n1.revoke_grant(g["grant_id"])
            env = t.n1.export_events(since=0, to_peer="desktop")
            self.assertEqual(env["count"], 0)

    def test_force_mode_denies_without_grants(self):
        """grants_mode=force：零 grant 全拒（空集不静默放开）。"""
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            t.n1.grants_mode = "force"
            env = t.n1.export_events(since=0, to_peer="desktop")
            self.assertEqual(env["count"], 0)

    def test_export_to_unregistered_peer_still_ciphers(self):
        """export 以发送者自己 key 加密，不预检 to_peer 注册（注册校验归
        ingest fail-closed）。给未注册名字发信封仍应成功产出密文。"""
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            env = t.n1.export_events(since=0, to_peer="ghost")
            self.assertIn("ciphertext", env)
            self.assertEqual(env["count"], 1)


class TestIngestEnforcement(unittest.TestCase):

    def test_ingest_denied_when_policy_covers_nothing(self):
        """接收端已声明策略（有行）却不覆盖 key → 拒收；补 inbound grant 放行。"""
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            t.n1.create_grant("shared/skill/*", "read", grantee="desktop",
                              ttl_hours=24)
            env = t.n1.export_events(since=0, to_peer="desktop")
            self.assertEqual(env["count"], 1)
            t.n2.create_grant(PREF_KEY, "sync", grantor="n100",
                              ttl_hours=24)  # 策略声明了但范围不含
            with self.assertRaises(FederationError) as ctx:
                t.n2.ingest_envelope(env)
            self.assertIn("grant_denied", str(ctx.exception))
            t.n2.create_grant("shared/skill/*", "sync", grantor="n100",
                              ttl_hours=24)
            r = t.n2.ingest_envelope(env)
            self.assertEqual(r["applied"], 1)

    def test_ingest_legacy_open_when_no_policy(self):
        """零策略节点维持 legacy 全通（B2/B3 实测链兼容）。"""
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            t.n1.create_grant("*", "read", grantee="desktop", ttl_hours=24)
            env = t.n1.export_events(since=0, to_peer="desktop")
            r = t.n2.ingest_envelope(env)  # n2 零行、未 force
            self.assertEqual(r["applied"], 1)

    def test_partial_missing_grant_rejects_whole_envelope(self):
        """混合 key 缺一即整包拒收，且不留残（账本零写入）。"""
        with _TwoNodes() as t:
            _publish(t.n1, SKILL_KEY)
            _publish(t.n1, PREF_KEY)
            t.n1.create_grant("*", "read", grantee="desktop", ttl_hours=24)
            t.n2.create_grant("shared/skill/*", "sync", grantor="n100",
                              ttl_hours=24)
            env = t.n1.export_events(since=0, to_peer="desktop")
            self.assertEqual(env["count"], 2)
            with self.assertRaises(FederationError):
                t.n2.ingest_envelope(env)
            self.assertIsNone(t.n2.crdt_state(SKILL_KEY))
            self.assertIsNone(t.n2.crdt_state(PREF_KEY))
            t.n2.create_grant("shared/pref/*", "sync", grantor="n100",
                              ttl_hours=24)
            r = t.n2.ingest_envelope(env)
            self.assertEqual(r["applied"], 2)


class TestGrantAudit(unittest.TestCase):

    def test_grants_are_enumerable(self):
        """配置面可审计（留痕纪律同源）：list_grants 枚举含判据字段。"""
        with _TwoNodes() as t:
            t.n1.create_grant(SKILL_KEY, "read", grantee="desktop",
                              ttl_hours=1)
            rows = t.n1.list_grants()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["scope"], SKILL_KEY)
            self.assertEqual(rows[0]["action"], "read")
            self.assertEqual(rows[0]["grantee_node"], "desktop")
            self.assertIsNone(rows[0]["revoked_at"])

    def test_revoke_unknown_grant_rejected(self):
        with _TwoNodes() as t:
            with self.assertRaises(FederationError):
                t.n1.revoke_grant("no-such-grant")


if __name__ == "__main__":
    unittest.main()
