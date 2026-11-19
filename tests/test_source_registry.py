# -*- coding: utf-8 -*-
"""1.8.0 spec §5: source connector registry, classifier parity, and policy tests."""
from __future__ import annotations

import unittest

from mimir_v8.classifier import (
    CONVERSATION_TYPES,
    EXTERNAL_INFO_TYPES,
    KNOWLEDGE_DOC_TYPES,
    SOURCE_CATEGORY_MAP,
    classify,
    is_conversation,
    is_external_info,
    is_knowledge_doc,
    is_quarantine,
)
from mimir_v8.schema import ValidationError
from mimir_v8.sources.registry import (
    SourceRegistry,
    SourceSpec,
    get_registry,
    list_sources,
    manifest,
    policy_for,
    register_source,
    resolve_category,
    validate_source_spec,
)


class TestClassifierParity(unittest.TestCase):
    """Built-ins must reproduce classifier.SOURCE_CATEGORY_MAP exactly (spec §5.3)."""

    def test_registry_matches_static_map(self):
        registry = get_registry()
        for connector_type, expected in SOURCE_CATEGORY_MAP.items():
            self.assertEqual(
                registry.resolve_category(connector_type),
                expected,
                f"{connector_type} classification drifted",
            )
            spec = registry.get(connector_type)
            self.assertIsNotNone(spec, connector_type)
            self.assertEqual(spec.source_category, expected)

    def test_classify_facade_parity(self):
        self.assertEqual(classify("hermes_cdc"), "conversation")
        self.assertEqual(classify("external_agent"), "conversation")
        self.assertEqual(classify("workbuddy"), "conversation")
        self.assertEqual(classify("rss"), "external_info")
        self.assertEqual(classify("web"), "external_info")
        self.assertEqual(classify("searxng"), "external_info")
        self.assertEqual(classify("feishu"), "knowledge_doc")
        self.assertEqual(classify("file"), "knowledge_doc")
        self.assertEqual(classify("document"), "knowledge_doc")
        self.assertEqual(classify("vault"), "knowledge_doc")
        self.assertEqual(classify("unknown_xyz"), "unknown/quarantine")
        self.assertEqual(classify(""), "unknown/quarantine")
        self.assertEqual(classify("   "), "unknown/quarantine")
        self.assertEqual(classify(None), "unknown/quarantine")
        self.assertEqual(classify(123), "unknown/quarantine")

    def test_helper_predicates_unchanged(self):
        self.assertTrue(is_conversation("hermes_cdc"))
        self.assertFalse(is_conversation("vault"))
        self.assertTrue(is_external_info("rss"))
        self.assertTrue(is_knowledge_doc("vault"))
        self.assertTrue(is_quarantine("nope"))
        self.assertTrue(is_quarantine(42))
        self.assertFalse(is_quarantine("hermes_cdc"))
        self.assertEqual(CONVERSATION_TYPES, frozenset({"hermes_cdc", "external_agent", "workbuddy"}))
        self.assertEqual(EXTERNAL_INFO_TYPES, frozenset({"rss", "web", "searxng"}))
        self.assertEqual(KNOWLEDGE_DOC_TYPES, frozenset({"feishu", "file", "document", "vault"}))


class TestSourcePolicy(unittest.TestCase):
    def test_conversation_policy_allows_observe_never_and_auto_candidate(self):
        for connector_type in ("hermes_cdc", "external_agent", "workbuddy"):
            spec = policy_for(connector_type)
            self.assertEqual(spec.allowed_memory_modes, frozenset({"observe", "never"}))
            self.assertTrue(spec.auto_candidate)
            self.assertEqual(spec.extraction_policy, "candidate_only")
            self.assertEqual(spec.ingest_kind, "conversation")

    def test_non_conversation_policy_never_auto_candidates(self):
        for connector_type in ("rss", "web", "searxng", "feishu", "file", "document", "vault"):
            spec = policy_for(connector_type)
            self.assertFalse(spec.auto_candidate, connector_type)
            self.assertNotIn("explicit", spec.allowed_memory_modes)

    def test_unknown_connector_gets_quarantine_policy(self):
        policy = policy_for("mystery_connector")
        self.assertFalse(policy.auto_candidate)
        self.assertNotIn("observe", policy.allowed_memory_modes)
        self.assertNotIn("explicit", policy.allowed_memory_modes)
        self.assertTrue(policy.metadata.get("quarantine"))
        # Quarantine policy never classifies as conversation.
        self.assertNotEqual(policy.source_category, "conversation")

    def test_resolve_category_quarantine_shapes(self):
        self.assertEqual(resolve_category("mystery"), "unknown/quarantine")
        self.assertEqual(resolve_category(""), "unknown/quarantine")
        self.assertEqual(resolve_category(None), "unknown/quarantine")


