# -*- coding: utf-8 -*-
"""1.1.0 Task 2 — POST /v8/facts/verify 链尾对账端点。

谱系链的运营出口。照抄 aduMEI 审计元教训：**验证端点绿灯制造虚假
信心**——破链必须显式判语（status=chain_broken），unsealed（1.1.0 前
从未封存的旧链）单独成态不得并入完好，空库不得报「一切正常」。

面：
1. 单/批 fact_ids：逐条 can_read ACL（owner 或 admin），无权限 403
2. 缺省全库：admin-only（防枚举）；聚合 chain_head_hash 做跨次对账锚
3. status 三态：ok / chain_broken / unsealed / empty（broken 优先）

TDD RED → GREEN.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.query import QueryKernel
from mimir_v8.schema import CreateFact, UpdateFact
from mimir_v8.store import CanonicalStore


class VerifyFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        tokens = {
            "mentor": "vfy-mentor-token",
            "jarvis": "vfy-jarvis-token",
            "admin": "vfy-admin-token",
        }
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": principal,
                        "token_sha256": hashlib.sha256(
                            token.encode("utf-8")).hexdigest(),
                        "scopes": ["read", "write", "ingest", "delete"],
                        "roles": ["admin"] if principal == "admin" else [],
                        "admin": principal == "admin",
                    }
                    for principal, token in tokens.items()
                ]
            }),
            encoding="utf-8",
        )
        context = ServiceContext(
            store=self.store, token_store=TokenStore(token_path),
            query=self.query,
        )
        self.client = TestClient(create_app(context),
                                 raise_server_exceptions=False)
        self.tokens = tokens

    def headers(self, principal="mentor"):
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    def seed(self, content, *, owner="mentor", visibility="all"):
        return self.store.create_fact(CreateFact(
            content=content, summary=content[:40], owner_principal=owner,
            domain="system", fact_type="event", visibility=visibility,
            sensitivity="internal", egress_policy="local_only",
            human_status="confirmed",
        ), actor_principal=owner)["fact_id"]

    def bump(self, fact_id, content):
        return self.store.update_fact(UpdateFact(
            fact_id=fact_id, content=content, expected_version=1,
            change_reason="verify 测试"), actor_principal="mentor")

    def tamper_v1(self, fact_id):
        """带外篡改模拟：DROP 触发器后伪造第一版快照。"""
        with self.store.connect() as c:
            c.execute("DROP TRIGGER IF EXISTS fact_versions_no_update")
            row = c.execute(
                "SELECT snapshot_json FROM fact_versions "
                "WHERE fact_id=? AND version=1", (fact_id,)).fetchone()
            snap = json.loads(row["snapshot_json"])
            snap["content"] = "伪造历史"
            c.execute(
                "UPDATE fact_versions SET snapshot_json=? "
                "WHERE fact_id=? AND version=1",
                (json.dumps(snap, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")), fact_id))


class TestVerifyEndpoint(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = VerifyFixture(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _verify(self, body=None, principal="mentor"):
        return self.fx.client.post("/v8/facts/verify",
                                   json=body if body is not None else {},
                                   headers=self.fx.headers(principal))

    def test_sealed_fact_reports_ok(self):
        fid = self.fx.seed("完好事实")
        self.fx.bump(fid, "完好事实 v2")
        r = self._verify({"fact_ids": [fid]})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["verified"], 1)
        self.assertEqual(data["broken"], [])

    def test_tampered_fact_reports_chain_broken(self):
        """破链判语：HTTP 200 但 status=chain_broken，broken 指到事实与版本。"""
        fid = self.fx.seed("将被篡改的事实")
        self.fx.bump(fid, "第二版")
        self.fx.tamper_v1(fid)
        r = self._verify({"fact_ids": [fid]})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "chain_broken")
        broken_ids = {b["fact_id"] for b in data["broken"]}
        self.assertIn(fid, broken_ids)
        entry = next(b for b in data["broken"] if b["fact_id"] == fid)
        self.assertIn(2, entry["breaks"])

    def test_legacy_unsealed_is_not_ok(self):
        """1.1.0 前的单版本事实（无封存链接）→ unsealed，不得并入完好。"""
        fid = self.fx.seed("旧时代事实")  # 仅 v1，链未封存
        r = self._verify({"fact_ids": [fid]})
        data = r.json()
        self.assertEqual(data["status"], "unsealed")
        self.assertIn(fid, data["unsealed"])

    def test_full_scan_requires_admin(self):
        fid = self.fx.seed("全库扫描对象")
        self.fx.bump(fid, "v2")
        r = self._verify({}, principal="mentor")
        self.assertEqual(r.status_code, 403)
        r_admin = self._verify({}, principal="admin")
        self.assertEqual(r_admin.status_code, 200)
        self.assertEqual(r_admin.json()["verified"], 1)
        self.assertTrue(r_admin.json()["chain_head_hash"])

    def test_per_fact_acl_denied_for_foreign_owner_only(self):
        """单条 verify 走 can_read：他人 owner_only 事实 403。"""
        fid = self.fx.seed("jarvis 的私密事实", owner="jarvis",
                           visibility="owner_only")
        self.fx.bump(fid, "v2")
        r = self._verify({"fact_ids": [fid]}, principal="mentor")
        self.assertEqual(r.status_code, 403)

    def test_empty_store_is_not_green(self):
        """空库：status=empty，不得输出「完好」。"""
        r = self._verify({}, principal="admin")
        data = r.json()
        self.assertEqual(data["status"], "empty")
        self.assertEqual(data["verified"], 0)

    def test_chain_head_hash_is_deterministic_anchor(self):
        """同库两次全扫 chain_head_hash 全等（跨次对账锚的最低要求）。"""
        fid = self.fx.seed("锚定事实")
        self.fx.bump(fid, "v2")
        h1 = self._verify({}, principal="admin").json()["chain_head_hash"]
        h2 = self._verify({}, principal="admin").json()["chain_head_hash"]
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)


if __name__ == "__main__":
    unittest.main()
