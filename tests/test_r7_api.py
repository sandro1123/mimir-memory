from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mimir_v8.api import ServiceContext, create_app
from mimir_v8.auth import TokenStore
from mimir_v8.knowledge import FeedbackLoop, KnowledgeService, UnifiedSearch
from mimir_v8.query import QueryKernel
from mimir_v8.runtime import build_runtime
from mimir_v8.schema import SCHEMA_VERSION
from mimir_v8.store import CanonicalStore


class R7APIFixture:
    def __init__(self, root: Path, *, enabled_layers=("memory", "learning", "wiki")):
        self.root = root
        self.store = CanonicalStore(root / "canonical.db")
        self.query = QueryKernel(self.store)
        self.knowledge = KnowledgeService(self.store)
        self.unified = UnifiedSearch(
            self.query, self.knowledge, enabled_layers=enabled_layers
        )
        self.feedback = FeedbackLoop(self.store, self.knowledge)
        self.tokens = {
            "mentor": "r7-mentor-token",
            "other": "r7-other-token",
            "admin": "r7-admin-token",
        }
        token_path = root / "tokens.json"
        token_path.write_text(
            json.dumps({
                "principals": [
                    {
                        "id": principal,
                        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "scopes": ["read", "write", "ingest"],
                        "roles": ["admin"] if principal == "admin" else [],
                        "admin": principal == "admin",
                    }
                    for principal, token in self.tokens.items()
                ]
            }),
            encoding="utf-8",
        )
        context = ServiceContext(
            store=self.store,
            token_store=TokenStore(token_path),
            query=self.query,
            knowledge=self.knowledge,
            unified_search=self.unified,
            feedback_loop=self.feedback,
        )
        self.client = TestClient(create_app(context), raise_server_exceptions=False)

    def headers(self, principal="mentor"):
        return {"Authorization": f"Bearer {self.tokens[principal]}"}

    @staticmethod
    def item_body(*, owner="mentor", status="review", key="r7-api-item"):
        return {
            "connector_type": "rss",
            "layer": "learning",
            "title": "N100 operations",
            "content": "N100 cooling and stability guidance",
            "summary": "N100 cooling guidance",
            "owner_principal": owner,
            "domain": "infrastructure",
            "source_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "idempotency_key": key,
            "topics": ["n100", "linux"],
            "status": status,
            "provenance": {"untrusted_external_content": True},
        }


