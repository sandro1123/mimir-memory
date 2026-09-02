"""Collector wiring tests (v12.1.3, task #40).

Before this patch the registry-driven pipeline existed but was only
half-wired for production use:

- ``classifier.SOURCE_CATEGORY_MAP`` had no "vault" key, so every vault
  note ingested through collect_all landed in ``unknown/quarantine``
  instead of ``knowledge_doc`` — quarantined data no downstream consumer
  can use.
- ``collect_all`` ignored the per-source ``exclude_dirs`` config, so a
  production vault with a plaintext-credentials directory had no way to
  keep it out of the first harvest.
- the web source idempotency key was ``web:<sha256(url)>`` — a URL
  whose page content changes makes the second run raise
  ConflictError ("idempotency key reused with different content") and
  the source lands in results["errors"] on every run after an update.

These tests pin all three wirings. They run RED on v12.1.2.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.classifier import classify, is_knowledge_doc
from mimir_v8.store import CanonicalStore
from mimir_v8.worker import collect_all


def _write_config(path: Path, body: dict) -> Path:
    import yaml

    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


class TestVaultClassification(unittest.TestCase):
    """Vault is a local knowledge document, not unknown/quarantine."""

    def test_vault_maps_to_knowledge_doc(self):
        self.assertEqual(classify("vault"), "knowledge_doc")

    def test_vault_counts_as_knowledge_doc(self):
        self.assertTrue(is_knowledge_doc("vault"))

    def test_vault_is_not_extraction_eligible(self):
        # knowledge_doc must never pass the conversation extraction
        # gate — vault content stays local, it is not shipped to the
        # LLM extraction worker.
        from mimir_v8.classifier import is_conversation

        self.assertFalse(is_conversation("vault"))


class TestCollectAllExcludeDirs(unittest.TestCase):
    """collect_all honors per-source exclude_dirs from the registry."""

    def _make_vault(self, root: Path) -> None:
        (root / "notes").mkdir(parents=True)
        (root / "notes" / "safe.md").write_text("safe note", encoding="utf-8")
        (root / "60-敏感信息").mkdir()
        (root / "60-敏感信息" / "creds.md").write_text("password: hunter2", encoding="utf-8")
        (root / ".obsidian").mkdir()
        (root / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")

    def test_exclude_dirs_keeps_sensitive_dir_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            self._make_vault(vault)
            cfg = _write_config(
                Path(tmp) / "mimir_config.yaml",
                {
                    "collector": {
                        "sources": [
                            {
                                "name": "vault",
                                "type": "vault",
                                "exclude_dirs": ["60-敏感信息"],
                            }
                        ]
                    }
                },
            )
            store = CanonicalStore(Path(tmp) / "canonical.db")

            results = collect_all(
                store,
                actor_principal="service:test_runner",
                config_path=cfg,
                vault_root=vault,
            )

            harvested = " ".join(r["path"] for r in results["vault"])
            self.assertIn("safe.md", harvested)
            self.assertNotIn("60-敏感信息", harvested)

    def test_exclude_dirs_merges_with_hidden_dir_defaults(self):
        # A config-specified exclude list must not silently drop the
        # built-in defaults: .obsidian stays excluded too.
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            self._make_vault(vault)
            cfg = _write_config(
                Path(tmp) / "mimir_config.yaml",
                {
                    "collector": {
                        "sources": [
                            {
                                "name": "vault",
                                "type": "vault",
                                "exclude_dirs": ["60-敏感信息"],
                            }
                        ]
                    }
                },
            )
            store = CanonicalStore(Path(tmp) / "canonical.db")

            results = collect_all(
                store,
                actor_principal="service:test_runner",
                config_path=cfg,
                vault_root=vault,
            )

            harvested = " ".join(r["path"] for r in results["vault"])
            self.assertNotIn(".obsidian", harvested)

    def test_vault_notes_land_as_knowledge_doc(self):
        # End to end: the ingested vault source row must carry
        # source_category='knowledge_doc', not 'unknown/quarantine'.
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            (vault / "note.md").write_text("vault e2e note", encoding="utf-8")
            cfg = _write_config(
                Path(tmp) / "mimir_config.yaml",
                {"collector": {"sources": [{"name": "vault", "type": "vault"}]}},
            )
            store = CanonicalStore(Path(tmp) / "canonical.db")

            collect_all(
                store,
                actor_principal="service:test_runner",
                config_path=cfg,
                vault_root=vault,
            )

            import contextlib

            with contextlib.closing(store.connect()) as connection:
                rows = connection.execute(
                    "SELECT connector_type, source_category FROM conversation_sources"
                ).fetchall()
            self.assertTrue(rows)
            for row in rows:
                if row["connector_type"] == "vault":
                    self.assertEqual(row["source_category"], "knowledge_doc")


class TestWebSourceIdempotency(unittest.TestCase):
    """A re-collected page whose content changed must ingest as a new
    version, not die with ConflictError."""

    def _run_web_collect(self, tmp: str, url: str, store: CanonicalStore) -> dict:
        cfg = _write_config(
            Path(tmp) / "mimir_config.yaml",
            {
                "collector": {
                    "sources": [{"name": "page", "type": "web", "url": url}]
                }
            },
        )
        return collect_all(
            store,
            actor_principal="service:test_runner",
            config_path=cfg,
        )

    def test_recollected_changed_page_ingests_new_version(self):
        # Two runs over the same URL with different content: the first
        # ingests, the second must also succeed (new version) instead of
        # raising ConflictError into results["errors"].
        import mimir_v8.collectors as collectors_pkg
        from mimir_v8.collectors.web import WebCollector

        url = "https://example.test/page"
        contents = iter(("first body", "updated body"))

        class FakeWeb(WebCollector):
            def collect_url(self, url, category="knowledge"):
                body = next(contents)
                from mimir_v8.collectors.base import CollectResult

                return CollectResult(
                    title="page", url=url, content=body, items_collected=1
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = CanonicalStore(Path(tmp) / "canonical.db")

            original = collectors_pkg.WebCollector
            collectors_pkg.WebCollector = FakeWeb
            try:
                first = self._run_web_collect(tmp, url, store)
                second = self._run_web_collect(tmp, url, store)
            finally:
                collectors_pkg.WebCollector = original

            self.assertEqual(first["web"][0]["ingested"], 1)
            self.assertEqual(first["web"][0]["errors"], [])
            # the key assertion: rerun after content change succeeds
            self.assertEqual(second["web"][0]["ingested"], 1)
            self.assertEqual(
                second["web"][0]["errors"], [],
                f"rerun must not ConflictError: {second['web'][0]}",
            )


if __name__ == "__main__":
    unittest.main()
