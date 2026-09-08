# -*- coding: utf-8 -*-
"""P0-I AGENT_IDS 配置化注册（GitHub issue #1）。

issue 提的 v12 期闭集阻塞已在 v14 部分演进（register_agent/
register_domain 动态 API 存在）；本卡补齐开箱面：env 引导 + CLI。
三入口：MIMIR_AGENTS/MIMIR_DOMAINS 环境变量、register-agent/
register-domain CLI、运行时 register_agent() API。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import (
    AGENT_IDS, DOMAINS,
    get_registered_agents, get_registered_domains,
    register_agent, register_domain,
    validate_agent_id, validate_domain,
    CreateFact,
)


class TestEnvBootstrap(unittest.TestCase):

    def test_default_roster_untouched(self):
        """不设 env → 四席默认照旧（生产回退面）。"""
        self.assertIn("mentor", get_registered_agents())
        self.assertIn("quant", get_registered_domains())

    def test_env_bootstrap_extends_roster(self):
        """MIMIR_AGENTS=juggernaut,nyx → 新成员可建 fact（干净进程语义）。"""
        import subprocess, json, tempfile
        code = (
            "import os, json; "
            "os.environ['MIMIR_AGENTS'] = 'juggernaut,nyx'; "
            "os.environ['MIMIR_DOMAINS'] = 'gaming'; "
            "from mimir_v8.schema import validate_agent_id, validate_domain, CreateFact; "
            "validate_agent_id('juggernaut'); validate_domain('gaming'); "
            "f = CreateFact(content='x', summary='x', owner_principal='juggernaut', "
            "fact_type='user_pref', domain='gaming'); "
            "print(json.dumps({'ok': f.owner_principal}))"
        )
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True,
                cwd=str(root), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("juggernaut", r.stdout)

    def test_runtime_registration(self):
        """运行时 register_agent + validate 直通。"""
        register_agent("atlas-agent")
        register_domain("robotics")
        self.assertEqual(validate_agent_id("atlas-agent"), "atlas-agent")
        self.assertEqual(validate_domain("robotics"), "robotics")

    def test_unknown_agent_still_rejected(self):
        """未注册者仍 fail closed（ACL 边界不因开箱可扩而放宽）。"""
        with self.assertRaises(Exception):
            validate_agent_id("never-registered-agent")


if __name__ == "__main__":
    unittest.main()
