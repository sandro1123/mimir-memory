"""Federation write-path tests (v12.1.2 hotfix: static-set consumers).

v12.1.0 wired the dynamic registry (config federation section + boot loader)
but only validate_agent_id consults it. Four write-path consumers still
check the static AGENT_IDS frozenset, so a config-registered agent
(quantstar) passes validate_agent_id yet is rejected by the actual write
paths — a half-wired registry that the production deployment caught live
(POST /v8/facts 422 invalid owner_principal: quantstar).

These tests pin the other half of the wiring: after register_agent(), the
write paths must accept the registered agent. Same symmetry for domains.

Isolation: the registry is a module-level mutable set, so every test
snapshots and restores it (_RegistrySnapshot from the P20 module).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimir_v8 import schema


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


class TestCreateFactAcceptsRegisteredAgent(unittest.TestCase):
    """P21-01: schema.CreateFact.validated (the POST /v8/facts write path)."""

    def test_registered_agent_passes_createfact_validation(self):
        # RED today: validated() checks the static AGENT_IDS frozenset.
        with _RegistrySnapshot():
            schema.register_agent("quantstar")
            fact = schema.CreateFact(
                owner_principal="quantstar",
                domain="quant",
                fact_type="event",
                content="hotfix red test: registered agent must pass write path",
                visibility="owner_only",
            )
            validated = fact.validated()
            self.assertEqual(validated.owner_principal, "quantstar")

    def test_registered_domain_passes_createfact_validation(self):
        with _RegistrySnapshot():
            schema.register_domain("quant_infra")
            fact = schema.CreateFact(
                owner_principal="mentor",
                domain="quant_infra",
                fact_type="event",
                content="hotfix red test: registered domain must pass write path",
                visibility="owner_only",
            )
            validated = fact.validated()
            self.assertEqual(validated.domain, "quant_infra")

    def test_unregistered_agent_still_rejected(self):
        with _RegistrySnapshot():
            fact = schema.CreateFact(
                owner_principal="bogus-agent",
                domain="quant",
                fact_type="event",
                content="unregistered agent must keep failing",
                visibility="owner_only",
            )
            with self.assertRaises(schema.ValidationError):
                fact.validated()


class TestLearningRememberAcceptsRegisteredAgent(unittest.TestCase):
    """P21-02: learning.remember explicit-memory intake."""

    def _make_service(self):
        import tempfile

        from mimir_v8.learning import LearningService
        from mimir_v8.store import CanonicalStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return LearningService(CanonicalStore(Path(tmp.name) / "store.db"))

    def test_remember_accepts_registered_agent(self):
        with _RegistrySnapshot():
            schema.register_agent("quantstar")
            svc = self._make_service()
            result = svc.remember(
                content="hotfix red test: remember accepts registered agent",
                owner_principal="quantstar",
                domain="quant",
                fact_type="event",
                actor_principal="quantstar",
                idempotency_key="p21-remember-1",
            )
            self.assertIn("candidate_id", result)

    def test_remember_still_rejects_unregistered_agent(self):
        with _RegistrySnapshot():
            from mimir_v8.schema import ValidationError

            svc = self._make_service()
            with self.assertRaises(ValidationError):
                svc.remember(
                    content="unregistered agent must keep failing",
                    owner_principal="bogus-agent",
                    domain="quant",
                    fact_type="event",
                    actor_principal="mentor",
                    idempotency_key="p21-remember-2",
                )


class TestCoreMemoryPromoteAcceptsRegisteredAgent(unittest.TestCase):
    """P21-03: core_memory.promote canonical promotion."""

    def _make_service(self):
        import tempfile

        from mimir_v8.core_memory import CoreMemoryService
        from mimir_v8.store import CanonicalStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return CoreMemoryService(CanonicalStore(Path(tmp.name) / "store.db"))

    def test_promote_accepts_registered_agent(self):
        from mimir_v8.core_memory import CoreMemoryPolicyError, PromoteCoreMemory

        with _RegistrySnapshot():
            schema.register_agent("quantstar")
            svc = self._make_service()
            cmd = PromoteCoreMemory(
                agent_id="quantstar",
                block_name="project_context",
                fact_id="fact-does-not-matter",
                reason="hotfix red test: promote accepts registered agent",
                idempotency_key="p21-promote-1",
            )
            # authorize must also accept; run as admin to isolate the
            # static-set check from the ACL layer. fact_id does not exist
            # in the empty store, so a non-policy error past the
            # "unknown agent" gate is the pass condition.
            with self.assertRaises(Exception) as ctx:
                svc.promote(cmd, actor_principal="admin", is_admin=True)
            self.assertNotIsInstance(ctx.exception, CoreMemoryPolicyError)
            self.assertNotIn("unknown agent", str(ctx.exception))

    def test_promote_still_rejects_unregistered_agent(self):
        from mimir_v8.core_memory import CoreMemoryPolicyError, PromoteCoreMemory

        with _RegistrySnapshot():
            svc = self._make_service()
            cmd = PromoteCoreMemory(
                agent_id="bogus-agent",
                block_name="project_context",
                fact_id="fact-does-not-matter",
                reason="unregistered agent must keep failing",
                idempotency_key="p21-promote-2",
            )
            with self.assertRaises(CoreMemoryPolicyError):
                svc.promote(cmd, actor_principal="admin", is_admin=True)


class TestKnowledgeCreateAcceptsRegisteredDomain(unittest.TestCase):
    """P21-04b: knowledge.py create_item domain check (grep-found consumer)."""

    def test_knowledge_domain_check_consults_dynamic_registry(self):
        import mimir_v8.knowledge

        source = _read_module_source(mimir_v8.knowledge)
        # The domain gate must consult the registry function, not the static
        # frozenset — a config-registered domain must be accepted.
        self.assertIn("get_registered_domains", source)
        self.assertNotIn("DOMAINS", _module_imports(_source_path(mimir_v8.knowledge)))


class TestEvaluatorDomainWhitelistIsDynamic(unittest.TestCase):
    """P21-04c: evaluator ALLOWED_DOMAINS frozen at import defeats registry.

    `ALLOWED_DOMAINS = frozenset(DOMAINS)` snapshots the static set at
    import time; registering quant_infra afterwards never reaches it.
    The whitelist must be computed per evaluation from the live registry.
    """

    def test_evaluator_domain_whitelist_consults_dynamic_registry(self):
        import mimir_v8.evaluator

        source = _read_module_source(mimir_v8.evaluator)
        self.assertNotIn(
            "ALLOWED_DOMAINS = frozenset(DOMAINS)",
            source,
            msg="evaluator still freezes the static domain whitelist at import time",
        )
        self.assertNotIn("DOMAINS", _module_imports(_source_path(mimir_v8.evaluator)))
        self.assertIn("get_registered_domains", source)


class TestCrystalApproveSourceUsesDynamicRegistry(unittest.TestCase):
    """P21-04: api.crystal_approve falls back to "mentor" only for truly
    unknown owners — a registered agent must keep its own principal."""

    def test_crystal_approve_source_checks_dynamic_registry(self):
        import inspect

        from mimir_v8 import api

        source = inspect.getsource(api)
        # The endpoint must consult the dynamic registry, not the static
        # frozenset: a config-registered owner (quantstar) approving a
        # crystal must be recorded as quantstar, not silently re-attributed
        # to mentor.
        for func in ("def crystal_approve",):
            self.assertIn(func, source)
        crystal_src = _extract_function_source(api, "crystal_approve")
        self.assertIn("get_registered_agents", crystal_src)
        self.assertNotIn("AGENT_IDS", crystal_src)


def _extract_function_source(module, func_name: str) -> str:
    import ast
    import textwrap

    source_file = _source_path(module)
    source_text = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return textwrap.dedent("\n".join(
                source_text.splitlines()[node.lineno - 1: node.end_lineno]
            ))
    raise AssertionError(f"function {func_name} not found in {module.__name__}")


def _source_path(module) -> str:
    import inspect

    return inspect.getsourcefile(module)


def _read_module_source(module) -> str:
    return Path(_source_path(module)).read_text(encoding="utf-8")


# back-compat alias used by earlier drafts of this module
inspect_source_file = _source_path


class TestWritePathsShareOneRegistry(unittest.TestCase):
    """P21-05: no write-path consumer may consult the static frozenset.

    The registry has exactly one source of truth. Any module that imports
    AGENT_IDS *and* uses it in a decision (as opposed to merely re-exporting)
    reintroduces the half-wired gap. This pins the source-level contract.
    """

    def test_no_module_decides_against_static_agent_ids(self):
        import mimir_v8
        import mimir_v8.api
        import mimir_v8.core_memory
        import mimir_v8.learning

        for module in (mimir_v8.schema, mimir_v8.learning, mimir_v8.api, mimir_v8.core_memory):
            source = _read_module_source(module)
            if module is mimir_v8.schema:
                # schema.py defines AGENT_IDS and the registry; allowed.
                continue
            # Consumers must not import AGENT_IDS from schema at all.
            self.assertNotIn(
                "AGENT_IDS",
                source,
                msg=f"{module.__name__} still references the static AGENT_IDS frozenset",
            )
            self.assertNotIn(
                "AGENT_IDS",
                _module_imports(_source_path(module)),
                msg=f"{module.__name__} still imports AGENT_IDS",
            )


def _module_imports(source_file) -> str:
    """Extract just the import lines from a module (CRLF-safe)."""
    lines = Path(source_file).read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if "import" in line)


if __name__ == "__main__":
    unittest.main()
