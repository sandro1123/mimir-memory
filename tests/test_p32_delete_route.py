# -*- coding: utf-8 -*-
"""Issue #32: a standard ``DELETE /v8/facts/{id}`` must reach the tombstone path.

Clients try ``DELETE`` first; without the route they got 405 and had to guess
that tombstoning was a ``POST``. The RESTful route is only a bridge — the
semantics stay soft-delete with version and audit guarantees.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.knowledge import FeedbackLoop, KnowledgeService, UnifiedSearch
from mimir_v8.query import QueryKernel
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class DeleteFixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        self.knowledge = KnowledgeService(self.store)
        self.unified = UnifiedSearch(self.query, self.knowledge)
        self.feedback = FeedbackLoop(self.store, self.knowledge)
        self.tokens = {
            "mentor": "del-mentor-token",
            "other": "del-other-token",
            "admin": "del-admin-token",
        }
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": principal,
                        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "scopes": ["read", "write", "delete"],
                        "roles": ["admin"] if principal == "admin" else [],
                        "admin": principal == "admin",
                    }
                    for principal, token in self.tokens.items()
                ]
            }),
            encoding="utf-8",
        )
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(token_path),
            query=self.query,
            knowledge=self.knowledge,
            unified_search=self.unified,
            feedback_loop=self.feedback,
        )
        self.client = TestClient(create_app(context), raise_server_exceptions=False)

    def headers(self, principal="mentor"):
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    def make_fact(self, owner="mentor", key="delete-route"):
        out = self.store.create_fact(
            CreateFact(content="stale memory", owner_principal=owner,
                       domain="system", fact_type="event", summary="stale",
                       idempotency_key=key),
            actor_principal="admin",
        )
        return out["fact_id"], out["version"]


class TestDeleteRoute(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.fixture = DeleteFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _delete(self, fact_id, version, *, principal="mentor", reason="superseded",
                key=None):
        return self.fixture.client.delete(
            f"/v8/facts/{fact_id}",
            params={"expected_version": version, "reason": reason,
                    **({"idempotency_key": key} if key else {})},
            headers=self.fixture.headers(principal),
        )

    def test_delete_tombstones_the_fact(self):
        fact_id, version = self.fixture.make_fact()
        response = self._delete(fact_id, version)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["status"], "tombstoned")
        self.assertEqual(self.fixture.store.get_fact(fact_id)["status"], "tombstoned")

    def test_delete_requires_the_current_version(self):
        fact_id, version = self.fixture.make_fact()
        response = self._delete(fact_id, version + 5)
        self.assertEqual(response.status_code, 409, response.text)

    def test_delete_reason_is_required(self):
        fact_id, version = self.fixture.make_fact()
        response = self.fixture.client.delete(
            f"/v8/facts/{fact_id}",
            params={"expected_version": version},
            headers=self.fixture.headers(),
        )
        self.assertEqual(response.status_code, 422)

    def test_other_principal_cannot_delete(self):
        fact_id, version = self.fixture.make_fact(owner="mentor")
        response = self._delete(fact_id, version, principal="other")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.fixture.store.get_fact(fact_id)["status"], "active")

    def test_admin_can_delete_across_principals(self):
        fact_id, version = self.fixture.make_fact(owner="mentor")
        response = self._delete(fact_id, version, principal="admin")
        self.assertEqual(response.status_code, 200, response.text)

    def test_delete_is_idempotent_with_a_key(self):
        fact_id, version = self.fixture.make_fact()
        first = self._delete(fact_id, version, key="del-1")
        second = self._delete(fact_id, version, key="del-1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["status"], second.json()["status"])

    def test_tombstoned_fact_can_still_be_restored(self):
        fact_id, version = self.fixture.make_fact()
        self.assertEqual(self._delete(fact_id, version).status_code, 200)
        restored = self.fixture.client.post(
            f"/v8/facts/{fact_id}/restore",
            json={"reason": "changed my mind"},
            headers=self.fixture.headers(),
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.fixture.store.get_fact(fact_id)["status"], "active")


if __name__ == "__main__":
    unittest.main()
