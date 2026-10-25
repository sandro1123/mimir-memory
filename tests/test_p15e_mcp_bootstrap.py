# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.mcp import tool_definitions


class TestMCPBootstrap(unittest.TestCase):
    def test_bootstrap_tool_registered(self):
        tools = {t["name"]: t for t in tool_definitions()}
        self.assertIn("mimir_bootstrap", tools)
        self.assertIn("handshake", tools["mimir_bootstrap"]["description"].lower())


if __name__ == "__main__":
    unittest.main()
