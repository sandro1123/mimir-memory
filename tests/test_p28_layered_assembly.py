"""Mímir v12.2.0 — L0~L3 分层装配 (spec 阶段二任务1).

渐进式展开语义：
- L3 Persona / Iron Rules  = iron_rule + user_pref      （默认装配）
- L2 Scenarios / Wiki      = pattern                    （默认装配）
- L1 Atom Facts            = 其余五型原子事实            （默认不装配，deep 才下钻）
- L0 Conversation          = 原始对话/执行痕迹（证据层）  （检索永不装配）

检索默认只装配 L3+L2，深度追溯（depth="deep"）才下钻 L1，
大幅削减 Token 消耗。映射零迁移：全部复用 facts.fact_type
既有枚举与 conversation_messages 证据层。

TDD RED → GREEN.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.projector import FTSProjector, ProjectorRunner
from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


class FTSPathFixture:
    """Build a real FTS projection and drain the outbox into it."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.projector = FTSProjector(Path(self._tmp.name) / "fts.db")

    def drain_all(self, store: CanonicalStore):
        runner = ProjectorRunner(store, self.projector)
        while True:
            result = runner.run_once(limit=200)
            if result["processed"] == 0 or result["failed"]:
                break

    def close(self):
        self._tmp.cleanup()


def _seed(store, content, *, fact_type, owner="mentor", domain="knowledge"):
    # ephemeral facts may not be human-confirmed (schema cross-field rule)
    human_status = "unreviewed" if fact_type == "ephemeral" else "confirmed"
    return store.create_fact(
        CreateFact(
            content=content,
            summary=content[:40],
            owner_principal=owner,
            domain=domain,
            fact_type=fact_type,
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status=human_status,
        ),
        actor_principal=owner,
    )["fact_id"]


class LayerFixture:
    """种子一套覆盖 L3/L2/L1 四型事实，关掉三相似度通道只留锚+装配层。

    相似度通道关闭后，唯一入口是锚通道（只注 iron_rule/user_pref），
    因此 search() 的结果面就是「装配层放行了什么」的纯观测窗口：
    L2 pattern 与 L1 五型想被装配必须经由装配层自身的可见性规则。
    """

    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.kernel = QueryKernel(self.store)  # no vector/fts/graph
        self.iron_id = _seed(self.store, "生产库只允许经 API 写入", fact_type="iron_rule")
        self.pref_id = _seed(self.store, "报告一律用中文输出", fact_type="user_pref")
        self.pattern_id = _seed(self.store, "节点漂移的标准排障流程五步", fact_type="pattern")
        self.event_id = _seed(self.store, "昨夜 03:00 备份窗口执行完成", fact_type="event")
        self.config_id = _seed(self.store, "备份窗口配置为 23:00", fact_type="project_config")
        self.learn_id = _seed(self.store, "学到的教训：先查记忆再动手", fact_type="learning")
        self.ref_id = _seed(self.store, "参考文档：SQLite WAL 模式", fact_type="reference")
        self.eph_id = _seed(self.store, "临时上下文：正在调试会话", fact_type="ephemeral")

    def search(self, **kw):
        defaults = dict(
            text="节点 漂移 排障 流程 备份 窗口 报告 中文 教训 文档",
            principal_id="mentor",
            limit=50,
        )
        defaults.update(kw)
        return self.kernel.search(QueryRequest(**defaults))