class TestR7AuthenticatedAPI(unittest.TestCase):
    def test_preview_requires_authentication_and_read_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp))
            response = fixture.client.post(
                "/v9/search-preview", json={"text": "N100", "layers": ["learning"]}
            )
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.json()["error"]["code"], "missing_token")

    def test_api_ingestion_cannot_bypass_review_or_owner_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp))
            active = fixture.client.post(
                "/v9/knowledge/items",
                json=fixture.item_body(status="active"),
                headers=fixture.headers(),
            )
            self.assertEqual(active.status_code, 403, active.text)
            self.assertEqual(active.json()["error"]["code"], "review_required")
            cross_owner = fixture.client.post(
                "/v9/knowledge/items",
                json=fixture.item_body(owner="other", key="cross-owner"),
                headers=fixture.headers(),
            )
            self.assertEqual(cross_owner.status_code, 403, cross_owner.text)
            self.assertEqual(cross_owner.json()["error"]["code"], "owner_boundary")
            memory_body = fixture.item_body(key="memory-bypass")
            memory_body["layer"] = "memory"
            memory_bypass = fixture.client.post(
                "/v9/knowledge/items",
                json=memory_body,
                headers=fixture.headers(),
            )
            self.assertEqual(memory_bypass.status_code, 422, memory_bypass.text)
            self.assertEqual(memory_bypass.json()["error"]["code"], "source_routing_error")
            with self.store_connection(fixture) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0], 0)

    def test_owner_search_acl_and_feedback_governance(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp))
            created = fixture.client.post(
                "/v9/knowledge/items",
                json=fixture.item_body(),
                headers=fixture.headers(),
            )
            self.assertEqual(created.status_code, 201, created.text)
            item_id = created.json()["item_id"]

            owner_get = fixture.client.get(
                f"/v9/knowledge/items/{item_id}", headers=fixture.headers()
            )
            self.assertEqual(owner_get.status_code, 200, owner_get.text)
            denied_get = fixture.client.get(
                f"/v9/knowledge/items/{item_id}", headers=fixture.headers("other")
            )
            self.assertEqual(denied_get.status_code, 403, denied_get.text)
            self.assertEqual(denied_get.json()["error"]["code"], "acl_denied")

            owner_search = fixture.client.post(
                "/v9/search-preview",
                json={"text": "N100", "layers": ["learning"]},
                headers=fixture.headers(),
            )
            self.assertEqual(owner_search.status_code, 200, owner_search.text)
            self.assertEqual([row["stable_id"] for row in owner_search.json()["results"]], [item_id])
            denied_search = fixture.client.post(
                "/v9/search-preview",
                json={"text": "N100", "layers": ["learning"]},
                headers=fixture.headers("other"),
            )
            self.assertEqual(denied_search.status_code, 200, denied_search.text)
            self.assertEqual(denied_search.json()["results"], [])

            before_facts = fixture.store.counts()["facts"]
            feedback = fixture.client.post(
                "/v9/knowledge/feedback",
                json={
                    "target_layer": "learning",
                    "target_id": item_id,
                    "signal_type": "useful",
                    "signal_text": "Worth reviewing for memory",
                    "idempotency_key": "r7-api-feedback",
                },
                headers=fixture.headers(),
            )
            self.assertEqual(feedback.status_code, 201, feedback.text)
            self.assertFalse(feedback.json()["canonical_mutated"])
            self.assertEqual(feedback.json()["suggestion"]["suggestion_type"], "remember")
            self.assertEqual(fixture.store.counts()["facts"], before_facts)
            denied_feedback = fixture.client.post(
                "/v9/knowledge/feedback",
                json={
                    "target_layer": "learning",
                    "target_id": item_id,
                    "signal_type": "useful",
                    "signal_text": "Unauthorized",
                    "idempotency_key": "r7-api-feedback-other",
                },
                headers=fixture.headers("other"),
            )
            self.assertEqual(denied_feedback.status_code, 403, denied_feedback.text)
            self.assertEqual(denied_feedback.json()["error"]["code"], "acl_denied")

    def test_global_knowledge_status_requires_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp))
            denied = fixture.client.get(
                "/v9/knowledge/status", headers=fixture.headers()
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"]["code"], "missing_scope")
            allowed = fixture.client.get(
                "/v9/knowledge/status", headers=fixture.headers("admin")
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertEqual(allowed.json()["schema_version"], SCHEMA_VERSION)

    def test_disabled_layer_degrades_without_breaking_v8_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            preview = fixture.client.post(
                "/v9/search-preview",
                json={"text": "N100", "layers": ["wiki"]},
                headers=fixture.headers(),
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertTrue(preview.json()["partial"])
            self.assertEqual(preview.json()["layers"]["wiki"]["status"], "disabled")
            health = fixture.client.get("/v8/health")
            self.assertEqual(health.status_code, 200, health.text)
            legacy_query = fixture.client.post(
                "/v8/query", json={"text": "N100"}, headers=fixture.headers()
            )
            self.assertEqual(legacy_query.status_code, 200, legacy_query.text)
            self.assertIn("results", legacy_query.json())

    @staticmethod
    def store_connection(fixture):
        import contextlib
        return contextlib.closing(fixture.store.connect())


class TestR7V12SearchTrace(unittest.TestCase):
    """v12 /v12/search/trace — funnel shape and parameter validation."""

    def _fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            return R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))

    def test_trace_returns_funnel_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/search/trace",
                json={"text": "N100 cooling", "limit": 5, "candidate_limit": 30},
                headers=fixture.headers(),
            )
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            stages = [s["stage"] for s in data.get("stages", [])]
            # v12.2.0: AnchorChannel sits between pool assembly and dedup;
            # LayerSweep (L2/L1 progressive assembly) follows the anchors.
            self.assertEqual(stages, ["RelevanceGate", "CandidatePool",
                                      "AnchorChannel", "LayerSweep",
                                      "JaccardDedup", "ChronosDecay", "TopK"])
            self.assertIn("results", data)

    def test_trace_validates_dedup_threshold_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/search/trace?dedup_threshold=2.5",
                json={"text": "N100"},
                headers=fixture.headers(),
            )
            self.assertEqual(r.status_code, 422, r.text)

    def test_trace_requires_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/search/trace",
                json={"limit": 5},
                headers=fixture.headers(),
            )
            self.assertEqual(r.status_code, 422, r.text)

    def test_trace_requires_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/search/trace",
                json={"text": "N100"},
            )
            self.assertEqual(r.status_code, 401, r.text)


