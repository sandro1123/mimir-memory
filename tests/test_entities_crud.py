"""CRUD tests for Mímir v1.6.0 entity knowledge base.

Tests the entity create, read, update, and soft-delete operations.
"""
import pytest
import tempfile
from pathlib import Path
from mimir_v8.store import CanonicalStore
from mimir_v8.schema import CreateFact


def test_create_entity_basic():
    """Basic entity creation and retrieval."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        result = store.create_entity(
            entity_type="model",
            name="Nemotron-3-Ultra",
            domain="free_model_pool",
            description="NVIDIA的免费模型",
            metadata={"context_window": 128000, "cost_per_token": 0},
        )
        assert result["entity_id"] is not None
        assert result["status"] == "active"

        entity = store.get_entity(result["entity_id"])
        assert entity is not None
        assert entity["name"] == "Nemotron-3-Ultra"
        assert entity["domain"] == "free_model_pool"
        assert entity["entity_type"] == "model"
        assert entity["sensitivity"] == "internal"
        assert entity["visibility"] == "all"
        assert entity["status"] == "active"
        assert entity["description"] == "NVIDIA的免费模型"
        assert entity["metadata_json"] == '{"context_window": 128000, "cost_per_token": 0}'


def test_create_entity_minimal():
    """Entity creation with only required fields."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        result = store.create_entity(
            entity_type="concept",
            name="注意力机制",
            domain="knowledge",
        )
        assert result["entity_id"] is not None

        entity = store.get_entity(result["entity_id"])
        assert entity is not None
        assert entity["name"] == "注意力机制"
        assert entity["domain"] == "knowledge"
        assert entity["entity_type"] == "concept"
        assert entity["status"] == "active"
        assert entity["sensitivity"] == "internal"
        assert entity["visibility"] == "all"


def test_update_entity_partial():
    """Update only some entity fields."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        result = store.create_entity(
            entity_type="model",
            name="Test-Model",
            domain="test",
            description="Original description",
        )

        updated = store.update_entity(
            result["entity_id"],
            status="deprecated",
        )
        assert updated

        entity = store.get_entity(result["entity_id"])
        assert entity is not None
        assert entity["status"] == "deprecated"
        # Other fields should remain unchanged
        assert entity["description"] == "Original description"
        assert entity["name"] == "Test-Model"


def test_update_entity_multiple_fields():
    """Update multiple entity fields at once."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        result = store.create_entity(
            entity_type="model",
            name="Test-Model",
            domain="test",
        )

        updated = store.update_entity(
            result["entity_id"],
            status="active",
            description="New description",
            sensitivity="restricted",
        )
        assert updated

        entity = store.get_entity(result["entity_id"])
        assert entity is not None
        assert entity["status"] == "active"
        assert entity["description"] == "New description"
        assert entity["sensitivity"] == "restricted"


def test_soft_delete_entity():
    """Soft delete entity: sets status='deleted' and valid_to=now."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        result = store.create_entity(
            entity_type="model",
            name="To-Be-Deleted",
            domain="test",
        )

        deleted = store.soft_delete_entity(result["entity_id"])
        assert deleted

        # After soft delete, get_entity should return None
        # because valid_to is set to now, and current time > valid_to
        entity = store.get_entity(result["entity_id"])
        assert entity is None

        # But the data is still in the DB - we can check directly
        conn = store.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT status, valid_to FROM entities WHERE entity_id = ?", (result["entity_id"],))
        row = cursor.fetchone()
        assert row[0] == "deleted"
        assert row[1] is not None  # valid_to should be set
        conn.close()


def test_update_non_existent_entity():
    """Updating an entity that doesn't exist should return False."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        updated = store.update_entity(
            "non-existent-id",
            status="active",
        )
        assert not updated


def test_soft_delete_non_existent_entity():
    """Soft deleting a non-existent entity should return False."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CanonicalStore(root / "canon.db")
        store.migrate_schema_21_to_22()

        deleted = store.soft_delete_entity("non-existent-id")
        assert not deleted