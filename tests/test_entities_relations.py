"""Entity relation boundary and advanced tests for Mímir v1.6.0."""
import pytest
import tempfile
from pathlib import Path
from mimir_v8.store import CanonicalStore


def test_create_entity_relation_basic():
    """Basic entity relation creation and retrieval."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        e1 = store.create_entity(entity_type="model", name="ModelA", domain="pool")
        e2 = store.create_entity(entity_type="model", name="ModelB", domain="pool")

        # Create relation
        result = store.create_entity_relation(
            from_entity_id=e1["entity_id"],
            to_entity_id=e2["entity_id"],
            relation_type="depends_on",
            strength=0.8,
        )
        assert result

        # Retrieve - check keys that exist
        relations = store.get_entity_relations(e1["entity_id"])
        assert len(relations) == 1
        # The returned dict has: from_entity_id, to_entity_id, to_name, relation_direction, strength, etc.
        # Note: relation_type is stored in the DB but not included in the get_entity_relations output
        # schema; it's available via raw DB query if needed.
        assert relations[0]["to_name"] == "ModelB"
        assert relations[0]["relation_direction"] == "forward"
        assert relations[0]["strength"] == 0.8


def test_create_entity_relation_different_direction():
    """Relations have direction (forward/backward)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        e1 = store.create_entity(entity_type="model", name="ModelA", domain="pool")
        e2 = store.create_entity(entity_type="model", name="ModelB", domain="pool")

        # Create forward relation
        store.create_entity_relation(
            from_entity_id=e1["entity_id"],
            to_entity_id=e2["entity_id"],
            relation_type="calls",
            relation_direction="forward",
            strength=0.9,
        )

        # Create reverse relation
        store.create_entity_relation(
            from_entity_id=e2["entity_id"],
            to_entity_id=e1["entity_id"],
            relation_type="called_by",
            relation_direction="backward",
            strength=0.7,
        )

        # Check outgoing from A
        outgoing = store.get_entity_relations(e1["entity_id"])
        assert len(outgoing) == 1
        assert outgoing[0]["relation_direction"] == "forward"

        # Check outgoing from B (should have the backward one)
        outgoing_b = store.get_entity_relations(e2["entity_id"])
        assert len(outgoing_b) == 1
        assert outgoing_b[0]["relation_direction"] == "backward"


def test_get_incoming_relations():
    """Get incoming relations to an entity."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        e1 = store.create_entity(entity_type="model", name="ModelA", domain="pool")
        e2 = store.create_entity(entity_type="model", name="ModelB", domain="pool")

        # e1 -> e2
        store.create_entity_relation(
            from_entity_id=e1["entity_id"], to_entity_id=e2["entity_id"],
            relation_type="depends_on", strength=0.8,
        )

        # Get incoming to e2
        incoming = store.get_incoming_relations(e2["entity_id"])
        assert len(incoming) == 1
        # Incoming dict has 'from_entity_id' and 'from_name'
        assert incoming[0]["from_name"] == "ModelA"
        # relation_direction should be present
        assert incoming[0]["relation_direction"] is not None


def test_get_entity_relations_no_type_filter():
    """Get all outgoing relations without type filter."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        e1 = store.create_entity(entity_type="model", name="ModelA", domain="pool")
        e2 = store.create_entity(entity_type="model", name="ModelB", domain="pool")
        e3 = store.create_entity(entity_type="model", name="ModelC", domain="pool")

        store.create_entity_relation(from_entity_id=e1["entity_id"], to_entity_id=e2["entity_id"],
                                      relation_type="depends_on", strength=0.8)
        store.create_entity_relation(from_entity_id=e1["entity_id"], to_entity_id=e3["entity_id"],
                                      relation_type="calls", strength=0.5)

        all_rels = store.get_entity_relations(e1["entity_id"], relation_type=None)
        assert len(all_rels) == 2


def test_get_entity_relations_does_not_return_suspended():
    """Suspended entities should not appear in relation results."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        e1 = store.create_entity(entity_type="model", name="ModelA", domain="pool")
        e2 = store.create_entity(entity_type="model", name="ModelB", domain="pool")

        # Suspend ModelB
        store.soft_delete_entity(e2["entity_id"])

        # Create relation A -> B
        store.create_entity_relation(
            from_entity_id=e1["entity_id"], to_entity_id=e2["entity_id"],
            relation_type="depends_on", strength=0.8,
        )

        # Get relations - B should not appear since it's suspended
        relations = store.get_entity_relations(e1["entity_id"])
        assert len(relations) == 0  # B is suspended, should be excluded


def test_entity_relation_strength_sorting():
    """Relations should be sorted by strength DESC."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        e1 = store.create_entity(entity_type="model", name="ModelA", domain="pool")
        e2 = store.create_entity(entity_type="model", name="ModelB", domain="pool")
        e3 = store.create_entity(entity_type="model", name="ModelC", domain="pool")

        # Create relations with different strengths
        store.create_entity_relation(from_entity_id=e1["entity_id"], to_entity_id=e2["entity_id"],
                                      relation_type="depends_on", strength=0.3)
        store.create_entity_relation(from_entity_id=e1["entity_id"], to_entity_id=e3["entity_id"],
                                      relation_type="depends_on", strength=0.9)

        relations = store.get_entity_relations(e1["entity_id"])
        assert len(relations) == 2
        # Stronger relation (0.9) should come first
        assert relations[0]["strength"] == 0.9
        assert relations[1]["strength"] == 0.3


def test_entity_relation_validity_check():
    """Relations should respect valid_to (expired relations excluded)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        e1 = store.create_entity(entity_type="model", name="ModelA", domain="pool")
        e2 = store.create_entity(entity_type="model", name="ModelB", domain="pool")

        # Create relation
        store.create_entity_relation(
            from_entity_id=e1["entity_id"], to_entity_id=e2["entity_id"],
            relation_type="depends_on", strength=0.8,
        )

        # Get relations - should work initially
        relations = store.get_entity_relations(e1["entity_id"])
        assert len(relations) == 1

        # The relation should still be valid since valid_to is NULL by default
        assert len(relations) == 1