# -*- coding: utf-8 -*-
"""Every collection name we ship must pass the rule the server enforces.

Regression: both Dockerfiles shipped ``MIMIR_V8_COLLECTION=mimir_v12_prod``,
which ``validate_vector_collection_name`` rejects (it only accepts the
``mimir_v8|v9|v10_(shadow|prod)_*`` namespaces). ``runtime._build_vector``
turns that rejection into ``RuntimeConfigurationError``, so ``docker run``
with the image's own defaults could not start at all — while
``docker compose`` (a different, valid name) could.

The lesson is not "pick another name" but "the defaults we ship are part of
the contract": this test parses the shipped defaults and validates them with
the same function the runtime uses.
"""
from __future__ import annotations

import inspect
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8.runtime import build_runtime
from mimir_v8.server import build_app
from mimir_v8.vector_projector import validate_vector_collection_name

ROOT = Path(__file__).resolve().parent.parent

SHIPPED_FILES = ("Dockerfile", "Dockerfile.lean", "docker-compose.yml")
_COLLECTION_RE = re.compile(r"MIMIR_V8_COLLECTION\s*=\s*([A-Za-z0-9_.\-]+)")


def _shipped_collections() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name in SHIPPED_FILES:
        path = ROOT / name
        if not path.is_file():
            continue
        for value in _COLLECTION_RE.findall(path.read_text(encoding="utf-8")):
            found.append((name, value))
    return found


class TestShippedCollectionDefaults(unittest.TestCase):
    def test_every_shipped_collection_name_validates(self):
        shipped = _shipped_collections()
        self.assertTrue(
            shipped,
            "no MIMIR_V8_COLLECTION found in the shipped deployment files — "
            "did the ENV block move?",
        )
        for source, value in shipped:
            with self.subTest(source=source, collection=value):
                validate_vector_collection_name(value)

    def test_code_defaults_validate(self):
        for factory in (build_runtime, build_app):
            default = inspect.signature(factory).parameters["collection_name"].default
            with self.subTest(factory=factory.__name__, collection=default):
                validate_vector_collection_name(default)

    def test_dockerfiles_agree_with_each_other(self):
        """Two images of the same product must not seed different collections."""
        per_file: dict[str, set[str]] = {}
        for source, value in _shipped_collections():
            if source.startswith("Dockerfile"):
                per_file.setdefault(source, set()).add(value)
        if len(per_file) < 2:
            self.skipTest("only one Dockerfile present")
        distinct = {frozenset(values) for values in per_file.values()}
        self.assertEqual(
            len(distinct), 1,
            f"Dockerfiles ship different default collections: {per_file}",
        )


if __name__ == "__main__":
    unittest.main()
