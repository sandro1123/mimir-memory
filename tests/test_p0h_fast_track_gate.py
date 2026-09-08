# -*- coding: utf-8 -*-
"""P0-H GOVERNANCE_AUTO_APPROVE 死键通电（issue #5 Finding 4）。

governance.py:60 定义但全文零引用——嘟嘟判「默认激进」、#5 判「死键」，
两审计矛盾的真相：默认值 "1" 从未生效，真正自动提交的是
fast_track_commit_all（置信度 ≥0.8 免审直进 canonical，worker 治理轮自动触发）。
修：死键接成 fast_track 总闸；默认 0（开源安全面）；
生产行为不变（unit 显式 MIMIR_GOVERNANCE_AUTO_APPROVE=1）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mimir_v8.governance as gov
from mimir_v8.governance import fast_track_commit_all
from mimir_v8.store import CanonicalStore


class TestFastTrackGate(unittest.TestCase):

    def test_gate_off_by_default(self):
        """开源默认关闭：fast_track 未显式开启即 no-op（防错误自动晋升）。"""
        self.assertFalse(gov.GOVERNANCE_AUTO_APPROVE)

    def test_disabled_gate_blocks_commit(self):
        with tempfile.TemporaryDirectory():
            pass  # store fixture not needed: gate fires before any DB touch
        store = CanonicalStore(Path(self.id().replace(":", "_") + ".db"))
        result = fast_track_commit_all(store, None)
        self.assertEqual(result["committed"], 0)
        self.assertEqual(result["fast_track"], "disabled")
        self.assertIn("AUTO_APPROVE", result["message"])

    def test_explicit_enable_via_env(self):
        """MIMIR_GOVERNANCE_AUTO_APPROVE=1 → 门开（v14.1 行为，生产路径）。"""
        import importlib
        import os
        saved = os.environ.get("MIMIR_GOVERNANCE_AUTO_APPROVE")
        try:
            os.environ["MIMIR_GOVERNANCE_AUTO_APPROVE"] = "1"
            importlib.reload(gov)
            self.assertTrue(gov.GOVERNANCE_AUTO_APPROVE)
        finally:
            if saved is None:
                os.environ.pop("MIMIR_GOVERNANCE_AUTO_APPROVE", None)
            else:
                os.environ["MIMIR_GOVERNANCE_AUTO_APPROVE"] = saved
            importlib.reload(gov)  # restore module state for other tests


if __name__ == "__main__":
    unittest.main()
