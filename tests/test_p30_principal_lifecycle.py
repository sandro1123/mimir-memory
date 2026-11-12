# -*- coding: utf-8 -*-
"""Principal lifecycle: onboarding a new agent and retiring one.

Background (issue #30): the token registry is the only thing that decides who
may call the API, but it had no signing entry point — a new agent meant hand
-editing JSON and recomputing sha256. Agent *names* had a runtime register_agent()
but it was process-local memory only, so a name learned by the API process was
invisible to the worker and forgotten on restart.

These tests pin the two halves that were missing:
  1. mint/revoke a principal into the token registry (atomic, hash-only)
  2. persist the agent-name registry so every process sees the same roster
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8 import schema as schema_mod
from mimir_v8.auth import AuthError, TokenStore, mint_principal, revoke_principal


class TokenLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "api_tokens.json"
        self.path.write_text(
            json.dumps({
                "version": 1,
                "principals": [{
                    "id": "admin",
                    "token_sha256": "0" * 64,
                    "scopes": ["read", "admin"],
                    "admin": True,
                }],
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_mint_returns_plaintext_once_and_stores_only_hash(self):
        token, record = mint_principal(
            self.path, principal_id="thor", scopes=["read", "write"]
        )
        self.assertTrue(token)
        self.assertEqual(record["id"], "thor")
        self.assertEqual(record["scopes"], ["read", "write"])
        self.assertNotIn(token, self.path.read_text(encoding="utf-8"))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        ids = [p["id"] for p in stored["principals"]]
        self.assertEqual(ids, ["admin", "thor"])

    def test_minted_token_authenticates(self):
        token, _ = mint_principal(self.path, principal_id="thor", scopes=["read"])
        principal = TokenStore(self.path).authenticate(f"Bearer {token}")
        self.assertEqual(principal.principal_id, "thor")
        self.assertEqual(principal.scopes, frozenset({"read"}))

    def test_duplicate_principal_id_is_rejected(self):
        mint_principal(self.path, principal_id="thor", scopes=["read"])
        with self.assertRaises(AuthError):
            mint_principal(self.path, principal_id="thor", scopes=["read"])

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(AuthError):
            mint_principal(self.path, principal_id="thor", scopes=["teleport"])

    def test_revoke_removes_principal_and_invalidates_token(self):
        token, _ = mint_principal(self.path, principal_id="thor", scopes=["read"])
        revoke_principal(self.path, principal_id="thor")
        with self.assertRaises(AuthError):
            TokenStore(self.path).authenticate(f"Bearer {token}")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in stored["principals"]], ["admin"])

    def test_revoke_last_principal_is_refused(self):
        with self.assertRaises(AuthError):
            revoke_principal(self.path, principal_id="admin")

    def test_revoke_missing_principal_is_a_noop_error(self):
        with self.assertRaises(AuthError):
            revoke_principal(self.path, principal_id="nobody")


class AgentRegistryPersistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self._tmp.name) / "agents.json"
        self._saved = schema_mod.AGENT_REGISTRY_FILE
        # Point the module-level registry file at our temp path for this test.
        schema_mod.AGENT_REGISTRY_FILE = self.registry
        schema_mod.reset_dynamic_registry()

    def tearDown(self):
        schema_mod.AGENT_REGISTRY_FILE = self._saved
        schema_mod.reset_dynamic_registry()
        self._tmp.cleanup()

    def test_register_agent_survives_registry_reset(self):
        schema_mod.register_agent("thor")
        self.assertIn("thor", schema_mod.get_registered_agents())

        # Simulate a process restart: in-memory set is cleared, the file stays.
        schema_mod.reset_dynamic_registry()
        self.assertNotIn("thor", schema_mod.get_registered_agents())

        schema_mod.load_dynamic_registry()
        self.assertIn("thor", schema_mod.get_registered_agents())

    def test_retire_agent_removes_it_from_the_file(self):
        schema_mod.register_agent("thor")
        schema_mod.retire_agent("thor")
        schema_mod.reset_dynamic_registry()
        schema_mod.load_dynamic_registry()
        self.assertNotIn("thor", schema_mod.get_registered_agents())

    def test_default_roster_is_never_removable(self):
        with self.assertRaises(Exception):
            schema_mod.retire_agent("mentor")
        self.assertIn("mentor", schema_mod.get_registered_agents())


if __name__ == "__main__":
    unittest.main()
