"""Isolated, rebuildable vector projection for Mímir v8."""

from __future__ import annotations

from collections.abc import Callable


class VectorProjectionError(RuntimeError):
    """Raised when vector projection configuration is unsafe."""


VECTOR_COLLECTION_PREFIXES = (
    "mimir_v8_shadow_",
    "mimir_v8_prod_",
    "mimir_v9_shadow_",
    "mimir_v9_prod_",
    "mimir_v10_shadow_",
    "mimir_v10_prod_",
)
VECTOR_PRODUCTION_PREFIXES = ("mimir_v8_prod_", "mimir_v9_prod_", "mimir_v10_prod_")


def validate_vector_collection_name(collection_name: str) -> str:
    if collection_name == "mimir_facts" or not collection_name.startswith(VECTOR_COLLECTION_PREFIXES):
        raise VectorProjectionError(
            "vector collection must use a namespaced v8/v9 shadow or production prefix"
        )
    return "production" if collection_name.startswith(VECTOR_PRODUCTION_PREFIXES) else "staging"


class VectorProjector:
    name = "vector"

    def __init__(self, collection, embedder: Callable[[str], list[float]], *, collection_name: str):
        validate_vector_collection_name(collection_name)
        self.collection = collection
        self.embedder = embedder
        self.collection_name = collection_name

    def apply(self, event, fact: dict) -> None:
        fact_id = fact["fact_id"]
        if fact["status"] != "active":
            self._delete(fact_id)
            return
        existing = self.collection.get(ids=[fact_id], include=["metadatas"])
        metadatas = existing.get("metadatas") or []
        if metadatas:
            metadata = metadatas[0] or {}
            if (
                int(metadata.get("version", 0)) == int(fact["current_version"])
                and metadata.get("content_hash") == fact["content_hash"]
            ):
                return
        embedding = self.embedder(fact["content"])
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        if not isinstance(embedding, list) or not embedding:
            raise VectorProjectionError("embedder returned an empty or invalid vector")
        metadata = {
            "version": int(fact["current_version"]),
            "content_hash": fact["content_hash"],
            "owner_principal": fact["owner_principal"],
            "domain": fact["domain"],
            "fact_type": fact["fact_type"],
            "status": fact["status"],
            "source_event_seq": int(event["event_seq"]),
        }
        if fact.get("project_id"):
            metadata["project_id"] = fact["project_id"]
        self.collection.upsert(
            ids=[fact_id], embeddings=[embedding], documents=[fact["content"]],
            metadatas=[metadata],
        )

    def _delete(self, fact_id: str) -> None:
        existing = self.collection.get(ids=[fact_id], include=[])
        if existing.get("ids"):
            self.collection.delete(ids=[fact_id])

    def count(self) -> int:
        return int(self.collection.count())

    def ids(self) -> set[str]:
        count = self.count()
        if not count:
            return set()
        return set(self.collection.get(limit=count, include=[]).get("ids") or [])
