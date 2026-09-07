"""Vault knowledge-doc routing tests (v12.2, task #41-A).

Real-system probe (2026-09-02) after the first vault harvest: the 413
harvested notes landed in conversation_sources/conversation_messages
with source_category='knowledge_doc', but /v8/query only serves the
facts projection — the wiki knowledge layer (knowledge_items via
KnowledgeService.create_item, SourceRouter's knowledge_doc→wiki route)
was never fed. The SourceRouter route was built in v11 and the search
side (KnowledgeLayerSearch, /v9/search-preview) was built too — the
missing link is the collector: the vault branch of collect_all only
walks the conversation pipeline and never calls create_item.

These tests pin the wiring:

- after collect_all over a vault source, knowledge_items contains a
  wiki-layer item per note (source_category knowledge_doc);
- the item is discoverable through the wiki knowledge search
  (KnowledgeLayerSearch), which is what /v9/search-preview serves;
- conversation-source archival still happens (dual routing, not a
  replacement) so idempotent replays keep working;
- per-source exclude_dirs keeps sensitive directories out of the
  wiki layer too.

They run RED on v12.1.4.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.knowledge import (
    KnowledgeLayerSearch,
    KnowledgeService,
    UnifiedSearchRequest,
)
from mimir_v8.store import CanonicalStore
from mimir_v8.worker import collect_all


def _write_config(path: Path, body: dict) -> Path:
    import yaml

    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _make_vault(root: Path) -> None:
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "qt-audit.md").write_text(
        "# QuantStar 全链路审计报告\n\n8 层递进审计模型，发现 13 个问题。",
        encoding="utf-8",
    )
    (root / "notes" / "memory-risk.md").write_text(
        "# 共享记忆池的风险\n\n多个 agent 共享记忆池有隔离风险。",
        encoding="utf-8",
    )
    (root / "60-敏感信息").mkdir()
    (root / "60-敏感信息" / "creds.md").write_text("password: hunter2", encoding="utf-8")


class TestVaultKnowledgeRouting(unittest.TestCase):
    def _run(self, tmp: str, sources: list[dict] | None = None) -> tuple[CanonicalStore, dict]:
        vault = Path(tmp) / "vault"
        _make_vault(vault)
        cfg = _write_config(
            Path(tmp) / "mimir_config.yaml",
            {
                "collector": {
                    "sources": sources
                    or [
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
        return store, results

    def test_vault_notes_land_in_wiki_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, results = self._run(tmp)
            with contextlib.closing(store.connect()) as c:
                rows = c.execute(
                    "SELECT layer, item_type, status, title, source_category"
                    " FROM knowledge_items"
                ).fetchall()
            self.assertTrue(
                rows, "knowledge_items empty — vault notes never reached the wiki layer"
            )
            for row in rows:
                self.assertEqual(row["layer"], "wiki")
                self.assertEqual(row["item_type"], "wiki_document")
                self.assertEqual(row["source_category"], "knowledge_doc")
            titles = {row["title"] for row in rows}
            self.assertIn("qt-audit", titles)
            self.assertIn("memory-risk", titles)
            # review status is searchable and needs no admin gating
            self.assertTrue(all(row["status"] in ("active", "review") for row in rows))

    def test_vault_notes_discoverable_via_wiki_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._run(tmp)
            knowledge = KnowledgeService(store)
            searcher = KnowledgeLayerSearch(store, knowledge, layer="wiki")
            request = UnifiedSearchRequest(
                text="全链路审计",
                principal_id="mentor",
                is_admin=True,
            )
            found = searcher.search(request, candidate_limit=10)
            self.assertTrue(
                found, "wiki-layer search cannot find the harvested vault note"
            )
            joined = " ".join(item["title"] for item in found)
            self.assertIn("qt-audit", joined)

    def test_conversation_archival_still_happens(self):
        # Dual routing: the knowledge layer is fed AND the conversation
        # pipeline keeps its archival row (idempotent replays of the same
        # note must keep deduplicating on the conversation side).
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._run(tmp)
            with contextlib.closing(store.connect()) as c:
                vault_rows = c.execute(
                    "SELECT COUNT(*) FROM conversation_sources"
                    " WHERE connector_type='vault'"
                ).fetchone()[0]
            self.assertEqual(vault_rows, 2)

    def test_sensitive_dirs_never_reach_wiki_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._run(tmp)
            with contextlib.closing(store.connect()) as c:
                leak = c.execute(
                    "SELECT COUNT(*) FROM knowledge_items"
                    " WHERE title='creds' OR content LIKE '%hunter2%'"
                ).fetchone()[0]
            self.assertEqual(leak, 0)

    def test_second_run_is_idempotent_on_both_pipelines(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault_root_holder = {}
            vault = Path(tmp) / "vault"
            _make_vault(vault)
            vault_root_holder["root"] = vault
            cfg = _write_config(
                Path(tmp) / "mimir_config.yaml",
                {"collector": {"sources": [{"name": "vault", "type": "vault"}]}},
            )
            store = CanonicalStore(Path(tmp) / "canonical.db")
            first = collect_all(
                store,
                actor_principal="service:test_runner",
                config_path=cfg,
                vault_root=vault,
            )
            with contextlib.closing(store.connect()) as c:
                ki_first = c.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
            second = collect_all(
                store,
                actor_principal="service:test_runner",
                config_path=cfg,
                vault_root=vault,
            )
            with contextlib.closing(store.connect()) as c:
                ki_second = c.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
            # This config carries no exclude_dirs, so all three notes
            # (including creds.md) legitimately land on the first run.
            self.assertEqual(ki_first, 3)
            # The rerun must add nothing: same notes, same mtime, both
            # pipelines (conversation replay + wiki dedup) hold.
            self.assertEqual(
                ki_second, ki_first,
                f"knowledge_items grew on rerun: {ki_first} -> {ki_second}",
            )
            self.assertEqual(
                sum(e.get("wiki_ingested", 0) for e in second["vault"]), 0,
                "wiki_ingested must be 0 on a rerun (honest new-item counter)",
            )
            self.assertEqual(
                len(second["vault"]), len(first["vault"]),
                "conversation-side result shape changed between runs",
            )


if __name__ == "__main__":
    unittest.main()
