"""Mímir v12.2.0 — Anchor Channel (spec 阶段二任务4).

铁律（iron_rule）与用户核心偏好（user_pref）免于被语义相似度阈值一票否决：
锚通道（anchor channel）在候选池构建阶段直接从 canonical 事实表注入
活跃的 iron_rule/user_pref 事实，不依赖 vector/fts/graph 三通道的相似度
命中。ACL 仲裁与状态过滤照常在 hydration 阶段执行——锚通道改变的是
「谁能进入候选池」，不是「谁能被读到」。

Red-line tests (TDD RED → GREEN):
- 查询词与铁律内容零词汇交集时，铁律仍出现在结果中（一票否决失效）
- user_pref 同享锚通道；event/pattern 等普通类型不进锚通道
- ACL 隔离不变：他 agent 的 owner_only 铁律不通过锚通道泄漏
- 停用（tombstoned/disputed）铁律不进锚通道
- 锚定结果带 channel 标记（可观测），RRF 融合不爆分
- trace() 报告 AnchorChannel 阶段（hits/注入数）
- 深度查询可关锚通道（use_anchor=False → 铁律不注入）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _make_fact(store, content, *, fact_type="event", owner="mentor",
               visibility="all", domain="knowledge", confidence=0.5):
    result = store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain=domain,
            fact_type=fact_type,
            visibility=visibility,
            sensitivity="internal",
            egress_policy="local_only",
            human_status="confirmed",
            confidence_score=confidence,
        ),
        actor_principal="mentor",
    )
    return result["fact_id"]


class _AnchorFixture:
    """Store + kernel with no vector/graph and an FTS projector whose
    index never matches the anchor facts (simulating similarity veto)."""

    def __init__(self, tmp):
        self.store = CanonicalStore(Path(tmp) / "canonical.db")
        self.fts = FTSProjector(Path(tmp) / "fts.db")
        self.runner = ProjectorRunner(self.store, self.fts)
        self.kernel = QueryKernel(self.store, fts=self.fts)

    def seed(self, content, **kw):
        return _make_fact(self.store, content, **kw)

    def project(self):
        self.runner.run_once(limit=500)

    def search(self, text, **kw):
        kw.pop("principal_id", None)  # consumed below
        return self.kernel.search(QueryRequest(
            text=text, principal_id="mentor",
            roles=(), is_admin=False,
            use_vector=False, use_fts=True, use_graph=False,
            include_provisional=False, **kw,
        ))


class TestAnchorChannel(unittest.TestCase):
    """核心断言：相似度零命中时铁律/偏好仍被锚入候选池。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = _AnchorFixture(self._tmp.name)
        # 铁律与查询词零词汇交集 — vector/fts 三通道均无法命中
        self.iron_id = self.fx.seed(
            "生产数据库只允许通过 Mímir API 写入，禁止直连 SQLite 修改",
            fact_type="iron_rule", owner="mentor",
        )
        self.pref_id = self.fx.seed(
            "用户偏好：所有报告输出使用中文",
            fact_type="user_pref", owner="mentor",
        )
        # 普通事件事实，可被 FTS 命中
        self.event_id = self.fx.seed(
            "Kubernetes 集群昨夜发生节点漂移事件",
            fact_type="event", owner="mentor",
        )
        self.fx.project()

    def tearDown(self):
        self._tmp.cleanup()

    def test_iron_rule_survives_zero_similarity_query(self):
        res = self.fx.search("Kubernetes 节点漂移")
        ids = [r["fact_id"] for r in res["results"]]
        self.assertIn(self.iron_id, ids,
                      "iron_rule must enter the pool even with zero lexical overlap")

    def test_user_pref_also_anchored(self):
        res = self.fx.search("Kubernetes 节点漂移")
        ids = [r["fact_id"] for r in res["results"]]
        self.assertIn(self.pref_id, ids)

    def test_ordinary_event_not_anchored_only_matched(self):
        # 事件类型必须仍然通过 FTS 命中（锚通道不注入普通类型）。
        # v12.2.0-1 起 event 属 L1：standard 档由装配层拦下（非锚注），
        # deep 档 FTS 命中后放行 — 两档都不允许 anchor 通道标记。
        res = self.fx.search("Kubernetes")
        ids = [r["fact_id"] for r in res["results"]]
        self.assertNotIn(self.event_id, ids)  # standard 档：L1 被装配层拦下
        deep = self.fx.search("Kubernetes", depth="deep")
        deep_ids = [r["fact_id"] for r in deep["results"]]
        self.assertIn(self.event_id, deep_ids)  # deep 档：FTS 命中下钻可见
        channels = {r["fact_id"]: r["score_explanation"]["channels"]
                    for r in deep["results"]}
        self.assertNotIn("anchor", channels.get(self.event_id, {}),
                         "event facts must not be reported as anchor-injected")

    def test_anchor_channel_marked_in_score_explanation(self):
        res = self.fx.search("Kubernetes 节点漂移")
        by_id = {r["fact_id"]: r for r in res["results"]}
        self.assertIn("anchor", by_id[self.iron_id]["score_explanation"]["channels"])
        self.assertIn("anchor", by_id[self.pref_id]["score_explanation"]["channels"])

    def test_use_anchor_false_disables_channel(self):
        res = self.fx.search("Kubernetes 节点漂移", use_anchor=False)
        ids = [r["fact_id"] for r in res["results"]]
        self.assertNotIn(self.iron_id, ids)
        self.assertNotIn(self.pref_id, ids)

    def test_acl_still_applies_to_anchored_facts(self):
        # 他 agent 的 owner_only 铁律不能通过锚通道泄漏给查询者
        other_iron = self.fx.seed(
            "quantmaster 私有铁律：下单前必须二次确认",
            fact_type="iron_rule", owner="quantmaster", visibility="owner_only",
        )
        self.fx.project()
        res = self.fx.search("Kubernetes 节点漂移", principal_id="jarvis")
        ids = [r["fact_id"] for r in res["results"]]
        self.assertNotIn(other_iron, ids,
                         "owner_only iron rules must not leak via the anchor channel")

    def test_tombstoned_iron_not_anchored(self):
        from mimir_v8.store import TombstoneFact
        fact = self.store_get(self.iron_id)
        self.fx.store.tombstone_fact(
            TombstoneFact(fact_id=self.iron_id,
                          expected_version=fact["current_version"],
                          reason="superseded", idempotency_key="anchor-test-1"),
            actor_principal="mentor",
        )
        self.fx.project()
        res = self.fx.search("Kubernetes 节点漂移")
        ids = [r["fact_id"] for r in res["results"]]
        self.assertNotIn(self.iron_id, ids)

    def store_get(self, fact_id):
        return self.fx.store.get_fact(fact_id)

    def test_anchor_injection_respects_fact_type_filter(self):
        # fact_type 过滤器是查询者显式意图，锚通道不得绕过
        res = self.fx.search("Kubernetes 节点漂移", fact_type="event")
        ids = [r["fact_id"] for r in res["results"]]
        self.assertNotIn(self.iron_id, ids,
                         "explicit fact_type filter must still gate anchors")

    def test_anchor_injection_respects_owner_filter(self):
        res = self.fx.search("Kubernetes 节点漂移", owner_principal="quantmaster")
        ids = [r["fact_id"] for r in res["results"]]
        self.assertNotIn(self.iron_id, ids)


