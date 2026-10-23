# -*- coding: utf-8 -*-
"""roadmap #14 预算诚实：超 budget 必须显式说，不许静默截断。

## 现状实测（2026-10-23）

`recall_explanation`（10-12 已做）里确实有预算诚实：`declared_budget` /
`used` / `dropped` 三件套，超预算且有丢弃但没有 reason 就抛 ValueError。

**但那个对象只在 `include_trace=True` 时才出现。** 默认路径下：

- `query.py:559` `top_results = results[: request.limit]` 静默截断
- 返回里有 `candidate_count`（候选总数）与 `results`（截断后）
- **没有任何字段说明"你比候选少看了 N 条，以及为什么"**

调用方得自己拿 `candidate_count - len(results)` 反推，且推不出原因。
这正是 #14 要防的形态：载荷被缩减，却没有随载荷给出解释。

TDD RED：`budget` 块尚不存在。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.query import QueryKernel, QueryRequest
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore

FILLER = "系统运行日志与状态记录，编号 {i}，包含常规巡检结果与节点状态说明。"


class _Base(unittest.TestCase):
    owner = "mentor"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = CanonicalStore(root / "canon.db")
        self.q = QueryKernel(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def seed(self, n, prefix="事实"):
        # 用 iron_rule：锚通道直取 canonical，天然进入候选池，不依赖
        # FTS 投影同步（本测试要测的是预算口径，不是投影链路）。
        for i in range(n):
            self.store.create_fact(CreateFact(
                content=f"{prefix} {i}：{FILLER.format(i=i)}",
                summary=f"{prefix}{i}"[:40], owner_principal=self.owner,
                domain="knowledge", fact_type="iron_rule", visibility="all",
                sensitivity="internal", egress_policy="local_only",
                human_status="confirmed"), actor_principal=self.owner)

    def ask(self, text="系统运行日志", limit=3):
        return self.q.search(QueryRequest(
            text=text, principal_id=self.owner, limit=limit, candidate_limit=50))


class TestBudgetBlock(_Base):
    def test_budget_block_always_present(self):
        """默认路径也必须给预算块——不必开 trace 才能知道被截了多少。"""
        self.seed(10)
        r = self.ask(limit=3)
        self.assertIn("budget", r, "默认返回必须带 budget 块")

    def test_budget_reports_dropped_count(self):
        """被截掉的条数必须显式给出，不能让调用方自己减。"""
        self.seed(10)
        r = self.ask(limit=3)
        b = r["budget"]
        self.assertEqual(b["requested"], 3)
        self.assertEqual(b["returned"], len(r["results"]))
        self.assertEqual(b["dropped"], b["candidates"] - b["returned"],
                         "dropped 必须等于候选数 - 返回数（同一口径）")

    def test_budget_exceeded_flag(self):
        """超预算要有明确的布尔，别让人比数字。"""
        self.seed(10)
        r = self.ask(limit=3)
        self.assertTrue(r["budget"]["exceeded"])

    def test_budget_not_exceeded_when_all_fit(self):
        """未截断时不得谎报 exceeded（防噪声）。"""
        self.seed(2)
        r = self.ask(limit=10)
        self.assertFalse(r["budget"]["exceeded"])
        self.assertEqual(r["budget"]["dropped"], 0)

    def test_budget_carries_reason(self):
        """丢弃必须带原因——只说"少了 N 条"仍不足以让人行动。"""
        self.seed(10)
        r = self.ask(limit=3)
        reasons = r["budget"].get("drop_reasons")
        self.assertIsInstance(reasons, dict)
        self.assertTrue(reasons, "必须有原因键")
        self.assertIn("limit", reasons, "截断原因应归到 limit")

    def test_no_silent_stub_shape(self):
        """#14 的核心断言：不允许「宣称成功但载荷被换掉」。

        返回的每条结果必须是真实 fact（有 fact_id 与内容），
        不能是占位 stub。
        """
        self.seed(10)
        r = self.ask(limit=3)
        for item in r["results"]:
            self.assertTrue(item.get("fact_id"), "结果必须是真实 fact，不得是 stub")
            self.assertIn("content", item)
            self.assertTrue(str(item["content"]).strip())
            self.assertNotIn("stub", str(item.get("content", "")).lower())


class TestBudgetIsHonestUnderZeroResults(_Base):
    def test_empty_result_budget_is_clean(self):
        r = self.ask(limit=3)
        b = r["budget"]
        self.assertEqual(b["returned"], 0)
        self.assertEqual(b["dropped"], 0)
        self.assertFalse(b["exceeded"], "没有结果不等于超预算")


if __name__ == "__main__":
    unittest.main()
