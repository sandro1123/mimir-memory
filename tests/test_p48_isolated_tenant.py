# -*- coding: utf-8 -*-
"""Jarvis 隔离租户（2026-11-27）。

ISOLATED_PRINCIPALS 里的身份与机群双向隔离：技术域共享、TEAM_READ_SHARE、
role 授权都不能跨越边界；只有本人、显式 principal 授权或 admin 可读。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact, register_agent, reset_dynamic_registry


def _fact(store, owner, domain="infrastructure", fact_type="reference", content=None):
    return store.create_fact(CreateFact(
        content=content or f"{owner} 的通用技术参考：Docker 内存限额配置",
        summary="tech ref", owner_principal=owner,
        domain=domain, fact_type=fact_type,
        visibility="owner_only", sensitivity="internal",
        egress_policy="local_only", human_status="confirmed",
    ), actor_principal=owner)["fact_id"]


class TestIsolatedTenant(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canon.db")
        register_agent("odin")

    def tearDown(self):
        reset_dynamic_registry()
        self._tmp.cleanup()

    def test_fleet_tech_reference_not_readable_by_jarvis(self):
        fid = _fact(self.store, "mentor")
        self.assertTrue(self.store.can_read(fid, "thor"), "机群内技术共享保持不变")
        self.assertFalse(self.store.can_read(fid, "jarvis"))
        self.assertFalse(self.store.can_read(fid, "jarvis", roles={"authenticated"}))

    def test_jarvis_tech_reference_not_readable_by_fleet(self):
        fid = _fact(self.store, "jarvis")
        for reader in ("mentor", "thor", "odin", "heimdallr"):
            self.assertFalse(self.store.can_read(fid, reader), reader)

    def test_team_share_does_not_reach_jarvis(self):
        fid = _fact(self.store, "odin", domain="system", fact_type="project_config")
        self.assertTrue(self.store.can_read(fid, "thor"))
        self.assertFalse(self.store.can_read(fid, "jarvis"))

    def test_jarvis_reads_and_writes_own_facts(self):
        fid = _fact(self.store, "jarvis", domain="personal", fact_type="user_pref")
        self.assertTrue(self.store.can_read(fid, "jarvis"))
        self.assertTrue(self.store.can_access(fid, "jarvis", "write"))

    def test_admin_still_reads_across(self):
        fid = _fact(self.store, "jarvis")
        self.assertTrue(self.store.can_read(fid, "odin", is_admin=True))

    def test_role_grant_does_not_cross_boundary(self):
        fid = _fact(self.store, "mentor", domain="system", fact_type="project_config")
        self._grant(fid, "role", "authenticated", "allow")
        self.assertTrue(self.store.can_read(fid, "thor"))
        self.assertFalse(self.store.can_read(fid, "jarvis"))

    def test_explicit_principal_grant_crosses_boundary(self):
        fid = _fact(self.store, "mentor", domain="system", fact_type="project_config")
        self._grant(fid, "principal", "jarvis", "allow")
        self.assertTrue(self.store.can_read(fid, "jarvis"))

    def _grant(self, fact_id, subject_type, subject_id, effect):
        from mimir_v8.schema import GrantFactAccess
        self.store.grant_fact_access(
            GrantFactAccess(fact_id=fact_id, subject_type=subject_type,
                            subject_id=subject_id, permission="read", effect=effect),
            actor_principal="mentor",
        )


if __name__ == "__main__":
    unittest.main()
