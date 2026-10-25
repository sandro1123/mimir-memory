# -*- coding: utf-8 -*-
import unittest
import sys
import tempfile
import json
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from mimir_v8.api import create_app, ServiceContext
from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact, TombstoneFact
from mimir_v8.query import QueryKernel
from mimir_v8.auth import TokenStore


class TestBootstrapAPI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canon.db")
        token_path = root / "tokens.json"
        token_path.write_text(json.dumps({
            "principals": [{
                "id": "mentor",
                "token_sha256": hashlib.sha256("tok-bootstrap".encode()).hexdigest(),
                "scopes": ["read"],
                "roles": ["authenticated"],
                "admin": False,
            }]
        }), encoding="utf-8")
        self.ctx = ServiceContext(store=self.store, token_store=TokenStore(token_path), query=QueryKernel(self.store))
        self.client = TestClient(create_app(self.ctx))

        # 种一条 L0 铁律和一条偏好
        self.store.create_fact(CreateFact(
            content="铁律：禁止向任何第三方服务泄露生产数据库凭据",
            summary="禁止泄密", owner_principal="mentor",
            domain="system", fact_type="iron_rule",
            visibility="all", sensitivity="internal", egress_policy="local_only",
            human_status="confirmed"
        ), actor_principal="mentor")

        # These records must not leak through bootstrap to a different principal.
        self.store.create_fact(CreateFact(
            content="PRIVATE OWNER-ONLY RULE", summary="private rule",
            owner_principal="quantmaster", domain="personal", fact_type="iron_rule",
            visibility="owner_only", sensitivity="internal", egress_policy="local_only",
            human_status="confirmed",
        ), actor_principal="quantmaster")
        self.store.create_fact(CreateFact(
            content="RESTRICTED PRIVATE PREFERENCE", summary="private preference",
            owner_principal="quantmaster", domain="personal", fact_type="user_pref",
            visibility="owner_only", sensitivity="restricted", egress_policy="local_only",
            human_status="confirmed",
        ), actor_principal="quantmaster")
        deleted = self.store.create_fact(CreateFact(
            content="DELETED FACT MUST NOT APPEAR", summary="deleted",
            owner_principal="mentor", domain="quant", fact_type="user_pref",
            visibility="all", sensitivity="internal", egress_policy="local_only",
            human_status="confirmed",
        ), actor_principal="mentor")
        self.store.tombstone_fact(
            TombstoneFact(fact_id=deleted["fact_id"], expected_version=1, reason="test"),
            actor_principal="mentor",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_bootstrap_returns_structured_overview(self):
        r = self.client.get("/v15/bootstrap", headers={"Authorization": "Bearer tok-bootstrap"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("system_version", data)
        self.assertIn("iron_rules", data)
        self.assertIn("user_prefs", data)
        self.assertIn("domain_counts", data)
        self.assertTrue(any("禁止向任何第三方" in rule["content"] for rule in data["iron_rules"]))
        self.assertFalse(any("PRIVATE OWNER-ONLY" in rule["content"] for rule in data["iron_rules"]))
        self.assertFalse(any("RESTRICTED PRIVATE" in pref["content"] for pref in data["user_prefs"]))
        self.assertFalse(any("DELETED FACT" in pref["content"] for pref in data["user_prefs"]))
        self.assertNotIn("personal", data["domain_counts"])
        self.assertNotIn("quant", data["domain_counts"])


if __name__ == "__main__":
    unittest.main()
