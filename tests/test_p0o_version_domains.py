# -*- coding: utf-8 -*-
"""P0-O 版本域单一事实源（嘟嘟🟡7 + 用户点名「版本迭代乱」）。

五域漂移实证（P0-D 夜当场踩中）：MIMIR_VERSION 改 14.2.0 时
pyproject.toml 与 test_r8_release 断言停在 14.1.0——两处假红。
修：MIMIR_VERSION 仍是唯一语义源，本测试做三方对拍断言
（schema.py ↔ pyproject.toml ↔ health 报文），任何一处漂移即红。
"""
from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.schema import MIMIR_VERSION, SCHEMA_VERSION


class TestVersionSingleSource(unittest.TestCase):

    def test_pyproject_matches_schema(self):
        root = Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["version"], MIMIR_VERSION,
                         "pyproject.toml 与 MIMIR_VERSION 漂移——改版必须同步（或跑本测试自红）")

    def test_changelog_has_version_section(self):
        root = Path(__file__).resolve().parent.parent
        text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## v{MIMIR_VERSION}", text,
                      "CHANGELOG 缺当前版本段——版本发布时五件套之一")

    def test_schema_version_has_migration_segment(self):
        """原意不是「版本号必须等于 20」，而是「版本提了必须配迁移段」。

        旧写法硬编码 20，提版即假红；新写法断言运行时 schema 已在迁移链
        目标集内，并断言源集覆盖历史全档——守卫加强而非放宽（提版忘记
        登记迁移目标 / 删掉历史源档，两种都会红）。
        """
        from mimir_v8.migration import (MIGRATABLE_SOURCE_VERSIONS,
                                        MIGRATABLE_TARGET_VERSIONS)

        self.assertIn(SCHEMA_VERSION, MIGRATABLE_TARGET_VERSIONS,
                      "SCHEMA_VERSION 提了但没在迁移链登记目标版本——迁移段缺失")
        self.assertTrue(
            {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20}
            <= MIGRATABLE_SOURCE_VERSIONS,
            "迁移链源版本集缩水——历史库将无法升级",
        )


if __name__ == "__main__":
    unittest.main()