class TestLayerAssembly(unittest.TestCase):
    """默认档：只装配 L3+L2；deep 档：下钻 L1。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = LayerFixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _result_ids(self, response):
        return {r["fact_id"] for r in response["results"]}

    def test_default_assembly_keeps_L3_and_L2_only(self):
        response = self.fx.search()
        ids = self._result_ids(response)
        self.assertIn(self.fx.iron_id, ids)      # L3
        self.assertIn(self.fx.pref_id, ids)      # L3
        self.assertIn(self.fx.pattern_id, ids)   # L2 — 关键增量语义
        # L1 五型全部不出现在默认结果里
        for fid in (self.fx.event_id, self.fx.config_id, self.fx.learn_id,
                    self.fx.ref_id, self.fx.eph_id):
            self.assertNotIn(fid, ids)

    def test_deep_assembly_expands_L1_atom_facts(self):
        response = self.fx.search(depth="deep")
        ids = self._result_ids(response)
        # L1 全型在 deep 档出现
        for fid in (self.fx.event_id, self.fx.config_id, self.fx.learn_id,
                    self.fx.ref_id, self.fx.eph_id):
            self.assertIn(fid, ids)
        self.assertIn(self.fx.pattern_id, ids)
        self.assertIn(self.fx.iron_id, ids)

    def test_invalid_depth_rejected(self):
        with self.assertRaises(ValueError):
            self.fx.search(depth="sideways")

    def test_default_reports_layer_filter_counts(self):
        # 门拦截计数须在「L1 确实进过候选池」的场景下观测：挂上 FTS 通道
        # 让相似度命中把 L1 送进池，再断言 standard 档 hydration 把它们
        # 拦下并计数（deep 档同场景放行）。
        from mimir_v8.projector import FTSProjector

        fts = FTSPathFixture()
        fts.drain_all(self.fx.store)
        kernel = QueryKernel(self.fx.store, fts=fts.projector)
        request = QueryRequest(
            text="备份 窗口 执行 完成 23:00", principal_id="mentor", limit=50,
        )
        response = kernel.search(request)
        self.assertEqual(response["filtered"]["layer"], 2)  # event + project_config
        deep = kernel.search(replace(request, depth="deep"))
        self.assertEqual(deep["filtered"]["layer"], 0)
        deep_ids = {r["fact_id"] for r in deep["results"]}
        self.assertIn(self.fx.event_id, deep_ids)

    def test_standard_drops_fts_matched_L1(self):
        # 与上一测同场景：standard 结果面里 FTS 已命中的 L1 也必须消失
        from mimir_v8.projector import FTSProjector

        fts = FTSPathFixture()
        fts.drain_all(self.fx.store)
        kernel = QueryKernel(self.fx.store, fts=fts.projector)
        response = kernel.search(QueryRequest(
            text="备份 窗口 执行 完成 23:00", principal_id="mentor", limit=50,
        ))
        ids = {r["fact_id"] for r in response["results"]}
        self.assertNotIn(self.fx.event_id, ids)
        self.assertNotIn(self.fx.config_id, ids)

    def test_explicit_fact_type_overrides_layer_default(self):
        # 显式 fact_type=event 表达检索者想挖 L1 原子事件 → 深挖意图放行
        response = self.fx.search(fact_type="event")
        ids = self._result_ids(response)
        self.assertIn(self.fx.event_id, ids)

    def test_filters_echo_depth(self):
        response = self.fx.search()
        self.assertEqual(response["filters"]["depth"], "standard")


class TestLayerConstants(unittest.TestCase):
    """分层映射常量与既有 fact_type 枚举零偏差。"""

    def test_L3_types_are_anchor_types(self):
        # L3 与锚通道共享语义（安全底线），但分层集合是超集也无妨——
        # v14.0 起钉死 L3 = {iron_rule, user_pref, skill}
        # （skill = 胜任门槛 + 一键审批编译出的 Hermes Skill）
        self.assertEqual(set(QueryKernel.LAYER3_FACT_TYPES), {"iron_rule", "user_pref", "skill"})

    def test_L2_types_are_pattern(self):
        self.assertEqual(set(QueryKernel.LAYER2_FACT_TYPES), {"pattern"})

    def test_L1_types_cover_remaining_enum(self):
        from mimir_v8.schema import FACT_TYPES
        covered = (set(QueryKernel.LAYER3_FACT_TYPES)
                   | set(QueryKernel.LAYER2_FACT_TYPES)
                   | set(QueryKernel.LAYER1_FACT_TYPES))
        self.assertEqual(covered, set(FACT_TYPES))
        # L1 与 L3/L2 无交集
        self.assertFalse(set(QueryKernel.LAYER1_FACT_TYPES)
                         & (set(QueryKernel.LAYER3_FACT_TYPES)
                            | set(QueryKernel.LAYER2_FACT_TYPES)))


if __name__ == "__main__":
    unittest.main()
