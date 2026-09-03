"""Mímir v14.0 — 跨模型认知语义投影 (spec 阶段四任务3).

适配不同模型窗口与输出格式，实现小模型挂载优质技能后的越级能力爆发：

- MODEL_TIERS 三档：claude（大窗，全保真）、deepseek（中窗，均衡）、
  local-small（小窗，紧凑方言）。
- project_context(facts, tier) 把检索面投影成目标模型的注入块：
  按层预算裁剪 —— L3（铁律/偏好/技能）保真保留，L2 摘要化，
  L1 只留标题行。
- 每档输出方言不同：claude 全量 markdown、deepseek 结构化列表、
  local-small 极简 KEY: value 线。
- 小模型挂载 L3 skill 后能力越级：skill 内容在 local-small 档
  依然完整保留（预算永远给技能让位 —— 与锚通道同一铁律）。

工程四严律：TDD 先行 / 不可变事件流 / 绝对路径安全 / 全量回归。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.projection import (
    MODEL_TIERS,
    ProjectionError,
    project_context,
    summarize_for_tier,
)
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _fact(store, content, *, fact_type="pattern", summary=None):
    return store.create_fact(
        CreateFact(
            content=content,
            summary=summary or content[:40],
            owner_principal="mentor",
            domain="knowledge",
            fact_type=fact_type,
            visibility="all",
            sensitivity="internal",
            egress_policy="local_only",
            human_status="confirmed",
        ),
        actor_principal="mentor",
    )


def _sample_facts(store):
    """One fact per layer: L3 iron rule, L3 skill, L2 pattern, L1 event."""
    return [
        _fact(store, "禁止在非变更窗口执行生产 SQL", fact_type="iron_rule"),
        _fact(store, "K8s 排障技能：先 drain 再查 cgroup",
              fact_type="skill",
              summary="k8s 排障技能"),
        _fact(store, "数据库连接池耗尽的排查模式：先看慢查询再查连接泄漏",
              fact_type="pattern"),
        _fact(store, "2026-09-03 例行巡检完成，无异常", fact_type="event"),
    ]


class TestModelTiers(unittest.TestCase):
    def test_three_tiers_registered(self):
        self.assertEqual(
            set(MODEL_TIERS), {"claude", "deepseek", "local-small"}
        )

    def test_tiers_carry_budget_and_dialect(self):
        for name, tier in MODEL_TIERS.items():
            with self.subTest(tier=name):
                self.assertGreater(tier["max_tokens"], 0)
                self.assertIn(tier["dialect"], ("markdown", "structured", "compact"))
        # budget ladder: claude > deepseek > local-small
        self.assertGreater(
            MODEL_TIERS["claude"]["max_tokens"],
            MODEL_TIERS["deepseek"]["max_tokens"],
        )
        self.assertGreater(
            MODEL_TIERS["deepseek"]["max_tokens"],
            MODEL_TIERS["local-small"]["max_tokens"],
        )


class TestProjectContext(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CanonicalStore(self.root / "canonical.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _facts(self):
        created = _sample_facts(self.store)
        return [self.store.get_fact(f["fact_id"]) for f in created]

    def test_projects_all_tiers(self):
        facts = self._facts()
        for tier in MODEL_TIERS:
            with self.subTest(tier=tier):
                out = project_context(facts, tier)
                self.assertIn(tier, out["tier"])
                self.assertTrue(out["blocks"])
                self.assertLessEqual(out["estimated_tokens"], out["budget"])

    def test_l3_content_survives_compact_tier(self):
        """小模型挂载优质技能后越级：skill 内容在 local-small 档
        完整保留 —— 预算永远给 L3 让位。"""
        facts = self._facts()
        out = project_context(facts, "local-small")
        blob = "\n".join(
            block["text"] for block in out["blocks"]
        )
        self.assertIn("K8s 排障技能", blob)
        self.assertIn("禁止在非变更窗口", blob)

    def test_l1_degrades_to_title_only(self):
        """L1 事件在所有档位只留标题行（证据可溯源，不占预算）。"""
        facts = self._facts()
        for tier in MODEL_TIERS:
            with self.subTest(tier=tier):
                out = project_context(facts, tier)
                blob = "\n".join(b["text"] for b in out["blocks"])
                self.assertNotIn("无异常", blob)

    def test_dialects_differ(self):
        """三档方言不同：claude markdown / deepseek 结构化 / 紧凑。"""
        facts = self._facts()
        c = project_context(facts, "claude")
        d = project_context(facts, "deepseek")
        s = project_context(facts, "local-small")
        self.assertNotEqual(c["blocks"][0]["text"], d["blocks"][0]["text"])
        self.assertNotEqual(d["blocks"][0]["text"], s["blocks"][0]["text"])

    def test_unknown_tier_rejected(self):
        facts = self._facts()
        with self.assertRaises(ProjectionError):
            project_context(facts, "gpt-999")

    def test_budget_shrinks_l2_first(self):
        """预算吃紧时先牺牲 L2（摘要化）而非丢 L3。"""
        facts = self._facts()
        wide = project_context(facts, "claude")
        tiny = project_context(facts, "local-small")
        # L2 pattern content is truncated on the compact tier
        tiny_blob = "\n".join(b["text"] for b in tiny["blocks"])
        self.assertNotIn("先看慢查询再查连接泄漏", tiny_blob)


class TestSummarizeForTier(unittest.TestCase):
    def test_summarize_truncates_long_content(self):
        long_text = "排障步骤 " + "细节填充 " * 200
        out = summarize_for_tier(long_text, max_chars=50)
        self.assertLessEqual(len(out), 52)
        self.assertIn("…", out)

    def test_summarize_keeps_short_content(self):
        out = summarize_for_tier("短内容", max_chars=50)
        self.assertEqual(out, "短内容")


if __name__ == "__main__":
    unittest.main()
