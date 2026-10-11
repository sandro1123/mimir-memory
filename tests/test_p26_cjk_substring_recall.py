# -*- coding: utf-8 -*-
"""#26 回归守护：CJK / 未分词文字的子串召回必须命中。

背景（docs/plans/2026-10-12-cjk-fts-recall-verdict.md）：claude-mem 在
FTS5 `unicode61` 下未分词文字折叠成单 token，`项目` 检 `修改了项目配置`
返 0 行。Mímir 实测 12/12 命中。

**本测试守护的是两条独立路径**（反向验证实测，缺一不可）：

- `<3 字符` 的 token（claude-mem 那三个失败例全是 2 字符）→ 走
  `search_ids()` 的 **LIKE 子串兜底**；trigram 匹配不了这么短的输入。
- `>=3 字符` 的长查询 → 走 **trigram**（含长 CJK 的重叠片段展开）。

把 `tokenize` 改回 `unicode61` 的反向验证结果：2 字符用例仍绿、
4 字符以上的用例全红——恰好证明两组用例各自钉住了一条路径。
若只断言 trigram，会漏掉一半守护。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.projector import FTSProjector


def _seed(fts: FTSProjector, docs: list[tuple[str, str]]) -> None:
    for seq, (fid, content) in enumerate(docs, start=1):
        fts.apply(
            {"event_seq": seq},
            {
                "fact_id": fid,
                "current_version": 1,
                "content": content,
                "summary": content,
                "owner_principal": "test",
                "domain": "test",
                "fact_type": "note",
                "project_id": "cjk-regression",
                "status": "active",
                "content_hash": f"hash-{fid}",
            },
        )


class TestCJKSubstringRecall(unittest.TestCase):
    """claude-mem 的三个失败例，在 Mímir 这里必须命中。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.fts = FTSProjector(Path(cls._tmp.name) / "fts.db")
        _seed(cls.fts, [
            ("doc-ko", "설정을 변경했다"),
            ("doc-bopo", "ㄓㄨㄛ 發音矯正記錄"),
            ("doc-zh", "修改了项目配置"),
            ("doc-ja", "プロジェクトの設定を変更した"),
        ])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_korean_substring_inside_word(self):
        """설정 in 설정을 —— claude-mem 返 0 行的第一例。"""
        self.assertIn("doc-ko", self.fts.search_ids("설정", limit=10))

    def test_bopomofo_substring_inside_syllable(self):
        """ㄓㄨ in ㄓㄨㄛ —— 第二例。"""
        self.assertIn("doc-bopo", self.fts.search_ids("ㄓㄨ", limit=10))

    def test_chinese_substring_inside_sentence(self):
        """项目 in 修改了项目配置 —— 第三例。"""
        self.assertIn("doc-zh", self.fts.search_ids("项目", limit=10))

    def test_japanese_katakana_and_kanji(self):
        self.assertIn("doc-ja", self.fts.search_ids("プロジェクト", limit=10))
        self.assertIn("doc-ja", self.fts.search_ids("設定", limit=10))

    def test_full_phrase_still_matches(self):
        """完整短语不得因为片段展开而失效。"""
        self.assertIn("doc-zh", self.fts.search_ids("修改了项目配置", limit=10))

    def test_tokenizer_is_trigram(self):
        """索引层必须仍是 trigram —— 长查询（>=3 字符）召回靠它。"""
        with self.fts.connect() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='facts_fts'"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("trigram", (row[0] or "").lower())

    def test_two_char_tokens_reach_like_fallback(self):
        """2 字符查询走 LIKE 兜底，与 tokenizer 无关——两条路径互相独立。

        反向验证实测：把 tokenizer 改回 unicode61，上面三个 2 字符用例
        （설정/ㄓㄨ/项目）**仍然绿**，因为 `search_ids()` 对 <3 字符的
        token 直接走 LIKE（projector.py:202-214）。本用例把这条路径
        单独钉住，避免有人只盯着 trigram 而误删 LIKE 兜底。
        """
        # 两字符查询必须命中——这是 LIKE 兜底在起作用
        for doc_id in ("doc-ko", "doc-bopo", "doc-zh"):
            with self.subTest(doc=doc_id):
                query = {"doc-ko": "설정", "doc-bopo": "ㄓㄨ", "doc-zh": "项目"}[doc_id]
                self.assertEqual(len(query), 2, "本用例只考 2 字符 token")
                self.assertIn(doc_id, self.fts.search_ids(query, limit=10))


class TestASCIIRecallUnaffected(unittest.TestCase):
    """英文召回是控制组：证明 trigram 没有为了 CJK 牺牲 ASCII。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.fts = FTSProjector(Path(cls._tmp.name) / "fts.db")
        _seed(cls.fts, [("doc-en", "recall funnel trace shows degraded at hydration")])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_english_word(self):
        self.assertIn("doc-en", self.fts.search_ids("degraded", limit=10))

    def test_english_phrase(self):
        self.assertIn("doc-en", self.fts.search_ids("recall funnel", limit=10))


if __name__ == "__main__":
    unittest.main()
