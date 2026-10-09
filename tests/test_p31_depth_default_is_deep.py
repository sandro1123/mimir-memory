# -*- coding: utf-8 -*-
"""方案C 测试: 客户端 depth 默认必须为 deep (防回归).

L1 gate 是 v12.2.0 刻意设计(spec 不动, 见 test_p28), 因此这里不测服务端
行为, 只测**调用方默认值** —— 客户端/MCP 必须是 deep, 否则插件/agent 的
主召回路径会丢掉 67% 的 L1 事实(reference/project_config/event/...)。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.client import MimirAPIClient


class TestClientDepthDefault(unittest.TestCase):
    def _capture_body(self, **kw):
        client = MimirAPIClient()
        captured = {}

        def fake_request(method, path, *, params=None, body=None, **k):
            captured["params"] = params
            captured["body"] = body
            captured["path"] = path
            return {"results": []}

        with mock.patch.object(client, "request", side_effect=fake_request):
            client.query("probe", **kw)
        return captured

    def test_query_defaults_to_deep(self):
        cap = self._capture_body()
        self.assertEqual(cap["body"]["depth"], "deep")

    def test_query_depth_overridable(self):
        cap = self._capture_body(depth="standard")
        self.assertEqual(cap["body"]["depth"], "standard")

    def test_query_trace_defaults_to_deep(self):
        client = MimirAPIClient()
        captured = {}

        def fake_request(method, path, *, params=None, body=None, **k):
            captured["params"] = params
            captured["body"] = body
            return {"recall_trace": {}}

        with mock.patch.object(client, "request", side_effect=fake_request):
            client.query_trace("probe")
        self.assertEqual(captured["body"]["depth"], "deep")
        self.assertEqual(captured["params"]["trace"], "true")


class TestMCPDepthDefault(unittest.TestCase):
    """MCP 工具: schema default 与分发默认都必须是 deep (两处不能漂移)."""

    def _mcp_source(self):
        root = Path(__file__).resolve().parent.parent
        return (root / "mimir_v8/mcp.py").read_text(encoding="utf-8")

    def test_mcp_schema_default_is_deep(self):
        src = self._mcp_source()
        self.assertIn('"depth": {"type": "string", "default": "deep"}', src)

    def test_mcp_dispatch_default_is_deep(self):
        src = self._mcp_source()
        self.assertIn('depth=args.get("depth", "deep")', src)

    def test_mcp_no_stale_standard_default(self):
        """旧值 standard 不得残留在 depth 相关默认里。"""
        src = self._mcp_source()
        self.assertNotIn('"depth": {"type": "string", "default": "standard"}', src)
        self.assertNotIn('depth=args.get("depth", "standard")', src)


if __name__ == "__main__":
    unittest.main()
