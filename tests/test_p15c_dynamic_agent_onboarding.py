# -*- coding: utf-8 -*-
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import validate_agent_id, get_registered_agents, register_agent
from fastapi.testclient import TestClient
from mimir_v8.api import create_app, ServiceContext
from mimir_v8.store import CanonicalStore
from mimir_v8.query import QueryKernel
from mimir_v8.auth import TokenStore
import tempfile
import json
import hashlib


class TestDynamicAgentOnboarding(unittest.TestCase):
    def test_dynamic_agent_registration(self):
        agent_name = "codex-subagent-99"
        self.assertNotIn(agent_name, get_registered_agents())
        register_agent(agent_name)
        self.assertIn(agent_name, get_registered_agents())
        self.assertEqual(validate_agent_id(agent_name), agent_name)

    def test_api_header_auto_registers_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CanonicalStore(root / "canon.db")
            token_path = root / "tokens.json"
            token_path.write_text(json.dumps({
                "principals": [{
                    "id": "system_caller",
                    "token_sha256": hashlib.sha256("test-token".encode()).hexdigest(),
                    "scopes": ["read", "write", "ingest"],
                    "roles": ["authenticated"],
                    "admin": False,
                }]
            }), encoding="utf-8")
            ctx = ServiceContext(store=store, token_store=TokenStore(token_path), query=QueryKernel(store))
            client = TestClient(create_app(ctx))

            # 携带 X-Mimir-Agent 访问
            r = client.get("/v8/health", headers={
                "Authorization": "Bearer test-token",
                "X-Mimir-Agent": "claude-code-instance-1"
            })
            self.assertEqual(r.status_code, 200)
            self.assertIn("claude-code-instance-1", get_registered_agents())


if __name__ == "__main__":
    unittest.main()
