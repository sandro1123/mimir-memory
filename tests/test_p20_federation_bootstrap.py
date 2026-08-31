"""Federation registry bootstrap tests (v12.1.0 hotfix: wire the dynamic registry).

The dynamic agent/domain registry (schema.register_agent / register_domain)
existed but nothing ever called it at boot — a textbook "built but not wired".
These tests pin the wiring: a config-driven federation section plus a
loader invoked from every entrypoint so config-registered agents are valid
in every process (server and worker alike).

Isolation: the registry is a module-level mutable set, so every test
snapshots and restores it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from mimir_v8 import schema
from mimir_v8.config import load_federation_registry


class _RegistrySnapshot:
    """Snapshot/restore the module-level registry sets around each test."""

    def __enter__(self):
        self._agents = set(schema.get_registered_agents())
        self._domains = set(schema.get_registered_domains())
        return self

    def __exit__(self, *exc):
        schema._DYNAMIC_AGENTS.clear()
        schema._DYNAMIC_AGENTS.update(self._agents)
        schema._DYNAMIC_DOMAINS.clear()
        schema._DYNAMIC_DOMAINS.update(self._domains)
        return False


def _write_config(tmp: str, body: dict) -> Path:
    cfg = Path(tmp) / "mimir_config.yaml"
    cfg.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return cfg


class TestLoadFederationRegistry(unittest.TestCase):
    """P20-01: config-driven federation registration."""

    def test_registers_agents_and_domains_from_config(self):
        body = {
            "federation": {
                "agents": ["quantstar", "atlas"],
                "domains": ["quant_infra"],
            }
        }
        with tempfile.TemporaryDirectory() as tmp, _RegistrySnapshot():
            cfg = _write_config(tmp, body)
            load_federation_registry(cfg)
            agents = schema.get_registered_agents()
            domains = schema.get_registered_domains()
            self.assertIn("quantstar", agents)
            self.assertIn("atlas", agents)
            self.assertIn("quant_infra", domains)
            # defaults survive alongside config-registered entries
            self.assertIn("mentor", agents)
            self.assertIn("quant", domains)
            # registered agents now validate
            self.assertEqual(schema.validate_agent_id("quantstar"), "quantstar")
            self.assertEqual(schema.validate_domain("quant_infra"), "quant_infra")

    def test_missing_config_file_is_a_noop(self):
        # No file -> defaults stay, no crash (worker paths run without config).
        with tempfile.TemporaryDirectory() as tmp, _RegistrySnapshot():
            load_federation_registry(Path(tmp) / "nope.yaml")
            self.assertIn("mentor", schema.get_registered_agents())

    def test_config_without_federation_section_is_a_noop(self):
        body = {"collector": {"rss_feeds": []}}
        with tempfile.TemporaryDirectory() as tmp, _RegistrySnapshot():
            cfg = _write_config(tmp, body)
            load_federation_registry(cfg)
            self.assertNotIn("quantstar", schema.get_registered_agents())

    def test_federation_section_must_be_a_mapping(self):
        body = {"federation": "not-a-mapping"}
        with tempfile.TemporaryDirectory() as tmp, _RegistrySnapshot():
            cfg = _write_config(tmp, body)
            with self.assertRaises(ValueError):
                load_federation_registry(cfg)

    def test_agent_entries_must_be_nonempty_strings(self):
        body = {"federation": {"agents": ["good-agent", ""]}}
        with tempfile.TemporaryDirectory() as tmp, _RegistrySnapshot():
            cfg = _write_config(tmp, body)
            with self.assertRaises(ValueError):
                load_federation_registry(cfg)

    def test_default_config_path_used_when_none_given(self):
        # None -> MimirPaths.from_env().config_file; must not raise even
        # when that file does not exist.
        with _RegistrySnapshot():
            load_federation_registry(None)
            self.assertIn("mentor", schema.get_registered_agents())


class TestEntrypointsCallLoader(unittest.TestCase):
    """P20-02: both process entrypoints invoke the loader."""

    def test_worker_main_calls_loader(self):
        import inspect

        from mimir_v8 import worker

        source = inspect.getsource(worker.main)
        self.assertIn("load_federation_registry", source)

    def test_runtime_build_calls_loader(self):
        import inspect

        from mimir_v8 import runtime

        source = inspect.getsource(runtime.build_runtime)
        self.assertIn("load_federation_registry", source)


if __name__ == "__main__":
    unittest.main()
