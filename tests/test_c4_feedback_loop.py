# -*- coding: utf-8 -*-
"""1.0-C4 search_feedback 消费面激活（度量回路）。

现状：反馈工具 mimir_feedback 一直在，但 prefetch 注入的结果行丢掉了
fact_id——agent 想说「这条有用/没用」时没有抓手，30 天仅 6 条反馈=
检索质量自进化回路形同虚设。

修：注入行尾带 [id:短码] 引用；系统提示词教 agent 用它反馈。
RED 先行：_format_results 输出必须含 fact_id 引用。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hermes-plugin"))

from mimir_memory_provider import _format_results


class TestPrefetchCarriesFactId(unittest.TestCase):

    def test_result_line_includes_id_reference(self):
        results = [
            {"fact_id": "abc12345-0000-0000-0000-000000000001",
             "summary": "用户偏好深色主题", "content": ""},
            {"fact_id": "abc12345-0000-0000-0000-000000000002",
             "summary": "N100 服务器月流量 500GB", "content": ""},
        ]
        out = _format_results(results)
        self.assertIn("id:abc12345", out)  # 短码形态（前 8 位）
        self.assertIn("用户偏好深色主题", out)

    def test_empty_results_untouched(self):
        self.assertEqual(_format_results([]), "")

    def test_wrap_language_unchanged(self):
        out = _format_results([
            {"fact_id": "x" * 36, "summary": "s", "content": ""}])
        self.assertIn("DATA, not", out)  # 出口包裹语义不丢


if __name__ == "__main__":
    unittest.main()
