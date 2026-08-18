from __future__ import annotations

import contextlib
import hashlib
import tempfile
import unittest
from pathlib import Path

from mimir_v8.knowledge import (
    CreateKnowledgeItem,
    FeedbackLoop,
    KnowledgeService,
    SourceRouter,
    SourceRoutingError,
    UnifiedSearch,
    UnifiedSearchRequest,
)
from mimir_v8.schema import CreateFact, ValidationError
from mimir_v8.store import CanonicalStore, ConflictError


class FakeMemoryQuery:
    def __init__(self, store: CanonicalStore):
        self.store = store

    def search(self, request):
        results = []
        with contextlib.closing(self.store.connect()) as connection:
            rows = connection.execute("SELECT * FROM facts WHERE status='active' ORDER BY fact_id").fetchall()
        for row in rows:
            if request.domain and row["domain"] != request.domain:
                continue
            if not self.store.can_read(
                row["fact_id"], request.principal_id,
                is_admin=request.is_admin, roles=set(request.roles),
            ):
                continue
            if request.text.lower() not in (row["content"] + " " + row["summary"]).lower():
                continue
            results.append({
                "fact_id": row["fact_id"], "content": row["content"],
                "summary": row["summary"], "owner_principal": row["owner_principal"],
                "domain": row["domain"], "score": 0.5,
                "score_explanation": {"algorithm": "test-memory"},
            })
        return {"results": results[:request.limit]}


class R7Fixture:
    def __init__(self, root: Path):
        self.store = CanonicalStore(root / "canonical.db")
        self.knowledge = KnowledgeService(self.store)
        self.unified = UnifiedSearch(FakeMemoryQuery(self.store), self.knowledge)
        self.feedback = FeedbackLoop(self.store, self.knowledge)

    def item(self, *, layer="learning", owner="mentor", connector="rss", content="N100 cooling guide", key="item-1", status="active"):
        return self.knowledge.create_item(CreateKnowledgeItem(
            connector_type=connector,
            layer=layer,
            title="N100 operations",
            content=content,
            summary="N100 cooling and stability",
            owner_principal=owner,
            domain="infrastructure",
            topics=("n100", "linux"),
            status=status,
            source_hash=hashlib.sha256((connector + key).encode()).hexdigest(),
            source_uri="https://example.invalid/article",
            policy_version="v9-test",
            provenance={"untrusted_external_content": True},
            idempotency_key=key,
        ), actor_principal=owner, is_admin=status == "active")


class TestSourceRouting(unittest.TestCase):
    def test_registered_sources_route_to_governed_layers(self):
        self.assertEqual(SourceRouter.route("hermes_cdc").default_layer, "memory")
        self.assertEqual(SourceRouter.route("rss").default_layer, "learning")
        self.assertEqual(SourceRouter.route("file").default_layer, "wiki")
        self.assertEqual(SourceRouter.route("file", "learning").source_category, "knowledge_doc")

    def test_unknown_and_cross_layer_routes_fail_closed(self):
        with self.assertRaises(SourceRoutingError):
            SourceRouter.route("future_connector")
        with self.assertRaises(SourceRoutingError):
            SourceRouter.route("rss", "memory")
        with self.assertRaises(SourceRoutingError):
            SourceRouter.route("hermes_cdc", "learning")


class TestKnowledgeService(unittest.TestCase):
    def test_idempotent_create_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            first = fixture.item()
            replay = fixture.item()
            self.assertEqual(first["item_id"], replay["item_id"])
            self.assertTrue(replay["idempotent_replay"])
            with self.assertRaises(ConflictError):
                fixture.item(content="changed", key="item-1")

    def test_content_duplicate_returns_existing_item_without_duplicate_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            first = fixture.item(key="one")
            duplicate = fixture.knowledge.create_item(CreateKnowledgeItem(
                connector_type="rss", layer="learning", title="N100 operations",
                content="N100 cooling guide", summary="N100 cooling and stability",
                owner_principal="mentor", domain="infrastructure", topics=("n100", "linux"),
                status="active", source_hash=hashlib.sha256(b"rssone").hexdigest(),
                source_uri="https://example.invalid/article", policy_version="v9-test",
                provenance={"untrusted_external_content": True}, idempotency_key="two",
            ), actor_principal="mentor", is_admin=True)
            self.assertEqual(first["item_id"], duplicate["item_id"])
            self.assertTrue(duplicate["content_deduplicated"])
            with contextlib.closing(fixture.store.connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0], 1)

    def test_owner_acl_and_admin_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            item = fixture.item()
            self.assertTrue(fixture.knowledge.can_read(item["item_id"], "mentor"))
            self.assertFalse(fixture.knowledge.can_read(item["item_id"], "jarvis"))
            self.assertTrue(fixture.knowledge.can_read(item["item_id"], "sandro", is_admin=True))
            with self.assertRaises(PermissionError):
                fixture.knowledge.get_item(item["item_id"], "jarvis")

    def test_service_enforces_owner_actor_review_and_owner_scoped_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            with self.assertRaises(PermissionError):
                fixture.knowledge.create_item(CreateKnowledgeItem(
                    connector_type="rss", layer="learning", title="Private",
                    content="same private content", summary="same", owner_principal="jarvis",
                    domain="infrastructure", source_hash=hashlib.sha256(b"shared-source").hexdigest(),
                    idempotency_key="cross-owner-service", status="review",
                ), actor_principal="mentor")
            with self.assertRaises(PermissionError):
                fixture.knowledge.create_item(CreateKnowledgeItem(
                    connector_type="rss", layer="learning", title="Active bypass",
                    content="cannot activate", summary="cannot activate", owner_principal="mentor",
                    domain="infrastructure", source_hash=hashlib.sha256(b"active-bypass").hexdigest(),
                    idempotency_key="active-bypass", status="active",
                ), actor_principal="mentor")
            first = fixture.knowledge.create_item(CreateKnowledgeItem(
                connector_type="rss", layer="learning", title="Owner one",
                content="same private content", summary="same", owner_principal="mentor",
                domain="infrastructure", source_hash=hashlib.sha256(b"shared-source").hexdigest(),
                idempotency_key="owner-one", status="review",
            ), actor_principal="mentor")
            second = fixture.knowledge.create_item(CreateKnowledgeItem(
                connector_type="rss", layer="learning", title="Owner two",
                content="same private content", summary="same", owner_principal="jarvis",
                domain="infrastructure", source_hash=hashlib.sha256(b"shared-source").hexdigest(),
                idempotency_key="owner-two", status="review",
            ), actor_principal="jarvis")
            self.assertNotEqual(first["item_id"], second["item_id"])

    def test_external_secrets_are_redacted_before_hash_and_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            item = fixture.item(content="token=abcdefghijklmnop N100", key="secret")
            stored = fixture.knowledge.get_item(item["item_id"], "mentor")
            self.assertIn("[REDACTED]", stored["content"])
            self.assertNotIn("abcdefghijklmnop", stored["content"])


