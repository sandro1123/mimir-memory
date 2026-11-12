# -*- coding: utf-8 -*-
"""Issue: stage-2 Task 1 — the publication path allowlist must match the real vault.

``_ALLOWED_ROOTS`` was written against a directory layout the real vault does
not use (``00-系统``/``30-知识``/``90-机器收件箱`` ghost roots vs the real
``005-系统设计``/``30-知识库``/``90-机器收件箱``). The fix moves the allowlist
into a declarative YAML table (same discipline as ``sources.yaml`` /
``vault_roles.yaml``) and validates the real roots.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.publication import PublicationPolicyError, load_publication_targets


class TestPublicationTargets(unittest.TestCase):
    def test_real_vault_roots_are_allowed(self):
        """白名单必须与真实库目录对得上（§7.6 缺陷 1）。"""
        from mimir_v8.publication import PublicationService

        for root in ("005-系统设计", "30-知识库", "90-机器收件箱", "10-项目"):
            path = PublicationService._validate_path(f"{root}/某页.md")
            self.assertEqual(path, f"{root}/某页.md")

    def test_sensitive_root_still_forbidden(self):
        from mimir_v8.publication import PublicationService

        with self.assertRaises(PublicationPolicyError):
            PublicationService._validate_path("60-敏感信息/API 密钥管理.md")

    def test_old_ghost_roots_are_rejected(self):
        """旧白名单里的 00-系统 / 30-知识 是照别的库写的，必须拒。"""
        from mimir_v8.publication import PublicationService

        for root in ("00-系统", "30-知识"):
            with self.assertRaises(PublicationPolicyError):
                PublicationService._validate_path(f"{root}/x.md")

    def test_loader_returns_expected_shape(self):
        targets = load_publication_targets()
        self.assertIn("005-系统设计", targets["allowed_roots"])
        self.assertIn("60-敏感信息", targets["forbidden_roots"])
        self.assertEqual(targets["card_root"], "90-机器收件箱/Mímir 卡片")

    def test_traversal_is_rejected(self):
        from mimir_v8.publication import PublicationService

        with self.assertRaises(PublicationPolicyError):
            PublicationService._validate_path("30-知识库/../60-敏感信息/x.md")

    def test_non_markdown_is_rejected(self):
        from mimir_v8.publication import PublicationService

        with self.assertRaises(PublicationPolicyError):
            PublicationService._validate_path("30-知识库/notes.txt")

    def test_yaml_file_is_registered_in_version_sweep(self):
        from scripts.version_sweep import REGISTRY

        self.assertTrue(
            any("publication_targets" in entry.relpath for entry in REGISTRY),
            "publication_targets.yaml must be registered in version_sweep REGISTRY",
        )


if __name__ == "__main__":
    unittest.main()
