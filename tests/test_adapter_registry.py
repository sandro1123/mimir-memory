# -*- coding: utf-8 -*-
"""1.8.0 spec §4: agent adapter contract, registry, and compat facade tests."""
from __future__ import annotations

import unittest

from mimir_v8.adapters import hooks_a_tier
from mimir_v8.adapters.registry import (
    AdapterCapabilities,
    AdapterSpec,
    EndpointBinding,
    get_adapter,
    get_registry,
    list_adapters,
    manifest,
    register_adapter,
    validate_adapter_spec,
)
from mimir_v8.schema import ValidationError


class TestBuiltinAdapters(unittest.TestCase):
    def test_builtins_registered_with_hook_paths(self):
        for adapter_id in ("claude-code", "codex"):
            spec = get_adapter(adapter_id)
            self.assertIsNotNone(spec, adapter_id)
            self.assertEqual(spec.tier, "A")
            self.assertEqual(spec.status, "builtin")
            self.assertEqual(spec.hooks_start.path, "/v8/hooks/session-start")
            self.assertEqual(spec.hooks_end.path, "/v8/hooks/session-end")
            self.assertTrue(spec.capabilities.session_start)
            self.assertTrue(spec.capabilities.session_end)

    def test_list_filters_by_tier_and_capability(self):
        a_tier = list_adapters(tier="A")
        self.assertEqual({s.adapter_id for s in a_tier}, {"claude-code", "codex"})
        mcp = list_adapters(capability="post_tool_use")
        self.assertEqual(mcp, [])
        start_capable = list_adapters(capability="session_start")
        self.assertEqual({s.adapter_id for s in start_capable}, {"claude-code", "codex"})

    def test_require_unknown_adapter_fails_closed(self):
        with self.assertRaises(ValidationError):
            get_registry().require("nonexistent-agent")

    def test_register_rejects_duplicate_without_override(self):
        spec = get_adapter("claude-code")
        with self.assertRaises(ValidationError):
            register_adapter(spec)

    def test_register_override_replaces(self):
        from mimir_v8.adapters.registry import AdapterRegistry

        registry = AdapterRegistry()
        registry.register(get_adapter("claude-code"))
        replacement = AdapterSpec(
            adapter_id="claude-code",
            display_name="Claude Code (pinned)",
            version="9.9.9",
            tier="A",
            status="experimental",
            description="test replacement",
            capabilities=AdapterCapabilities(session_start=True, session_end=True),
            hooks_start=EndpointBinding("GET", "/v8/hooks/session-start"),
            hooks_end=EndpointBinding("POST", "/v8/hooks/session-end"),
        )
        registry.register(replacement, override=True)
        self.assertEqual(registry.get("claude-code").version, "9.9.9")

    def test_explicit_memory_mode_rejected_in_spec(self):
        # The frozen dataclass accepts the value (Literal is a typing hint),
        # so the strict validator must be the fail-closed gate.
        spec = AdapterSpec(
            adapter_id="bad-agent",
            display_name="Bad",
            version="1.0.0",
            tier="C",
            status="experimental",
            description="bad mode",
            capabilities=AdapterCapabilities(explicit_remember_assist=True),
            default_memory_mode="explicit",
        )
        with self.assertRaises(ValidationError):
            validate_adapter_spec(spec)

    def test_minimum_viable_capability_enforced(self):
        spec = AdapterSpec(
            adapter_id="no-capability",
            display_name="No Capability",
            version="1.0.0",
            tier="C",
            status="experimental",
            description="no surfaces declared",
            capabilities=AdapterCapabilities(),
        )
        with self.assertRaises(ValidationError):
            validate_adapter_spec(spec)

    def test_kebab_case_id_enforced(self):
        spec = AdapterSpec(
            adapter_id="Bad_ID",
            display_name="Bad",
            version="1.0.0",
            tier="C",
            status="experimental",
            description="bad id",
            capabilities=AdapterCapabilities(explicit_remember_assist=True),
        )
        with self.assertRaises(ValidationError):
            validate_adapter_spec(spec)

    def test_hook_capability_requires_matching_binding(self):
        spec = AdapterSpec(
            adapter_id="half-hooked",
            display_name="Half",
            version="1.0.0",
            tier="A",
            status="experimental",
            description="session_start without binding",
            capabilities=AdapterCapabilities(session_start=True),
        )
        with self.assertRaises(ValidationError):
            validate_adapter_spec(spec)

    def test_nonstandard_hook_path_rejected(self):
        spec = AdapterSpec(
            adapter_id="path-drift",
            display_name="Drift",
            version="1.0.0",
            tier="A",
            status="experimental",
            description="custom endpoint",
            capabilities=AdapterCapabilities(session_start=True, session_end=True),
            hooks_start=EndpointBinding("GET", "/v9/hooks/session-start"),
            hooks_end=EndpointBinding("POST", "/v8/hooks/session-end"),
        )
        with self.assertRaises(ValidationError):
            validate_adapter_spec(spec)


class TestHooksATierCompat(unittest.TestCase):
    def test_hooks_a_tier_shape_unchanged(self):
        self.assertEqual(set(hooks_a_tier.HOOKS_A_TIER), {"claude-code", "codex"})
        for paths in hooks_a_tier.HOOKS_A_TIER.values():
            self.assertEqual(paths["start"], "/v8/hooks/session-start")
            self.assertEqual(paths["end"], "/v8/hooks/session-end")

    def test_build_session_end_payload_defaults_match_baseline(self):
        payload = hooks_a_tier.build_session_end_payload(
            connector_type="claude-code",
            connector_id="claude-code",
            session_id="sess-1",
            owner_principal="jarvis",
            messages=[{"role": "user", "content": "hi"}],
            idempotency_key="hook:sess-1:end",
        )
        self.assertEqual(payload["memory_mode"], "observe")
        self.assertEqual(payload["retention_class"], "short")
        self.assertEqual(payload["idempotency_key"], "hook:sess-1:end")

    def test_build_session_end_payload_rejects_explicit(self):
        with self.assertRaises(ValidationError):
            hooks_a_tier.build_session_end_payload(
                connector_type="claude-code",
                connector_id="claude-code",
                session_id="sess-1",
                owner_principal="jarvis",
                messages=[{"role": "user", "content": "hi"}],
                idempotency_key="hook:sess-1:end",
                memory_mode="explicit",
            )

    def test_adapter_id_applies_registry_retention_default(self):
        payload = hooks_a_tier.build_session_end_payload(
            connector_type="external_agent",
            connector_id="codex:box1",
            session_id="sess-2",
            owner_principal="jarvis",
            messages=[],
            idempotency_key="hook:sess-2:end",
            adapter_id="codex",
        )
        self.assertEqual(payload["retention_class"], "short")


class TestManifest(unittest.TestCase):
    def test_manifest_is_non_sensitive_and_complete(self):
        data = manifest(get_adapter("claude-code"))
        self.assertEqual(data["adapter_id"], "claude-code")
        self.assertEqual(data["tier"], "A")
        self.assertEqual(data["hooks"]["start"]["path"], "/v8/hooks/session-start")
        self.assertIn("capabilities", data)
        self.assertIn("default_connector_type", data)
        # No secrets/tokens anywhere in the manifest.
        serialized = str(data)
        for token in ("token", "secret", "password"):
            self.assertNotIn(token, serialized.lower())


if __name__ == "__main__":
    unittest.main()
