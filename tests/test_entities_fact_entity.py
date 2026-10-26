"""Fact-Entity linking tests for Mímir v1.6.0 entity knowledge base.

Tests the link/unlink and query operations between facts and entities.
Uses 'heimdallr' as owner_principal since it's a registered agent.
"""
import pytest
import tempfile
from pathlib import Path
from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact


def test_link_fact_entity_basic():
    """Basic fact-entity linking and retrieval."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        # Create a fact with heimdallr as owner (registered agent)
        fact_result = store.create_fact(
            CreateFact(
                content='Nemotron eval report',
                summary='Nemotron eval',
                domain='knowledge',
                fact_type='reference',
                visibility='all',
                sensitivity='internal',
                human_status='confirmed',
                owner_principal='heimdallr',
            ),
            actor_principal='heimdallr'
        )
        assert fact_result["fact_id"] is not None

        # Create an entity
        entity_result = store.create_entity(
            entity_type="model",
            name="Nemotron-3-Ultra",
            domain="free_model_pool",
            description="NVIDIA的免费模型",
        )

        # Link fact to entity
        linked = store.link_fact_entity(fact_result["fact_id"], entity_result["entity_id"], role="subject")
        assert linked

        # Retrieve linked entities
        linked_entities = store.get_fact_entities(fact_result["fact_id"])
        assert len(linked_entities) == 1
        assert linked_entities[0]["name"] == "Nemotron-3-Ultra"
        assert linked_entities[0]["role"] == "subject"


def test_link_fact_entity_multiple():
    """A fact can be linked to multiple entities."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        # Create a fact with heimdallr as owner
        fact_result = store.create_fact(
            CreateFact(
                content='Model comparison report',
                summary='Model comparison',
                domain='knowledge',
                fact_type='reference',
                visibility='all',
                sensitivity='internal',
                human_status='confirmed',
                owner_principal='heimdallr',
            ),
            actor_principal='heimdallr'
        )

        # Create two entities
        entity1 = store.create_entity(
            entity_type="model",
            name="Nemotron-3-Ultra",
            domain="free_model_pool",
        )
        entity2 = store.create_entity(
            entity_type="model",
            name="Llama-3.1-8B",
            domain="free_model_pool",
        )

        # Link fact to both entities
        store.link_fact_entity(fact_result["fact_id"], entity1["entity_id"], role="subject")
        store.link_fact_entity(fact_result["fact_id"], entity2["entity_id"], role="object")

        # Retrieve linked entities
        linked_entities = store.get_fact_entities(fact_result["fact_id"])
        assert len(linked_entities) == 2

        # Check roles
        roles = {e["name"]: e["role"] for e in linked_entities}
        assert "Nemotron-3-Ultra" in roles
        assert "Llama-3.1-8B" in roles
        assert roles["Nemotron-3-Ultra"] == "subject"
        assert roles["Llama-3.1-8B"] == "object"


def test_unlink_fact_entity():
    """Remove fact-entity linking."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        # Create a fact with heimdallr as owner
        fact_result = store.create_fact(
            CreateFact(
                content='Test report',
                summary='Test',
                domain='knowledge',
                fact_type='reference',
                visibility='all',
                sensitivity='internal',
                human_status='confirmed',
                owner_principal='heimdallr',
            ),
            actor_principal='heimdallr'
        )

        # Create an entity
        entity_result = store.create_entity(
            entity_type="model",
            name="Test-Model",
            domain="test",
        )

        # Link and verify
        store.link_fact_entity(fact_result["fact_id"], entity_result["entity_id"], role="subject")
        linked = store.get_fact_entities(fact_result["fact_id"])
        assert len(linked) == 1

        # Unlink
        unlinked = store.unlink_fact_entity(fact_result["fact_id"], entity_result["entity_id"])
        assert unlinked

        # Verify unlinked
        linked_after = store.get_fact_entities(fact_result["fact_id"])
        assert len(linked_after) == 0


def test_get_fact_entities_with_role():
    """Retrieve fact entities with correct role and confidence."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        # Create a fact with heimdallr as owner
        fact_result = store.create_fact(
            CreateFact(
                content='Evaluation data',
                summary='Eval data',
                domain='knowledge',
                fact_type='reference',
                visibility='all',
                sensitivity='internal',
                human_status='confirmed',
                owner_principal='heimdallr',
            ),
            actor_principal='heimdallr'
        )

        # Create entity
        entity_result = store.create_entity(
            entity_type="model",
            name="Nemotron-3-Ultra",
            domain="free_model_pool",
        )

        # Link with specific role and confidence
        store.link_fact_entity(fact_result["fact_id"], entity_result["entity_id"], role="subject", confidence=0.95)

        # Retrieve
        linked = store.get_fact_entities(fact_result["fact_id"])
        assert len(linked) == 1
        assert linked[0]["role"] == "subject"
        assert linked[0]["confidence"] == 0.95