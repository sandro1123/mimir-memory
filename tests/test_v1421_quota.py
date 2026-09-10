# -*- coding: utf-8 -*-
"""v14.2.1-2 RSS 日配额（用户 09-11 拍板：vault 全量、rss 限 100/日）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.worker import _extract_source_quotas


class TestQuotaConfig(unittest.TestCase):

    def test_default_rss_100(self):
        self.assertEqual(_extract_source_quotas().get("rss"), 100)

    def test_env_override(self):
        os.environ["MIMIR_EXTRACT_DAILY_QUOTA"] = "rss:50,vault:0"
        try:
            q = _extract_source_quotas()
            self.assertEqual(q["rss"], 50)
            self.assertEqual(q.get("vault"), 0)  # 0 = 不限
        finally:
            os.environ.pop("MIMIR_EXTRACT_DAILY_QUOTA", None)

    def test_env_clear_disables_defaults(self):
        """显式设 env 就完全接管（不再有 rss:100 默认）。"""
        os.environ["MIMIR_EXTRACT_DAILY_QUOTA"] = "rss:0"
        try:
            q = _extract_source_quotas()
            self.assertEqual(q.get("rss"), 0)
            self.assertNotIn("vault", q)  # vault 不在 quota 管控面=不限
        finally:
            os.environ.pop("MIMIR_EXTRACT_DAILY_QUOTA", None)


if __name__ == "__main__":
    unittest.main()
