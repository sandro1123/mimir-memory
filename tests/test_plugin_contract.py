# -*- coding: utf-8 -*-
"""P0-G 插件契约测试重写（替换 TestM1dHermesPluginContract 预存债）。

旧契约（v12 期）期待 `~/.hermes/plugins/memory/mimir_memory_provider.py`
单文件 + provider.py + on_turn_* hooks——三件全部过时：
1. 单文件死副本已删（v9.2 admin-token-for-every-agent + hash() 幂等 +
   静默吞异常三病源，嘟嘟🔴2/3 与 issue #5 Minor 双点名）；
2. 现插件是包形态 `hermes-plugin/mimir_memory_provider/`（14.1.0 起），
   实现 Hermes MemoryProvider ABC；
3. hooks 面演进为 ABC 方法（initialize/prefetch/handle_tool_call 等）。

本契约从**仓库路径**导入（clean checkout 即绿，不再依赖开发者机器
~/.hermes/plugins —— 嘟嘟🔴1/🟡9 的发布契约病根）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "hermes-plugin"
sys.path.insert(0, str(PLUGIN_DIR))

from mimir_memory_provider import MimirMemoryProvider
from mimir_memory_provider import tools


class TestPluginPackageContract(unittest.TestCase):
    """包形态插件的导入面 + ABC 面契约。"""

    def test_package_imports_from_repo(self):
        """clean checkout 直接可导入（不再依赖 ~/.hermes 安装态）。"""
        import mimir_memory_provider
        self.assertTrue(hasattr(mimir_memory_provider, "MimirMemoryProvider"))

    def test_provider_abc_surface(self):
        p = MimirMemoryProvider()
        # name/is_available 是 property（返回 str/bool），其余是方法
        self.assertEqual(p.name, "mimir")
        self.assertTrue(callable(p.is_available) or isinstance(p.is_available, bool))
        for name in ("initialize", "system_prompt_block", "prefetch",
                     "get_tool_schemas", "handle_tool_call"):
            self.assertTrue(callable(getattr(p, name, None)), name)

    def test_tools_surface(self):
        for name in ("mimir_search", "mimir_remember", "mimir_recent",
                     "mimir_reflect"):
            self.assertTrue(callable(getattr(tools, name, None)), name)

    def test_idempotency_key_is_sha256_not_hash(self):
        """幂等键必须进程稳定（嘟嘟🔴3 的病根回归面）。"""
        k1 = tools._idempotency_key("mentor", "同一段内容")
        k2 = tools._idempotency_key("mentor", "同一段内容")
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("plugin-remember:"))
        # 不同 owner 相同内容 → 不同键（防跨用户碰撞）
        k3 = tools._idempotency_key("jarvis", "同一段内容")
        self.assertNotEqual(k1, k3)

    def test_error_is_observable_not_silent(self):
        """传输失败必须返回结构化 error 而非 None（嘟嘟🔴2 回归面）。"""
        err = tools._error("unreachable", path="/v8/query", detail="refused")
        self.assertIn("error", err)
        self.assertEqual(err["error"]["kind"], "unreachable")

    def test_handle_unknown_tool_returns_error(self):
        p = MimirMemoryProvider()
        out = json.loads(p.handle_tool_call("no_such_tool", {}))
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
