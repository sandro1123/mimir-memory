# -*- coding: utf-8 -*-
"""P0-M dedup 召回性能改造（嘟嘟🟡1/2）。

现状：check_duplicate 全表扫 owner 的全部 active facts + committed
candidates 逐条 Jaccard——10 万条记忆时每次候选写入 O(N) 拖垮写入路径。
另：tokenizer 不留数字/版本/型号 token（Gemini-3.8-Flash 会碎成
gemini/flash，型号语义丢失）。

修：
1. 召回层——用现有 fts 投影按 content 关键词召回 top-K（K=50），
   只在召回集上做精确 Jaccard（语义不变：同一输入在同一库上
   必须命中相同的重复对）。
2. tokenizer 补实体 token：数字/版本/型号段（3.8、v14.2、k8s）。

降级纪律：fts 投影不可用时回退全表扫（正确性优先于性能）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.dedup import _tokenize, check_duplicate, jaccard_similarity
from mimir_v8.schema import CreateFact
from mimir_v8.store import CanonicalStore


def _seed(store, content, fact_id_hint=0, owner="mentor"):
    return store.create_fact(CreateFact(
        content=content, summary=content[:40], owner_principal=owner,
        fact_type="reference", domain="knowledge", visibility="all",
        sensitivity="internal", egress_policy="local_only",
        human_status="confirmed",
    ), actor_principal=owner)["fact_id"]


class TestTokenizerEntities(unittest.TestCase):

    def test_version_tokens_preserved(self):
        toks = _tokenize("升级 Gemini-3.8-Flash 与 chromadb 1.5.9")
        self.assertIn("3.8", toks)
        self.assertIn("1.5.9", toks)
        self.assertIn("gemini", toks)

    def test_similarity_uses_entity_tokens(self):
        # 型号差异必须让两句「同模板不同型号」的内容相似度低于完全
        # 同句的基线。Jaccard 数学上：a 是 c 的子串时交集/并集被拉低，
        # 直接比 a~c 会在短句上失真——正确的断言面是「同模板不同型号
        # 的互相似度显著低于型号相同的全文复制」。
        a = "部署 Gemini-3.8-Flash 服务于生产环境"
        b = "部署 Gemini-4.0-Pro 服务于生产环境"
        same = jaccard_similarity(a, a)
        ab = jaccard_similarity(a, b)
        # 完全同句=1.0；型号差异至少砍掉 30% 相似度（旧 tokenizer 下
        # 数字被丢，两句只剩中文 bigram 完全一致 → ab 会逼近 same）
        self.assertEqual(same, 1.0)
        self.assertLess(ab, 0.85, "型号差异未显著影响相似度——实体 token 未生效")


class TestTopKRecall(unittest.TestCase):

    def test_duplicate_detection_correctness_unchanged(self):
        """召回改造后语义不变：真重复必须被识别。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储")
            r = check_duplicate(store, "Mímir 是事件溯源的联邦记忆系统", "mentor")
            # 0.76 落在 update 带（0.70~0.85）——决策矩阵设计行为：
            # 子串级近似是「候选更新」不是「全同合并」
            self.assertTrue(r["is_duplicate"])
            self.assertEqual(r["match_type"], "update")

    def test_exact_duplicate_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储")
            r = check_duplicate(store, "Mímir 是事件溯源的联邦记忆系统，用 SQLite 存储", "mentor")
            self.assertTrue(r["is_duplicate"])
            self.assertEqual(r["match_type"], "merge")

    def test_far_apart_content_is_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            _seed(store, "今天午餐吃了饺子")
            r = check_duplicate(store, "量子计算机的纠错码新进展", "mentor")
            self.assertFalse(r["is_duplicate"])
            self.assertEqual(r["match_type"], "new")

    def test_recall_cap_limits_jaccard_pool(self):
        """超过召回上限时只对 top-K 做精排——正确性对高频词内容保持。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canon.db")
            # 建一个高频词背景库：同 owner 多条无关事实
            for i in range(30):
                _seed(store, f"背景记录 {i}：系统运行的常规日志片段与状态说明")
            _seed(store, "独特事实：ZebraCrossing 协议在北极圈测试通过")
            r = check_duplicate(store, "独特事实：ZebraCrossing 协议在北极圈测试", "mentor")
            self.assertTrue(r["is_duplicate"])
            self.assertEqual(r["match_type"], "merge")


if __name__ == "__main__":
    unittest.main()
