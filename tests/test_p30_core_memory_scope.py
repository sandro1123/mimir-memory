# -*- coding: utf-8 -*-
"""Every scope the API checks must also be mintable (issue #73).

``/v8/core-memory/promotions`` and ``/v8/core-memory/items/{id}/retire`` are
guarded by ``scoped("core_memory")``, but ``KNOWN_SCOPES`` did not list it, so
``mint_principal`` rejected any request asking for it. The two routes degraded
to admin-only and the "KNOWN_SCOPES is the real check surface" invariant was
broken.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.auth import (
    AuthError,
    KNOWN_SCOPES,
    Principal,
    TokenStore,
    mint_principal,
)


class TestCoreMemoryScope(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.registry = Path(self._tmp.name) / "tokens.json"
        self.registry.write_text(
            json.dumps({"version": 1, "principals": []}), encoding="utf-8"
        )

    def test_scope_is_declared(self):
        self.assertIn("core_memory", KNOWN_SCOPES)

    def test_mint_accepts_core_memory(self):
        token, record = mint_principal(
            self.registry, principal_id="cursor", scopes=["read", "core_memory"]
        )
        self.assertIsInstance(token, str)
        self.assertIn("core_memory", record["scopes"])

    def test_unknown_scope_still_fails_closed(self):
        # Guard against widening the set into "anything goes": a typo must 400.
        with self.assertRaises(AuthError) as raised:
            mint_principal(
                self.registry, principal_id="cursor", scopes=["core-memory"]
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_minted_token_can_satisfy_the_scope(self):
        token, _ = mint_principal(
            self.registry, principal_id="cursor", scopes=["read", "core_memory"]
        )
        principal = TokenStore(self.registry).authenticate(f"Bearer {token}")
        principal.require("core_memory")  # must not raise

    def test_principal_without_the_scope_is_denied(self):
        with self.assertRaises(AuthError) as raised:
            Principal(
                principal_id="developer", scopes=frozenset({"read"})
            ).require("core_memory")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.code, "missing_scope")


if __name__ == "__main__":
    unittest.main()
