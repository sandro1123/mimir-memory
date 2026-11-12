# -*- coding: utf-8 -*-
"""Stage-2 Task 5 — publish_entity orchestration.

Order is fixed: eligibility → target path → git snapshot → render →
register (first time) → request publication → write file → apply.
The vault for tests is a throwaway git repo so snapshots are real.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.publication import VAULT_ADAPTER_PRINCIPAL
from mimir_v8.store import CanonicalStore
from mimir_v8.vault_git import _clean_git_env

CARD_ROOT = "90-机器收件箱/Mímir 卡片"


def _fact(**over):
    base = {
        "sensitivity": "internal",
        "visibility": "shared",
        "egress_policy": "external_allowed",
    }
    base.update(over)
    return base


def _git_init(root: Path) -> None:
    env = _clean_git_env()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, env=env)


class _Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.vault = root / "vault"
        self.vault.mkdir()
        _git_init(self.vault)
        (self.vault / ".gitignore").write_text(".stfolder/\n", encoding="utf-8")
        self.store = CanonicalStore(root / "canonical.db")
        self.fact = _fact()
        self.entity = {"entity_id": "ent_1", "name": "某模型", "entity_type": "tool_card"}
        self.addCleanup(self._tmp.cleanup)


class TestPublishEntity(_Env):
    def _publish(self, **kw):
        from mimir_v8.vault_publish import publish_entity

        params = dict(
            vault_root=self.vault,
            store=self.store,
            fact=self.fact,
            entity=self.entity,
            actor_principal=VAULT_ADAPTER_PRINCIPAL,
            sections={"mimir-card": "正文 v1"},
        )
        params.update(kw)
        return publish_entity(**params)

    def test_publish_writes_new_card_under_card_root(self):
        result = self._publish()
        self.assertEqual(result["status"], "published")
        written = (self.vault / result["path"]).read_text(encoding="utf-8")
        self.assertIn("mimir_generated: true", written)
        self.assertTrue(result["path"].startswith(CARD_ROOT))

    def test_publish_is_idempotent(self):
        first = self._publish()
        second = self._publish(sections={"mimir-card": "正文 v2"})
        self.assertEqual(first["path"], second["path"])
        text = (self.vault / first["path"]).read_text(encoding="utf-8")
        self.assertEqual(text.count("mimir:managed:start"), 1)
        self.assertIn("正文 v2", text)

    def test_publish_refuses_local_only_fact(self):
        import shutil

        # 独立 vault：目录里不能有其他测试写下的卡
        vault2 = self.vault.parent / "vault2"
        vault2.mkdir()
        _git_init(vault2)
        from mimir_v8.vault_publish import publish_entity

        result = publish_entity(
            vault_root=vault2,
            store=self.store,
            fact=_fact(egress_policy="local_only"),
            entity=self.entity,
            actor_principal=VAULT_ADAPTER_PRINCIPAL,
            sections={"mimir-card": "x"},
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "egress:local_only")
        self.assertFalse((vault2 / CARD_ROOT).exists())

    def test_publish_never_touches_human_content(self):
        first = self._publish()
        path = self.vault / first["path"]
        path.write_text(path.read_text(encoding="utf-8") + "\n\n人写的一行\n", encoding="utf-8")
        result = self._publish(sections={"mimir-card": "正文 v3"})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "human_content_changed")
        self.assertIn("人写的一行", path.read_text(encoding="utf-8"))

    def test_publish_snapshots_before_write(self):
        """人先 git commit（文件未变）→ 再发布 v9：human hash 未变，应成功；
        git 里留下人编辑的快照，可回滚。"""
        self._publish()
        path = self.vault / "90-机器收件箱/Mímir 卡片/ent_1.md"
        env = _clean_git_env()
        subprocess.run(["git", "add", "-A"], cwd=self.vault, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "human edit"],
            cwd=self.vault,
            check=True,
            env=env,
        )
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.vault, capture_output=True, text=True, env=env
        ).stdout.strip()
        result = self._publish(sections={"mimir-card": "正文 v9"})
        self.assertEqual(result["status"], "published")
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.vault, capture_output=True, text=True, env=env
        ).stdout.strip()
        self.assertNotEqual(head_before, head_after)

    def test_publish_rejects_wrong_actor(self):
        result = self._publish(actor_principal="jarvis")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "actor:not_vault_adapter")


if __name__ == "__main__":
    unittest.main()
