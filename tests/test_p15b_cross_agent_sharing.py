# -*- coding: utf-8 -*-
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact
from mimir_v8.query import QueryKernel, QueryRequest


class TestCrossAgentSharing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CanonicalStore(Path(self._tmp.name) / "canon.db")
        self.q = QueryKernel(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_technical_reference_is_readable_across_authenticated_agents(self):
        fid = self.store.create_fact(CreateFact(
            content="通用技术配置：Docker 内存限额配置方案",
            summary="Docker配置", owner_principal="mentor",
            domain="infrastructure", fact_type="reference",
            visibility="owner_only", sensitivity="internal",
            egress_policy="local_only", human_status="confirmed"
        ), actor_principal="mentor")["fact_id"]

        # claude-code 是另一个 agent，未配特定 grant
        self.assertTrue(self.store.can_read(fid, "claude-code"),
                        "技术类参考事实应允许同机已认证 agent 读取")

    def test_personal_domain_remains_strictly_owner_only(self):
        fid = self.store.create_fact(CreateFact(
            content="用户个人私密偏好：健康与私人日程安排",
            summary="隐私", owner_principal="mentor",
            domain="personal", fact_type="user_pref",
            visibility="owner_only", sensitivity="internal",
            egress_policy="local_only", human_status="confirmed"
        ), actor_principal="mentor")["fact_id"]

        self.assertFalse(self.store.can_read(fid, "claude-code"),
                         "personal 域必须严格保持私有，严禁跨 agent 穿透")

    def test_restricted_sensitivity_never_leaks(self):
        fid = self.store.create_fact(CreateFact(
            content="核心凭据信息：Token与内部密钥",
            summary="密钥", owner_principal="mentor",
            domain="infrastructure", fact_type="reference",
            visibility="owner_only", sensitivity="restricted",
            egress_policy="local_only", human_status="confirmed"
        ), actor_principal="mentor")["fact_id"]

        self.assertFalse(self.store.can_read(fid, "claude-code"),
                         "restricted 敏感度事实绝不泄露")


if __name__ == "__main__":
    unittest.main()
