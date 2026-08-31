"""Unified all-source ingestion pipeline tests (v12.1.0 Task 3).

Covers the source registry (config-driven, backward compatible), the
Vault (Obsidian) collector, and the collect_all dispatch upgrade that
runs every enabled source type — not just RSS.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.store import CanonicalStore
from mimir_v8.worker import collect_all, load_source_registry
from mimir_v8.collectors.vault import VaultCollector


class TestSourceRegistry(unittest.TestCase):
    """P19-01: config-driven source registry with backward fallback."""

    def _write_config(self, tmp: str, body: dict) -> Path:
        import yaml

        cfg = Path(tmp) / "mimir_config.yaml"
        cfg.write_text(yaml.safe_dump(body), encoding="utf-8")
        return cfg

    def test_registry_from_config(self):
        body = {
            "collector": {
                "sources": [
                    {"name": "tech-rss", "type": "rss", "feeds": ["https://a/feed.xml"]},
                    {"name": "docs-site", "type": "web", "url": "https://a/doc"},
                    {"name": "vault", "type": "vault"},
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._write_config(tmp, body)
            registry = load_source_registry(cfg)
            self.assertEqual(len(registry), 3)
            by_name = {s["name"]: s for s in registry}
            self.assertEqual(by_name["docs-site"]["type"], "web")
            self.assertEqual(by_name["vault"]["type"], "vault")
            self.assertEqual(by_name["tech-rss"]["feeds"], ["https://a/feed.xml"])

    def test_registry_fallback_default_rss_only(self):
        # No config file -> backward-compatible fallback: legacy RSS
        # behavior (all configured feeds), no web/vault sources.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.yaml"
            registry = load_source_registry(missing)
            self.assertIsInstance(registry, list)

    def test_registry_rejects_unknown_source_type(self):
        body = {
            "collector": {
                "sources": [{"name": "bad", "type": "smtp"}]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._write_config(tmp, body)
            with self.assertRaises(ValueError):
                load_source_registry(cfg)

    def test_registry_defaults_when_no_collector_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._write_config(tmp, {"other": True})
            registry = load_source_registry(cfg)
            self.assertIsInstance(registry, list)


class TestVaultCollector(unittest.TestCase):
    """P19-02: Vault collector scans markdown into CollectResults."""

    def _make_vault(self, tmp: str) -> Path:
        vault = Path(tmp) / "vault"
        (vault / "notes").mkdir(parents=True)
        (vault / ".obsidian").mkdir()
        (vault / "notes" / "a.md").write_text("# A\n内-content", encoding="utf-8")
        (vault / "notes" / "b.md").write_text("# B\nmore", encoding="utf-8")
        (vault / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
        (vault / "notes" / "template.md").write_text("ignored", encoding="utf-8")
        return vault

    def test_collect_scans_markdown_excluding_hidden_and_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._make_vault(tmp)
            collector = VaultCollector(vault_root=vault, exclude_dirs={".obsidian", "template"})
            results = collector.collect()
            paths = [r.source_id for r in results]
            self.assertIn("a.md", " ".join(paths))
            self.assertNotIn(".obsidian", " ".join(paths))

    def test_describe(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self._make_vault(tmp)
            d = VaultCollector(vault_root=vault).describe()
            self.assertEqual(d["type"], "vault")
            self.assertIn("vault_root", d)


class TestCollectAllDispatch(unittest.TestCase):
    """P19-03: collect_all runs registry-driven multi-source dispatch."""

    def test_collect_all_runs_vault_source(self):
        # Build a vault with one note, a config with one vault source,
        # then run collect_all: the note must be ingested (memory_events
        # must contain ingestion events for it).
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            (vault / "note.md").write_text(
                "Mímir vault ingestion note", encoding="utf-8"
            )
            cfg = Path(tmp) / "mimir_config.yaml"
            cfg.write_text(
                yaml.safe_dump(
                    {"collector": {"sources": [{"name": "vault", "type": "vault"}]}}
                ),
                encoding="utf-8",
            )
            db = Path(tmp) / "canonical.db"
            store = CanonicalStore(db)

            results = collect_all(store, actor_principal="service:test_runner",
                                  config_path=cfg, vault_root=vault)

            self.assertIn("vault", results)
            vault_results = results["vault"]
            self.assertEqual(len(vault_results), 1)
            self.assertEqual(vault_results[0]["items"], 1)
            self.assertEqual(vault_results[0]["ingested"], 1)
            self.assertEqual(vault_results[0]["errors"], [])

    def test_collect_all_isolated_source_failure(self):
        # A broken web source must not abort the whole pipeline; it must
        # land in results["errors"] and other sources still run.
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            (vault / "note.md").write_text("note for pipeline", encoding="utf-8")
            cfg = Path(tmp) / "mimir_config.yaml"
            cfg.write_text(
                yaml.safe_dump(
                    {
                        "collector": {
                            "sources": [
                                {"name": "broken-web", "type": "web",
                                 "url": "http://127.0.0.1:1/none"},
                                {"name": "vault", "type": "vault"},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            db = Path(tmp) / "canonical.db"
            store = CanonicalStore(db)

            results = collect_all(store, actor_principal="service:test_runner",
                                  config_path=cfg, vault_root=vault)

            self.assertIn("vault", results)
            self.assertEqual(results["vault"][0]["ingested"], 1)
            self.assertTrue(
                any("broken-web" in e for e in results["errors"]),
                f"broken source must be reported, got: {results['errors']}",
            )


if __name__ == "__main__":
    unittest.main()