class TestR7V12ConflictAPI(unittest.TestCase):
    """v12 /v12/conflicts — detect, list, resolve, dismiss via API."""

    def _detect(self, fixture, principal="admin"):
        r = fixture.client.post(
            "/v12/conflicts/detect?threshold=0.6",
            headers=fixture.headers(principal),
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_detect_requires_manage_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/conflicts/detect", headers=fixture.headers("mentor")
            )
            self.assertEqual(r.status_code, 403, r.text)

    def test_validate_threshold_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/conflicts/detect?threshold=3", headers=fixture.headers("admin")
            )
            self.assertEqual(r.status_code, 422, r.text)

    def test_list_and_resolve_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            # seed two near-duplicate facts via the API
            for content in (
                "the agent stores facts in canonical memory store for recall",
                "the agent stores facts in canonical memory store for recall now",
            ):
                r = fixture.client.post("/v8/facts", headers=fixture.headers("admin"), json={
                    "content": content, "owner_principal": "mentor", "domain": "knowledge",
                    "fact_type": "event", "summary": content[:30], "visibility": "all",
                    "sensitivity": "internal", "egress_policy": "local_only",
                    "human_status": "confirmed",
                })
                self.assertEqual(r.status_code, 201, r.text)
            self._detect(fixture)
            lst = fixture.client.get(
                "/v12/conflicts?status=open", headers=fixture.headers("admin")
            ).json()
            self.assertGreaterEqual(len(lst["conflicts"]), 1)
            cid = lst["conflicts"][0]["conflict_id"]
            winner = lst["conflicts"][0]["fact_id_a"]
            resolve = fixture.client.post(
                f"/v12/conflicts/{cid}/resolve", headers=fixture.headers("admin"),
                json={"winner_fact_id": winner, "reason": "api test"},
            )
            self.assertEqual(resolve.status_code, 200, resolve.text)
            self.assertIn("loser_fact_id", resolve.json())
            # loser is now disputed
            loser = resolve.json()["loser_fact_id"]
            fact = fixture.client.get(
                f"/v8/facts/{loser}", headers=fixture.headers("admin")
            ).json()
            self.assertEqual(fact["status"], "disputed")


class TestR7V12CrystalAPI(unittest.TestCase):
    """v12 /v12/crystals — scan, list, approve, dismiss via API."""

    def _seed_facts(self, fixture, copies=3):
        for i in range(copies):
            r = fixture.client.post("/v8/facts", headers=fixture.headers("admin"), json={
                "content": f"crystal api topic common flow now variant {i}",
                "owner_principal": "mentor", "domain": "infrastructure",
                "fact_type": "pattern", "summary": f"crystal variant {i}",
                "visibility": "all", "sensitivity": "internal",
                "egress_policy": "local_only", "human_status": "confirmed",
            })
            self.assertEqual(r.status_code, 201, r.text)

    def test_scan_requires_manage_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/crystals/scan", headers=fixture.headers("mentor")
            )
            self.assertEqual(r.status_code, 403, r.text)

    def test_scan_validates_min_freq(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            r = fixture.client.post(
                "/v12/crystals/scan?min_freq=1", headers=fixture.headers("admin")
            )
            self.assertEqual(r.status_code, 422, r.text)

    def test_scan_list_approve_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            self._seed_facts(fixture)
            scan = fixture.client.post(
                "/v12/crystals/scan", headers=fixture.headers("admin")
            )
            self.assertEqual(scan.status_code, 200, scan.text)
            self.assertGreaterEqual(scan.json()["created"], 1)
            lst = fixture.client.get(
                "/v12/crystals?status=candidate", headers=fixture.headers("admin")
            ).json()
            self.assertGreaterEqual(len(lst["candidates"]), 1)
            cid = lst["candidates"][0]["candidate_id"]
            approved = fixture.client.post(
                f"/v12/crystals/{cid}/approve", headers=fixture.headers("admin")
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            crystal_fid = approved.json()["crystal_fact_id"]
            fact = fixture.client.get(
                f"/v8/facts/{crystal_fid}", headers=fixture.headers("admin")
            ).json()
            self.assertEqual(fact["fact_type"], "pattern")
            self.assertEqual(fact["status"], "active")

    def test_dismiss_then_approve_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            self._seed_facts(fixture)
            fixture.client.post("/v12/crystals/scan", headers=fixture.headers("admin"))
            cid = fixture.client.get(
                "/v12/crystals?status=candidate", headers=fixture.headers("admin")
            ).json()["candidates"][0]["candidate_id"]
            dismiss = fixture.client.post(
                f"/v12/crystals/{cid}/dismiss", headers=fixture.headers("admin"),
                json={"reason": "api rejected"},
            )
            self.assertEqual(dismiss.status_code, 200, dismiss.text)
            after = fixture.client.post(
                f"/v12/crystals/{cid}/approve", headers=fixture.headers("admin")
            )
            self.assertEqual(after.status_code, 409, after.text)


class TestR7V12MCP(unittest.TestCase):
    """M3c: MCP surface exposes 15+ tools incl. v12 trace/evolve/conflict/crystal."""

    def test_tool_definitions_include_v12_insight_tools(self):
        from mimir_v8.mcp import tool_definitions
        tools = {t["name"] for t in tool_definitions()}
        self.assertGreaterEqual(len(tools), 15)
        for name in (
            "mimir_search_trace", "mimir_evolve_feedback", "mimir_evolve_report",
            "mimir_conflict_detect", "mimir_conflict_list", "mimir_conflict_resolve",
            "mimir_conflict_dismiss", "mimir_crystal_scan", "mimir_crystal_list",
            "mimir_crystal_approve", "mimir_crystal_dismiss",
        ):
            self.assertIn(name, tools)

    def test_mcp_dispatch_delegates_to_client(self):
        from mimir_v8 import mcp as mcp_mod
        from mimir_v8.client import APIClientError
        calls = []

        class FakeAPI:
            def search_trace(self, text, *, limit=10, dedup_threshold=0.8,
                             candidate_limit=50):
                calls.append(("trace", text))
                return {"status": "ok", "pool_size": 5}

            def error_tool(self):
                raise APIClientError("boom", status_code=403, code="forbidden")

        server = mcp_mod.MimirMCPServer(api_client=FakeAPI())
        resp = server.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "mimir_search_trace",
                       "arguments": {"text": "N100"}},
        })
        self.assertEqual(resp["id"], 1)
        self.assertIn("pool_size", resp["result"]["content"][0]["text"])
        self.assertEqual(calls, [("trace", "N100")])

    def test_mcp_unknown_tool_returns_error(self):
        from mimir_v8 import mcp as mcp_mod
        server = mcp_mod.MimirMCPServer(api_client=object())
        resp = server.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "mimir_nope", "arguments": {}},
        })
        self.assertEqual(resp["error"]["code"], "invalid_params")

    def test_mcp_tools_list_roundtrip(self):
        from mimir_v8 import mcp as mcp_mod
        server = mcp_mod.MimirMCPServer(api_client=object())
        resp = server.handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
        })
        self.assertGreaterEqual(len(resp["result"]["tools"]), 15)


