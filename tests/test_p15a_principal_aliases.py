# -*- coding: utf-8 -*-
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.auth import Principal, PRINCIPAL_ALIASES
from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact


class TestPrincipalAliases(unittest.TestCase):
    def test_alias_mapping_defined(self):
        self.assertIn("heimdallr", PRINCIPAL_ALIASES)
        self.assertIn("mentor", PRINCIPAL_ALIASES["heimdallr"])

    def test_can_act_as_resolves_alias(self):
        p = Principal(principal_id="heimdallr", scopes=frozenset({"read", "write"}))
        self.assertTrue(p.can_act_as("mentor"), "heimdallr 必须能代表 mentor")
        self.assertFalse(p.can_act_as("jarvis"), "未授权的别名不能代表")

    def test_heimdallr_reads_mentor_owner_only_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            fid = store.create_fact(CreateFact(
                content="mentor 私有记忆：技术选型经验",
                summary="选型", owner_principal="mentor",
                domain="knowledge", fact_type="reference",
                visibility="owner_only", sensitivity="internal",
                egress_policy="local_only", human_status="confirmed"
            ), actor_principal="mentor")["fact_id"]

            self.assertTrue(store.can_read(fid, "mentor"))
            self.assertTrue(store.can_read(fid, "heimdallr"), "heimdallr 必须能读取 mentor 的 owner_only 事实")
            self.assertFalse(store.can_read(fid, "quantmaster"), "其他 Agent 不能读取 mentor 的 owner_only 事实")


if __name__ == "__main__":
    unittest.main()