class TestRegistryContract(unittest.TestCase):
    def test_register_rejects_duplicate_without_override(self):
        registry = SourceRegistry()
        spec = get_registry().get("hermes_cdc")
        registry.register(spec)
        with self.assertRaises(ValidationError):
            registry.register(spec)
        registry.register(spec, override=True)  # allowed

    def test_register_validates_fail_closed(self):
        registry = SourceRegistry()
        bad = SourceSpec(
            connector_type="Bad-Type",
            display_name="Bad",
            description="kebab not snake",
            source_category="conversation",
            ingest_kind="conversation",
        )
        with self.assertRaises(ValidationError):
            registry.register(bad)

    def test_conversation_source_cannot_allow_explicit(self):
        spec = SourceSpec(
            connector_type="rogue_agent",
            display_name="Rogue",
            description="tries to allow explicit",
            source_category="conversation",
            ingest_kind="conversation",
            allowed_memory_modes=frozenset({"observe", "explicit"}),
        )
        with self.assertRaises(ValidationError):
            validate_source_spec(spec)

    def test_default_mode_must_be_allowed(self):
        spec = SourceSpec(
            connector_type="mode_drift",
            display_name="Drift",
            description="default not in allowed set",
            source_category="knowledge_doc",
            ingest_kind="knowledge_item",
            allowed_memory_modes=frozenset({"never"}),
            default_memory_mode="observe",
        )
        with self.assertRaises(ValidationError):
            validate_source_spec(spec)

    def test_auto_candidate_requires_candidate_policy(self):
        spec = SourceSpec(
            connector_type="policy_drift",
            display_name="Drift",
            description="auto candidate with extraction none",
            source_category="conversation",
            ingest_kind="conversation",
            extraction_policy="none",
            auto_candidate=True,
        )
        with self.assertRaises(ValidationError):
            validate_source_spec(spec)

    def test_require_unknown_fails_closed(self):
        with self.assertRaises(ValidationError):
            get_registry().require("not_a_source")

    def test_register_new_source_via_public_api(self):
        spec = SourceSpec(
            connector_type="thor_auto",
            display_name="Thor Auto Collection",
            description="Thor's automatic collection exemplar (spec §5.6).",
            source_category="conversation",
            ingest_kind="conversation",
            status="experimental",
            default_retention_class="standard",
            auto_candidate=True,
            extraction_policy="candidate_only",
        )
        register_source(spec)
        try:
            registered = get_registry().require("thor_auto")
            self.assertEqual(registered.source_category, "conversation")
            self.assertEqual(classify("thor_auto"), "conversation")
        finally:
            # Unregister to keep the process-wide registry built-in-clean.
            get_registry()._specs.pop("thor_auto", None)
        self.assertEqual(classify("thor_auto"), "unknown/quarantine")

    def test_list_filters(self):
        conv = list_sources(category="conversation")
        self.assertEqual(
            {s.connector_type for s in conv},
            {"hermes_cdc", "external_agent", "workbuddy"},
        )
        knowledge = list_sources(ingest_kind="knowledge_item")
        self.assertEqual(
            {s.connector_type for s in knowledge},
            {"rss", "web", "searxng", "feishu", "file", "document", "vault"},
        )


class TestManifest(unittest.TestCase):
    def test_manifest_shape_and_no_secrets(self):
        data = manifest(policy_for("external_agent"))
        self.assertEqual(data["connector_type"], "external_agent")
        self.assertEqual(data["source_category"], "conversation")
        self.assertIn("extraction_policy", data)
        self.assertIn("idempotency_strategy", data)
        serialized = str(data)
        for token in ("token", "secret", "password"):
            self.assertNotIn(token, serialized.lower())


if __name__ == "__main__":
    unittest.main()