class TestR7V12MultiModalAPI(unittest.TestCase):
    """v12 /v12/facts/{id}/assets — attach/list multi-modal assets."""

    def _seed_published_fact(self, fixture):
        r = fixture.client.post("/v8/facts", headers=fixture.headers("admin"), json={
            "content": "N100 server handles all traffic at high throughput",
            "owner_principal": "mentor", "domain": "infrastructure",
            "fact_type": "pattern", "summary": "N100 capacity", "visibility": "all",
            "sensitivity": "internal", "egress_policy": "redacted_external",
            "human_status": "confirmed",
        })
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["fact_id"]

    def test_asset_requires_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            fid = self._seed_published_fact(fixture)
            r = fixture.client.post(
                f"/v12/facts/{fid}/assets", headers=fixture.headers("mentor"),
                json={"asset_kind": "image", "asset_ref": "assets/x.png"},
            )
            self.assertEqual(r.status_code, 403, r.text)

    def test_asset_requires_kind_and_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            fid = self._seed_published_fact(fixture)
            r = fixture.client.post(
                f"/v12/facts/{fid}/assets", headers=fixture.headers("admin"),
                json={},
            )
            self.assertEqual(r.status_code, 422, r.text)

    def test_attach_and_list_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = R7APIFixture(Path(tmp), enabled_layers=("memory", "learning"))
            fid = self._seed_published_fact(fixture)
            attach = fixture.client.post(
                f"/v12/facts/{fid}/assets", headers=fixture.headers("admin"),
                json={"asset_kind": "image", "asset_ref": "assets/dash.png"},
            )
            self.assertEqual(attach.status_code, 200, attach.text)
            lst = fixture.client.get(
                f"/v12/facts/{fid}/assets", headers=fixture.headers("admin")
            ).json()
            self.assertEqual(len(lst["assets"]), 1)
            self.assertEqual(lst["assets"][0]["kind"], "image")
            self.assertEqual(lst["assets"][0]["asset_ref"], "assets/dash.png")


class TestR7RuntimeWiring(unittest.TestCase):
    def test_runtime_wires_optional_layers_without_starting_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "runtime-token"
            token_path = root / "tokens.json"
            token_path.write_text(json.dumps({"principals": [{
                "id": "mentor",
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "scopes": ["read", "write", "ingest"],
                "roles": [],
                "admin": False,
            }]}), encoding="utf-8")
            app, components = build_runtime(
                root / "data",
                token_path,
                vector_enabled=False,
                start_supervisor=False,
                enabled_knowledge_layers=("memory", "learning"),
            )
            self.assertIsNotNone(app.state.context.knowledge)
            self.assertEqual(
                app.state.context.unified_search.enabled_layers,
                ("memory", "learning"),
            )
            self.assertFalse(components.supervisor._thread)


if __name__ == "__main__":
    unittest.main()
