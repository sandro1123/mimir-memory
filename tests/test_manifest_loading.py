# -*- coding: utf-8 -*-
"""1.8.0 spec §9 Task 3: declarative manifest loading (strict, fail closed)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from mimir_v8.adapters.manifests import (
    adapter_spec_from_manifest,
    load_adapter_manifests,
    load_source_manifests,
)
from mimir_v8.classifier import classify
from mimir_v8.schema import ValidationError
from mimir_v8.sources.registry import get_registry, policy_for


_ADAPTER_JSON = {
    "adapter_id": "opencode",
    "display_name": "OpenCode",
    "version": "0.1.0",
    "tier": "B",
    "status": "experimental",
    "description": "MCP-triggered explicit/remember-assist adapter exemplar.",
    "capabilities": {"explicit_remember_assist": True},
    "default_memory_mode": "observe",
    "default_retention_class": "standard",
}

_SOURCE_JSON = {
    "connector_type": "odin_auto",
    "display_name": "Odin Auto Collection",
    "description": "Odin node automatic collection exemplar (spec §5.6).",
    "source_category": "conversation",
    "ingest_kind": "conversation",
    "status": "experimental",
    "default_retention_class": "standard",
    "auto_candidate": True,
    "extraction_policy": "candidate_only",
    "idempotency_strategy": "session_turn_range",
}


class TestAdapterManifests(unittest.TestCase):
    def test_unset_env_is_noop(self):
        saved = os.environ.pop("MIMIR_ADAPTERS_DIR", None)
        try:
            self.assertEqual(load_adapter_manifests(), [])
        finally:
            if saved is not None:
                os.environ["MIMIR_ADAPTERS_DIR"] = saved

    def test_valid_manifest_loads_and_registers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.json"
            path.write_text(json.dumps(_ADAPTER_JSON), encoding="utf-8")
            loaded = load_adapter_manifests(tmp)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].adapter_id, "opencode")
            spec = get_adapter_safe("opencode")
            self.assertIsNotNone(spec)
            self.assertEqual(spec.tier, "B")
            self.assertEqual(spec.status, "experimental")

    def test_invalid_manifest_aborts_entire_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "opencode.json"
            good.write_text(json.dumps(_ADAPTER_JSON), encoding="utf-8")
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({**_ADAPTER_JSON, "adapter_id": "Bad_ID"}), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_adapter_manifests(tmp)
            # All-or-nothing: the good manifest must not have registered.
            self.assertIsNone(get_adapter_safe("opencode"))

    def test_missing_directory_fails_closed(self):
        with self.assertRaises(ValidationError):
            load_adapter_manifests("/nonexistent/adapters-dir")

    def test_explicit_mode_rejected_at_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "explicit.json"
            bad.write_text(json.dumps({**_ADAPTER_JSON, "default_memory_mode": "explicit"}), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_adapter_manifests(tmp)

    def test_hook_capability_requires_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "half.json"
            bad.write_text(json.dumps({
                **_ADAPTER_JSON,
                "adapter_id": "half-hooked",
                "tier": "A",
                "capabilities": {"session_end": True},
            }), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_adapter_manifests(tmp)

    def test_manifest_dict_direct_build(self):
        spec = adapter_spec_from_manifest(_ADAPTER_JSON, origin="inline")
        self.assertEqual(spec.adapter_id, "opencode")


class TestSourceManifests(unittest.TestCase):
    def test_unset_env_is_noop(self):
        saved = os.environ.pop("MIMIR_SOURCES_DIR", None)
        try:
            self.assertEqual(load_source_manifests(), [])
        finally:
            if saved is not None:
                os.environ["MIMIR_SOURCES_DIR"] = saved

    def test_valid_source_manifest_registers_and_classifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "odin_auto.json"
            path.write_text(json.dumps(_SOURCE_JSON), encoding="utf-8")
            loaded = load_source_manifests(tmp)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(classify("odin_auto"), "conversation")
            policy = policy_for("odin_auto")
            self.assertTrue(policy.auto_candidate)
            self.assertNotIn("explicit", policy.allowed_memory_modes)
        # Env-based load is isolated to this process; leave registry clean.
        get_registry()._specs.pop("odin_auto", None)

    def test_invalid_source_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({
                **_SOURCE_JSON,
                "connector_type": "kebab-not-snake",
            }), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_source_manifests(tmp)
            self.assertEqual(classify("kebab-not-snake"), "unknown/quarantine")

    def test_conversation_source_manifest_cannot_allow_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "rogue.json"
            bad.write_text(json.dumps({
                **_SOURCE_JSON,
                "connector_type": "rogue_source",
                "allowed_memory_modes": ["observe", "explicit"],
            }), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_source_manifests(tmp)


def get_adapter_safe(adapter_id):
    from mimir_v8.adapters.registry import get_adapter

    return get_adapter(adapter_id)


if __name__ == "__main__":
    unittest.main()