class TestAnchorTrace(unittest.TestCase):
    """trace() 漏斗必须暴露 AnchorChannel 阶段。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = _AnchorFixture(self._tmp.name)
        self.iron_id = self.fx.seed("部署后必须运行冒烟测试",
                                    fact_type="iron_rule", owner="mentor")
        self.fx.project()

    def tearDown(self):
        self._tmp.cleanup()

    def test_trace_reports_anchor_stage(self):
        result = self.fx.kernel.trace(QueryRequest(
            text="数据库迁移", principal_id="mentor", roles=(), is_admin=False,
            owner_principal=None, domain=None, fact_type=None,
            use_vector=False, use_fts=True, use_graph=False,
            include_provisional=False,
        ))
        stages = {s["stage"] for s in result["stages"]}
        self.assertIn("AnchorChannel", stages)
        anchor = next(s for s in result["stages"] if s["stage"] == "AnchorChannel")
        self.assertGreaterEqual(anchor["hit"], 1)
        ids = [r["fact_id"] for r in result["results"]]
        self.assertIn(self.iron_id, ids)

    def test_trace_anchor_stage_reports_injected_count(self):
        result = self.fx.kernel.trace(QueryRequest(
            text="数据库迁移", principal_id="mentor", roles=(), is_admin=False,
            owner_principal=None, domain=None, fact_type=None,
            use_vector=False, use_fts=True, use_graph=False,
            include_provisional=False,
        ))
        anchor = next(s for s in result["stages"] if s["stage"] == "AnchorChannel")
        self.assertIn("injected", anchor)
        self.assertEqual(anchor["injected"], 1)


class TestAnchorLimits(unittest.TestCase):
    """锚通道注入量必须受预算约束，防止铁律库膨胀时挤占 top-K。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = _AnchorFixture(self._tmp.name)
        for i in range(40):
            self.fx.seed(f"铁律编号{i}：所有变更必须走事件流",
                         fact_type="iron_rule", owner="mentor")
        self.fx.seed("一次普通的事件记录", fact_type="event", owner="mentor")
        self.fx.project()

    def tearDown(self):
        self._tmp.cleanup()

    def test_anchor_injection_is_capped(self):
        res = self.fx.search("普通事件", limit=10)
        anchored = [r for r in res["results"]
                    if "anchor" in r["score_explanation"]["channels"]]
        self.assertLessEqual(len(anchored), 20,
                             "anchor channel must be budget-capped, not unbounded")


if __name__ == "__main__":
    unittest.main()
