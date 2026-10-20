# -*- coding: utf-8 -*-
"""roadmap #16：判重三档信任分层 —— Tier-0 确定性静默合并 + 冷启动门。

issue #10（映射 roadmap 16）契约节选：
- Tier-0：content_hash 精确命中即静默合并（无 LLM，O(1)，不依赖 Jaccard 召回池）
- 冷启动门 is_fuzzy_ready：样本不足时不启动模糊判重（宁可漏报，不误杀）
- Tier-1（模糊）只准 ADD 语义：本卡只做门控，不改写路径契约（零破坏）

TDD RED：本文件引用的符号尚不存在，必须先红。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _seed(store, content, owner="mentor"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal=owner,
        fact_type="reference", domain="knowledge", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)["fact_id"]


class TestFuzzyReadyGate(unittest.TestCase):
    def test_empty_store_is_not_ready(self):
        from mimir_v8.dedup import is_fuzzy_ready
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            self.assertFalse(is_fuzzy_ready(store, "mentor", min_docs=3))

    def test_ready_after_enough_docs(self):
        from mimir_v8.dedup import is_fuzzy_ready
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "事实一：系统运行的常规日志片段")
            _seed(store, "事实二：系统运行的常规日志片段之二")
            self.assertFalse(is_fuzzy_ready(store, "mentor", min_docs=3))
            _seed(store, "事实三：系统运行的常规日志片段之三")
            self.assertTrue(is_fuzzy_ready(store, "mentor", min_docs=3))

    def test_ready_is_per_owner(self):
        """A 的样本数不得给 B 开门（owner 隔离）。"""
        from mimir_v8.dedup import is_fuzzy_ready
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            for i in range(3):
                _seed(store, f"A 的事实 {i}：系统运行的常规日志", owner="jarvis")
            self.assertTrue(is_fuzzy_ready(store, "jarvis", min_docs=3))
            self.assertFalse(is_fuzzy_ready(store, "quantmaster", min_docs=3))


class TestTier0ExactMerge(unittest.TestCase):
    def test_exact_duplicate_without_tokens_still_merges(self):
        """无 token 内容（Jaccard 恒 0）也必须被 Tier-0 哈希命中。

        这是 Tier-0 存在的全部理由：不依赖分词/召回池的 O(1) 确定性路径。
        """
        from mimir_v8.dedup import check_duplicate
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            fid = _seed(store, "!!! ??? ...")
            r = check_duplicate(store, "!!! ??? ...", "mentor")
            self.assertTrue(r["is_duplicate"], "精确同内容必须命中")
            self.assertEqual(r["match_type"], "merge")
            self.assertEqual(r["matched_fact_id"], fid)
            self.assertEqual(r.get("tier"), "tier0")

    def test_exact_duplicate_reports_tier0(self):
        from mimir_v8.dedup import check_duplicate
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            fid = _seed(store, "Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储")
            r = check_duplicate(store, "Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储", "mentor")
            self.assertTrue(r["is_duplicate"])
            self.assertEqual(r["match_type"], "merge")
            self.assertEqual(r["matched_fact_id"], fid)
            self.assertEqual(r.get("tier"), "tier0")

    def test_exact_bypasses_fuzzy_gate_when_cold(self):
        """冷启动门关着时，精确同内容仍必须合并（Tier-0 不受门控）。

        这是分层顺序的全部证明：Tier-0 必须跑在门检查之前。
        """
        from mimir_v8.dedup import check_duplicate
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            fid = _seed(store, "!!! ??? ...")
            r = check_duplicate(store, "!!! ??? ...", "mentor", fuzzy_min_docs=5)
            self.assertTrue(r["is_duplicate"], "Tier-0 不受冷启动门限制")
            self.assertEqual(r["match_type"], "merge")
            self.assertEqual(r.get("tier"), "tier0")

    def test_exact_is_owner_scoped(self):
        """跨 owner 的同内容不得合并（ACL 隔离）。"""
        from mimir_v8.dedup import check_duplicate
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "共享文本：系统运行的常规日志", owner="jarvis")
            r = check_duplicate(store, "共享文本：系统运行的常规日志", "quantmaster")
            self.assertFalse(r["is_duplicate"])
            self.assertEqual(r["match_type"], "new")


class TestFuzzyGateWiring(unittest.TestCase):
    NEAR_A = "用户偏好新、环境好、带泳池等设施、价位与城堡酒店相当的酒店"
    NEAR_B = "用户偏好选择较新、环境好、带泳池等设施、价位与城堡酒店相当的酒店"

    def _seed_ready(self, store, n=3):
        _seed(store, self.NEAR_A)
        for i in range(n - 1):
            _seed(store, f"背景记录 {i}：系统运行的常规日志片段与状态说明")

    def test_fuzzy_gated_when_cold(self):
        """样本不足时模糊判重必须让路（返回 new），即使 Jaccard 会命中。"""
        from mimir_v8.dedup import check_duplicate
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            self._seed_ready(store, n=1)
            r = check_duplicate(store, self.NEAR_B, "mentor", fuzzy_min_docs=5)
            self.assertFalse(r["is_duplicate"], "冷启动时模糊判重不得报重复")
            self.assertEqual(r["match_type"], "new")

    def test_fuzzy_allowed_when_ready(self):
        from mimir_v8.dedup import check_duplicate
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            self._seed_ready(store, n=5)
            r = check_duplicate(store, self.NEAR_B, "mentor", fuzzy_min_docs=5)
            self.assertTrue(r["is_duplicate"])
            self.assertEqual(r["match_type"], "update")
            self.assertEqual(r.get("tier"), "tier1")

    def test_gate_off_by_default_is_zero_breakage(self):
        """不传 fuzzy_min_docs 时行为与从前一致（门默认关闭）。"""
        from mimir_v8.dedup import check_duplicate
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            self._seed_ready(store, n=1)
            r = check_duplicate(store, self.NEAR_B, "mentor")
            self.assertTrue(r["is_duplicate"])
            self.assertEqual(r["match_type"], "update")


if __name__ == "__main__":
    unittest.main()