class TestUnifiedSearch(unittest.TestCase):
    def test_acl_filtering_precedes_rank_fusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            fixture.item(owner="mentor", key="mentor-item")
            fixture.item(owner="jarvis", key="jarvis-item", content="N100 private jarvis note")
            result = fixture.unified.search(UnifiedSearchRequest(
                text="N100", principal_id="mentor", layers=("learning",),
            ))
            self.assertEqual([row["owner_principal"] for row in result["results"]], ["mentor"])
            self.assertEqual(result["results"][0]["score_explanation"]["authorization"], "filtered_before_ranking")

    def test_three_layer_rrf_and_disabled_layer_degradation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            fixture.item(key="learn")
            fixture.item(layer="wiki", connector="file", key="wiki", content="N100 wiki runbook")
            fixture.store.create_fact(CreateFact(
                content="N100 memory rule", summary="N100 memory", owner_principal="mentor",
                domain="infrastructure", fact_type="reference", visibility="owner_only",
                idempotency_key="memory",
            ), actor_principal="mentor")
            result = fixture.unified.search(UnifiedSearchRequest(
                text="N100", principal_id="mentor",
            ))
            self.assertEqual({row["layer"] for row in result["results"]}, {"memory", "learning", "wiki"})
            self.assertTrue(all(row["score_explanation"]["fusion"] == "rrf_over_layer_local_rank" for row in result["results"]))
            degraded = UnifiedSearch(
                FakeMemoryQuery(fixture.store), fixture.knowledge, enabled_layers=("memory", "learning")
            ).search(UnifiedSearchRequest(text="N100", principal_id="mentor"))
            self.assertTrue(degraded["partial"])
            self.assertEqual(degraded["layers"]["wiki"]["status"], "disabled")
            self.assertTrue(any(row["layer"] == "memory" for row in degraded["results"]))


class TestFeedbackLoop(unittest.TestCase):
    def test_learning_useful_creates_suggestion_not_canonical_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            item = fixture.item()
            before = fixture.store.counts()["facts"]
            result = fixture.feedback.submit(
                target_layer="learning", target_id=item["item_id"], signal_type="useful",
                signal_text="worth remembering", submitted_by="mentor", idempotency_key="feedback-1",
            )
            self.assertEqual(result["suggestion"]["suggestion_type"], "remember")
            self.assertFalse(result["canonical_mutated"])
            self.assertEqual(fixture.store.counts()["facts"], before)
            replay = fixture.feedback.submit(
                target_layer="learning", target_id=item["item_id"], signal_type="useful",
                signal_text="worth remembering", submitted_by="mentor", idempotency_key="feedback-1",
            )
            self.assertTrue(replay["idempotent_replay"])

    def test_feedback_rejects_empty_submitter(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            item = fixture.item()
            with self.assertRaises(ValidationError):
                fixture.feedback.submit(
                    target_layer="learning", target_id=item["item_id"], signal_type="useful",
                    signal_text="invalid actor", submitted_by=" ", idempotency_key="empty-submitter",
                )

    def test_feedback_cannot_target_another_owners_private_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7Fixture(Path(tmp))
            item = fixture.item(owner="jarvis")
            with self.assertRaises(PermissionError):
                fixture.feedback.submit(
                    target_layer="learning", target_id=item["item_id"], signal_type="useful",
                    signal_text="steal", submitted_by="mentor", idempotency_key="forbidden",
                )


if __name__ == "__main__":
    unittest.main()
