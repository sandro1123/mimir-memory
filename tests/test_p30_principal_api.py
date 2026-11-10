# -*- coding: utf-8 -*-
"""#30: API surface for onboarding / retiring an agent.

The CLI path is covered in test_p30_principal_lifecycle; these tests pin the
HTTP path an operator (or dashboard) actually uses, and the owner-boundary
behaviour a newly minted agent then relies on.
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
from mimir_v8.learning import LearningService
from mimir_v8.query import QueryKernel
from mimir_v8.schema import reset_dynamic_registry
from mimir_v8.store import CanonicalStore


class PrincipalApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canon.db")
        self.token_path = root / "api_tokens.json"
        self.token_path.write_text(json.dumps({
            "version": 1,
            "principals": [{
                "id": "admin",
                "token_sha256": hashlib.sha256(b"admin-token").hexdigest(),
                "scopes": ["read", "write", "admin"],
                "admin": True,
            }],
        }), encoding="utf-8")
        self.context = ServiceContext(
            store=self.store,
            token_store=TokenStore(self.token_path),
            query=QueryKernel(self.store),
            learning=LearningService(self.store),
        )
        self.client = TestClient(create_app(self.context))
        self.admin = {"Authorization": "Bearer admin-token"}

    def tearDown(self):
        reset_dynamic_registry()
        self._tmp.cleanup()

    def test_mint_returns_plaintext_token_that_works(self):
        r = self.client.post(
            "/v8/principals/register",
            json={"principal_id": "thor", "scopes": ["read", "write"]},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        token = body["token"]
        self.assertTrue(token)
        self.assertEqual(body["principal"]["id"], "thor")
        # Only the hash is persisted.
        self.assertNotIn(token, self.token_path.read_text(encoding="utf-8"))

        # The new principal can now read.
        r2 = self.client.get(
            "/v8/principals", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(r2.status_code, 403)  # minted without admin scope

    def test_mint_requires_admin(self):
        self.token_path.write_text(json.dumps({
            "version": 1,
            "principals": [{
                "id": "reader",
                "token_sha256": hashlib.sha256(b"reader-token").hexdigest(),
                "scopes": ["read"],
                "admin": False,
            }],
        }), encoding="utf-8")
        r = self.client.post(
            "/v8/principals/register",
            json={"principal_id": "thor"},
            headers={"Authorization": "Bearer reader-token"},
        )
        self.assertEqual(r.status_code, 403)

    def test_revoked_token_stops_working(self):
        token = self.client.post(
            "/v8/principals/register",
            json={"principal_id": "thor", "scopes": ["read"]},
            headers=self.admin,
        ).json()["token"]
        r = self.client.post(
            "/v8/principals/revoke",
            json={"principal_id": "thor"},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200, r.text)
        r2 = self.client.get(
            "/v8/principals", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(r2.status_code, 401)

    def test_minted_agent_can_remember_for_itself(self):
        token = self.client.post(
            "/v8/principals/register",
            json={"principal_id": "thor", "scopes": ["read", "write", "review"]},
            headers=self.admin,
        ).json()["token"]
        # The name is registered, so owner_principal validation accepts it.
        r = self.client.post(
            "/v8/learning/remember",
            json={
                "content": "thor learned something",
                "owner_principal": "thor",
                "domain": "knowledge",
                "fact_type": "event",
                "idempotency_key": "thor-1",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 201, r.text)


if __name__ == "__main__":
    unittest.main()
