# -*- coding: utf-8 -*-
"""Stage-2 Task 3 — the entity card renderer.

Cards are the only thing Mímir writes into the vault. Two invariants:

1. The frontmatter carries the self-ingest marker (``mimir_generated: true``
   + ``mimir_id``) so the collector can skip them (anti-feedback-loop, §7.2).
2. The human content hash is stable across card updates — the card body is
   entirely a managed section, so refreshing content must never trip the
   human-content conflict check.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.vault_card import card_filename, render_card


class TestRenderCard(unittest.TestCase):
    def test_card_carries_self_ingest_marker(self):
        text = render_card(mimir_id="ent_123", title="某模型", sections={"mimir-card": "正文"})
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("mimir_generated: true", text)
        self.assertIn("mimir_id: ent_123", text)

    def test_card_managed_block_is_round_trippable(self):
        from mimir_v8.publication import human_content_hash

        a = render_card(mimir_id="ent_1", title="T", sections={"mimir-card": "第一版"})
        b = render_card(mimir_id="ent_1", title="T", sections={"mimir-card": "第二版"})
        self.assertEqual(human_content_hash(a), human_content_hash(b))

    def test_card_filename_is_stable_and_path_safe(self):
        self.assertEqual(card_filename("ent_1", "某模型 / 带斜杠"), "ent_1.md")
        self.assertEqual(card_filename("ent_1", "另一标题"), "ent_1.md")

    def test_card_managed_block_markers_present(self):
        text = render_card(mimir_id="e", title="T", sections={"mimir-card": "x"})
        self.assertIn("<!-- mimir:managed:start name=mimir-card -->", text)
        self.assertIn("<!-- mimir:managed:end name=mimir-card -->", text)

    def test_multiple_sections_render_in_order(self):
        text = render_card(mimir_id="e", title="T", sections={"alpha": "1", "beta": "2"})
        self.assertLess(text.index("name=alpha"), text.index("name=beta"))

    def test_invalid_section_name_is_rejected(self):
        from mimir_v8.vault_card import CardRenderError

        with self.assertRaises(CardRenderError):
            render_card(mimir_id="e", title="T", sections={"Bad Name": "x"})

    def test_filename_rejects_mimir_id_with_slash(self):
        from mimir_v8.vault_card import CardRenderError

        with self.assertRaises(CardRenderError):
            card_filename("../escape", "T")


if __name__ == "__main__":
    unittest.main()
