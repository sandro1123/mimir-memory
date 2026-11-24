# -*- coding: utf-8 -*-
"""Principal aliases vs. read-only team sharing (1.9.0, docs/ops/ROLES.md).

- PRINCIPAL_ALIASES = identity equivalence (can_act_as). Only the legacy
  heimdallr -> mentor row remains.
- TEAM_READ_SHARE = visibility only. thor / odin / claudecode can READ each
  other's and mentor's non-restricted facts, but can never write, delete,
  review or act as one another.
"""
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.auth import Principal, PRINCIPAL_ALIASES, TEAM_READ_SHARE
from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact, register_agent, reset_dynamic_registry


def _fact(store, owner, sensitivity="internal"):
    return store.create_fact(CreateFact(
        content=f"{owner} 偏好记录 {sensitivity}",
        summary="偏好", owner_principal=owner,
        domain="personal", fact_type="user_pref",
        visibility="owner_only", sensitivity=sensitivity,
        egress_policy="local_only", human_status="confirmed"
    ), actor_principal=owner)["fact_id"]


class TestPrincipalAliases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Production learns these ids at mint / first request; tests start
        # from the built-in roster, so register them explicitly.
        for agent in ("thor", "odin", "claudecode"):
            register_agent(agent)

    @classmethod
    def tearDownClass(cls):
        reset_dynamic_registry()

    def test_alias_mapping_defined(self):
        self.assertIn("mentor", PRINCIPAL_ALIASES["heimdallr"])
        for agent in ("thor", "odin", "claudecode"):
            self.assertNotIn(agent, PRINCIPAL_ALIASES, f"{agent} 不得拥有身份等价别名")
            self.assertIn(agent, TEAM_READ_SHARE)
            self.assertIn("mentor", TEAM_READ_SHARE[agent])

    def test_can_act_as_no_impersonation(self):
        p = Principal(principal_id="heimdallr", scopes=frozenset({"read", "write"}))
        self.assertTrue(p.can_act_as("mentor"), "heimdallr 必须能代表 mentor")
        self.assertFalse(p.can_act_as("jarvis"), "未授权的别名不能代表")
        thor = Principal(principal_id="thor", scopes=frozenset({"read", "ingest"}))
        self.assertTrue(thor.can_act_as("thor"))
        for other in ("claudecode", "odin", "mentor"):
            self.assertFalse(thor.can_act_as(other), f"thor 不得冒充 {other}")
        odin = Principal(principal_id="odin", scopes=frozenset({"read", "ingest"}))
        self.assertFalse(odin.can_act_as("thor"))

    def test_team_reads_mentor_and_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            mentor_fid = _fact(store, "mentor")
            thor_fid = _fact(store, "thor")
            for reader in ("thor", "odin", "claudecode"):
                self.assertTrue(store.can_read(mentor_fid, reader), f"{reader} 必须能读 mentor")
            self.assertTrue(store.can_read(thor_fid, "odin"))
            self.assertTrue(store.can_read(thor_fid, "claudecode"))
            self.assertFalse(store.can_read(thor_fid, "quantmaster"), "团队外不可读")

    def test_team_share_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            thor_fid = _fact(store, "thor")
            for perm in ("write", "delete", "review"):
                self.assertFalse(store.can_access(thor_fid, "odin", perm), f"odin 不得 {perm} thor 的事实")
                self.assertFalse(store.can_access(thor_fid, "claudecode", perm))
            self.assertTrue(store.can_access(thor_fid, "thor", "write"), "owner 自己仍可写")

    def test_restricted_not_team_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            fid = _fact(store, "mentor", sensitivity="restricted")
            self.assertFalse(store.can_read(fid, "thor"), "restricted 不随团队共享外泄")

    def test_heimdallr_reads_mentor_owner_only_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            fid = _fact(store, "mentor")
            self.assertTrue(store.can_read(fid, "mentor"))
            self.assertTrue(store.can_read(fid, "heimdallr"), "heimdallr 必须能读取 mentor 的 owner_only 事实")
            self.assertFalse(store.can_read(fid, "quantmaster"), "其他 Agent 不能读取 mentor 的 owner_only 事实")


if __name__ == "__main__":
    unittest.main()
