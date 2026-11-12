# -*- coding: utf-8 -*-
"""Stage-2 Task 4 — self-ingest guard (anti-feedback-loop, spec §7.2).

Mímir writes entity cards into the vault; the vault collector ingests the
vault. Without a guard the loop closes: card → ingest → crystallize → new
card. The guard must land BEFORE the first card is ever written, and the
reverse-injection test proves the test really guards it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSelfIngestGuard(unittest.TestCase):
    def _make_vault(self, root: Path):
        card = root / "90-机器收件箱" / "Mímir 卡片"
        card.mkdir(parents=True)
        (card / "ent_1.md").write_text(
            "---\nmimir_generated: true\nmimir_id: ent_1\n---\n\n正文\n",
            encoding="utf-8",
        )
        (root / "普通笔记.md").write_text("---\n---\n\n人类内容\n", encoding="utf-8")

    def test_mimir_card_is_not_ingested(self):
        import tempfile

        from mimir_v8.collectors.vault import VaultCollector

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_vault(root)
            results = VaultCollector(vault_root=root).collect()
            ids = [r.source_id for r in results]
            self.assertTrue(any("普通笔记" in i for i in ids), ids)
            self.assertFalse(any("ent_1.md" in i for i in ids), ids)

    def test_reverse_injection_removing_guard_makes_card_ingested(self):
        """把守卫短路后卡片必须被摄入——证明这条测试真的在守它。"""
        import tempfile
        import unittest.mock

        from mimir_v8.collectors import vault as vault_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_vault(root)
            with unittest.mock.patch.object(vault_mod, "_is_mimir_generated", lambda *_: False):
                results = vault_mod.VaultCollector(vault_root=root).collect()
                self.assertTrue(any("ent_1.md" in r.source_id for r in results))

    def test_quoted_marker_variant_also_skipped(self):
        """容忍 YAML 引号形态 mimir_generated: "true"。"""
        import tempfile

        from mimir_v8.collectors.vault import _is_mimir_generated

        self.assertTrue(_is_mimir_generated('---\nmimir_generated: "true"\n---\n'))
        self.assertTrue(_is_mimir_generated("---\nmimir_generated: true  \n---\n"))
        self.assertFalse(_is_mimir_generated("---\ntitle: x\n---\n"))
        self.assertFalse(_is_mimir_generated("no frontmatter at all"))
        self.assertFalse(_is_mimir_generated("---\nmimir_generated: false\n---\n"))

    def test_guard_lives_before_content_read(self):
        """守卫读的是 4KB frontmatter 头，正文截断读不走守卫。"""
        import mimir_v8.collectors.vault as vault_mod
        import inspect

        src = inspect.getsource(vault_mod.VaultCollector.collect)
        first_read = src.index("note.read_text")
        guard = src.index("_is_mimir_generated")
        self.assertLess(first_read, guard)
        # 第一次读取只取 frontmatter 头（截断后的正文可能撕掉标记）
        head_slice = src[first_read:first_read + 300]
        self.assertIn("_FRONTMATTER_HEAD_BYTES", head_slice)


if __name__ == "__main__":
    unittest.main()
