# -*- coding: utf-8 -*-
"""Issue #39: the embedder must fail at startup on a dimension mismatch.

Without the probe a model swap "starts fine" and then fails every projection,
which looks like a healthy service with dead recall.
"""
import unittest

from mimir_v8.runtime import RuntimeConfigurationError, _verify_embedding_dimension


class _FakeEmbedder:
    def __init__(self, dimension):
        self.dimension = dimension

    def __call__(self, text):
        return [0.0] * self.dimension


class _FakeCollection:
    def __init__(self, metadata=None, count=0):
        self.metadata = dict(metadata or {})
        self._count = count
        self.modified = None

    def count(self):
        return self._count

    def modify(self, metadata):
        self.modified = dict(metadata)
        self.metadata = dict(metadata)


class TestDimensionCheck(unittest.TestCase):
    def test_matching_dimension_starts(self):
        collection = _FakeCollection(metadata={"mimir_dim": 8})
        _verify_embedding_dimension(collection, _FakeEmbedder(8), "c")

    def test_mismatch_refuses_to_start(self):
        collection = _FakeCollection(metadata={"mimir_dim": 8})
        with self.assertRaises(RuntimeConfigurationError) as ctx:
            _verify_embedding_dimension(collection, _FakeEmbedder(16), "c")
        self.assertIn("16", str(ctx.exception))
        self.assertIn("8", str(ctx.exception))

    def test_first_run_records_dimension(self):
        collection = _FakeCollection(metadata={"hnsw:space": "cosine"})
        _verify_embedding_dimension(collection, _FakeEmbedder(384), "c")
        self.assertEqual(collection.modified["mimir_dim"], 384)
        self.assertEqual(collection.modified["hnsw:space"], "cosine")

    def test_does_not_stamp_metadata_onto_a_populated_collection(self):
        """An existing collection without the marker keeps its metadata.

        The dimension of a populated collection is implied by its vectors, and
        rewriting metadata on every startup is not worth the surprise; the
        probe still exercised the embedder.
        """
        collection = _FakeCollection(metadata={"hnsw:space": "cosine"}, count=3)
        _verify_embedding_dimension(collection, _FakeEmbedder(384), "c")
        self.assertIsNone(collection.modified)


if __name__ == "__main__":
    unittest.main()
