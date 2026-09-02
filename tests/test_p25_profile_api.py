"""Mímir v12.2.0 — Unified Profile View API (spec 阶段二任务2).

GET /v12/profile?agent_id=... 一键返回该 agent 的解构画像：
{ iron_rules: [...], user_prefs: [...], dynamic_context: [...] }

- iron_rules / user_prefs 直取该 owner 的活跃 iron_rule / user_pref 事实
  （与锚通道同一「安全底线永不丢」语义：画像装配不依赖相似度）
- dynamic_context = 其余活跃事实的近期快照（默认 20 条，可调）
- ACL：owner-only 视图 — 调用者必须 can_act_as(agent_id)（admin 直通）
- 单一事实源：从 canonical facts 表装配，零新表零迁移

TDD RED → GREEN.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.query import QueryKernel
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore

import hashlib
import json


class ProfileFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        tokens = {
            "mentor": "p25-mentor-token",
            "jarvis": "p25-jarvis-token",
            "admin": "p25-admin-token",
        }
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": principal,
                        "token_sha256": hashlib.sha256(
                            token.encode("utf-8")).hexdigest(),
                        "scopes": ["read", "write", "ingest"],
                        "roles": ["admin"] if principal == "admin" else [],
                        "admin": principal == "admin",
                    }
                    for principal, token in tokens.items()
                ]
            }),
            encoding="utf-8",
        )
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(token_path),
            query=self.query,
        )
        self.client = TestClient(create_app(context),
                                 raise_server_exceptions=False)
        self.tokens = tokens

    def headers(self, principal="mentor"):
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    def seed(self, content, *, fact_type="event", owner="mentor",
             visibility="all", domain="knowledge"):
        result = self.store.create_fact(
            CreateFact(
                content=content,
                summary=content[:40],
                owner_principal=owner,
                domain=domain,
                fact_type=fact_type,
                visibility=visibility,
                sensitivity="internal",
                egress_policy="local_only",
                human_status="confirmed",
                confidence_score=0.5,
            ),
            actor_principal=owner,
        )
        return result["fact_id"]


class TestProfileAPI(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ProfileFixture(Path(self._tmp.name))
        self.iron_a = self.fx.seed("生产库只允许经 API 写入", fact_type="iron_rule")
        self.iron_b = self.fx.seed("禁止直连 SQLite 改数据", fact_type="iron_rule")
        self.pref_a = self.fx.seed("报告一律用中文", fact_type="user_pref")
        self.evt_a = self.fx.seed("昨夜节点漂移已修复", fact_type="event")
        self.evt_b = self.fx.seed("备份窗口改到 23:00", fact_type="event")

    def tearDown(self):
        self._tmp.cleanup()

    def _get(self, agent="mentor", principal="mentor", **params):
        return self.fx.client.get(
            "/v12/profile",
            params={"agent_id": agent, **params},
            headers=self.fx.headers(principal),
        )

    def test_returns_deconstructed_profile_shape(self):
        r = self._get()
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        for key in ("iron_rules", "user_prefs", "dynamic_context"):
            self.assertIn(key, data)
        iron_ids = {item["fact_id"] for item in data["iron_rules"]}
        self.assertEqual(iron_ids, {self.iron_a, self.iron_b})
        pref_ids = {item["fact_id"] for item in data["user_prefs"]}
        self.assertEqual(pref_ids, {self.pref_a})
        ctx_ids = {item["fact_id"] for item in data["dynamic_context"]}
        self.assertEqual(ctx_ids, {self.evt_a, self.evt_b})

    def test_items_carry_content_and_metadata(self):
        r = self._get()
        data = r.json()
        item = data["iron_rules"][0]
        for key in ("fact_id", "content", "summary", "domain", "updated_at"):
            self.assertIn(key, item)

    def test_owner_only_view_enforces_act_as(self):
        # jarvis 不能拉 mentor 的画像
        r = self._get(agent="mentor", principal="jarvis")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["error"]["code"], "owner_boundary")

    def test_admin_can_read_any_profile(self):
        r = self._get(agent="mentor", principal="admin")
        self.assertEqual(r.status_code, 200, r.text)

    def test_requires_authentication(self):
        r = self.fx.client.get("/v12/profile",
                              params={"agent_id": "mentor"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_unknown_agent_returns_empty_profile(self):
        r = self._get(agent="quantmaster", principal="mentor")
        self.assertEqual(r.status_code, 403, r.text)  # mentor 不能扮演 quantmaster

    def test_admin_unknown_agent_gets_empty_sections(self):
        r = self._get(agent="quantmaster", principal="admin")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["iron_rules"], [])
        self.assertEqual(data["user_prefs"], [])
        self.assertEqual(data["dynamic_context"], [])
        self.assertEqual(data["agent_id"], "quantmaster")

    def test_owner_filter_isolates_other_agents_facts(self):
        # jarvis 自己的画像不含 mentor 的铁律
        self.fx.seed("jarvis 私有铁律：先查记忆再动手",
                     fact_type="iron_rule", owner="jarvis")
        r = self._get(agent="jarvis", principal="admin")
        data = r.json()
        iron_contents = [i["content"] for i in data["iron_rules"]]
        self.assertEqual(len(iron_contents), 1)
        self.assertIn("jarvis 私有铁律", iron_contents[0])

    def test_tombstoned_iron_excluded(self):
        from mimir_v8.store import TombstoneFact
        fact = self.fx.store.get_fact(self.iron_a)
        self.fx.store.tombstone_fact(
            TombstoneFact(fact_id=self.iron_a,
                          expected_version=fact["current_version"],
                          reason="superseded",
                          idempotency_key="p25-tomb-1"),
            actor_principal="mentor",
        )
        r = self._get()
        data = r.json()
        iron_ids = {item["fact_id"] for item in data["iron_rules"]}
        self.assertNotIn(self.iron_a, iron_ids)
        self.assertIn(self.iron_b, iron_ids)

    def test_dynamic_context_limit_param(self):
        for i in range(30):
            self.fx.seed(f"历史事件流水 {i}", fact_type="event")
        r = self._get()
        data = r.json()
        self.assertEqual(len(data["dynamic_context"]), 20)
        r2 = self._get(dynamic_context_limit=5)
        self.assertEqual(len(r2.json()["dynamic_context"]), 5)

    def test_dynamic_context_limit_validated(self):
        r = self.fx.client.get(
            "/v12/profile",
            params={"agent_id": "mentor", "dynamic_context_limit": 500},
            headers=self.fx.headers(),
        )
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main()
