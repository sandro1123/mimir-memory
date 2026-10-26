"""Tests for scripts/version_sweep.py — 版本号派生登记表扫描器。

三问：
1. 扫描器能扫到东西（真仓跑一遍，至少 5 条全 OK）。
2. 反向注入红（故意传假版本号 → 至少 1 条 STALE）。
3. 不误伤（CHANGELOG 里引用旧版本号不报 STALE）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 把 scripts/ 加到路径，使 version_sweep 可 import
sys.path.insert(0, str(ROOT / "scripts"))

import version_sweep  # noqa: E402


class TestVersionSweep(unittest.TestCase):

    def test_current_version_all_ok(self):
        """真仓跑一遍：当前版本号在所有登记点都在。"""
        expected = version_sweep._read_current_version(ROOT)
        results = version_sweep.sweep(ROOT, expected)
        self.assertTrue(len(results) >= 5, f"登记点不足 5 条: {len(results)}")
        for r in results:
            self.assertEqual(
                r.status, "OK",
                f"{r.relpath}: {r.description} — "
                f"found {r.found_version}, expected {r.expected_version}",
            )

    def test_fake_version_triggers_stale(self):
        """反向注入：假版本号 → 至少 1 条 STALE。"""
        results = version_sweep.sweep(ROOT, "0.0.0-nonexistent")
        stale = [r for r in results if r.status != "OK"]
        self.assertGreater(len(stale), 0,
                           "假版本号 0.0.0-nonexistent 居然全 OK？扫描器失效")

    def test_all_five_known_registries_scanned(self):
        """扫描器登记表至少覆盖已知五处。"""
        relpaths = {e.relpath for e in version_sweep.REGISTRY}
        for expected_path in (
            "mimir_v8/schema.py",
            "pyproject.toml",
            "dashboard/backend/main.py",
            "ARCHITECTURE.md",
            "tests/test_r8_release.py",
        ):
            self.assertIn(expected_path, relpaths,
                          f"登记表缺少 {expected_path}")

    def test_changelog_old_versions_not_flagged(self):
        """CHANGELOG 中引用旧版本号属于历史记录，不应出现在扫描结果中。"""
        # version_sweep 只扫 REGISTRY 里列出的文件，CHANGELOG 不在其中。
        relpaths = {e.relpath for e in version_sweep.REGISTRY}
        self.assertNotIn("CHANGELOG.md", relpaths,
                         "CHANGELOG.md 不应被列为登记点（会误伤历史条目）")


if __name__ == "__main__":
    unittest.main()
