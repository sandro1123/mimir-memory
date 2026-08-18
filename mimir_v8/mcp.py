"""MCP stdio adapter backed exclusively by the v8 REST client."""

from __future__ import annotations

import json
import sys

from .client import APIClientError, MimirAPIClient
from .schema import MIMIR_VERSION

MCP_VERSION = "2025-03-26"


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "mimir_query",
            "description": "Search ACL-filtered Mímir v8 canonical facts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"}, "limit": {"type": "integer", "default": 10},
                    "owner_principal": {"type": "string"}, "domain": {"type": "string"},
                    "fact_type": {"type": "string"}, "use_vector": {"type": "boolean", "default": True},
                    "use_fts": {"type": "boolean", "default": True}, "use_graph": {"type": "boolean", "default": True},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mimir_write",
            "description": "Create a v8 candidate-free canonical fact for the caller principal.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"}, "owner_principal": {"type": "string"},
                    "domain": {"type": "string"}, "fact_type": {"type": "string"},
                    "summary": {"type": "string"}, "visibility": {"enum": ["all", "shared", "owner_only"]},
                    "sensitivity": {"enum": ["internal", "confidential", "restricted"]},
                    "egress_policy": {"enum": ["local_only", "redacted_external", "external_allowed"]},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["content", "domain", "fact_type"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mimir_remember",
            "description": "Propose an explicit owner-only memory through DLP and Candidate review.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"}, "owner_principal": {"type": "string"},
                    "domain": {"type": "string", "default": "personal"},
                    "fact_type": {"type": "string", "default": "user_pref"},
                    "summary": {"type": "string"},
                    "retention_class": {"enum": ["session", "short", "standard", "permanent", "legal_hold"]},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["content", "idempotency_key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mimir_ingest_conversation",
            "description": "Ingest a DLP-redacted conversation envelope without writing canonical facts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector_type": {"type": "string"}, "connector_id": {"type": "string"},
                    "session_id": {"type": "string"}, "owner_principal": {"type": "string"},
                    "memory_mode": {"enum": ["explicit", "observe", "never"]},
                    "retention_class": {"enum": ["session", "short", "standard", "permanent", "legal_hold"]},
                    "messages": {"type": "array", "items": {"type": "object"}},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["connector_type", "connector_id", "messages", "idempotency_key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "mimir_forget",
            "description": "Tombstone an owned canonical fact after ACL and version checks.",
            "inputSchema": {"type": "object", "properties": {
                "fact_id": {"type": "string"}, "expected_version": {"type": "integer"},
                "reason": {"type": "string"}, "idempotency_key": {"type": "string"},
            }, "required": ["fact_id", "expected_version", "reason", "idempotency_key"], "additionalProperties": False},
        },
        {
            "name": "mimir_correct",
            "description": "Propose a DLP-redacted correction that must pass Candidate review.",
            "inputSchema": {"type": "object", "properties": {
                "fact_id": {"type": "string"}, "expected_version": {"type": "integer"},
                "corrected_content": {"type": "string"}, "summary": {"type": "string"},
                "reason": {"type": "string"}, "idempotency_key": {"type": "string"},
            }, "required": ["fact_id", "expected_version", "corrected_content", "reason", "idempotency_key"], "additionalProperties": False},
        },
        {
            "name": "mimir_feedback",
            "description": "Submit redacted, idempotent learning feedback for a candidate or fact.",
            "inputSchema": {"type": "object", "properties": {
                "candidate_id": {"type": "string"}, "fact_id": {"type": "string"},
                "feedback_type": {"enum": ["useful", "incorrect", "stale", "duplicate", "harmful", "withdraw"]},
                "feedback_text": {"type": "string"}, "idempotency_key": {"type": "string"},
            }, "required": ["feedback_type", "feedback_text", "idempotency_key"], "additionalProperties": False},
        },
        {
            "name": "mimir_candidates",
            "description": "List the caller-visible Candidate review queue.",
            "inputSchema": {"type": "object", "properties": {
                "status": {"type": "string"}, "limit": {"type": "integer", "default": 50},
            }, "additionalProperties": False},
        },
        {
            "name": "mimir_review_candidate",
            "description": "Approve, reject, or request evidence for a Candidate.",
            "inputSchema": {"type": "object", "properties": {
                "candidate_id": {"type": "string"},
                "action": {"enum": ["approve", "reject", "needs_more_evidence"]},
                "reason": {"type": "string"}, "idempotency_key": {"type": "string"},
            }, "required": ["candidate_id", "action", "reason", "idempotency_key"], "additionalProperties": False},
        },
        {
            "name": "mimir_commit_candidate",
            "description": "Commit an approved Candidate into canonical memory.",
            "inputSchema": {"type": "object", "properties": {
                "candidate_id": {"type": "string"}, "idempotency_key": {"type": "string"},
            }, "required": ["candidate_id", "idempotency_key"], "additionalProperties": False},
        },
        {
            "name": "mimir_learning_status",
            "description": "Read v8.1 ingestion, candidate, feedback, and retention watermarks.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "mimir_awareness",
            "description": "Read ACL-filtered shared updates without reading push files.",
            "inputSchema": {"type": "object", "properties": {
                "agent_id": {"type": "string"}, "hours": {"type": "integer"},
            }, "additionalProperties": False},
        },
        {
            "name": "mimir_stats",
            "description": "Read statistics visible to the authenticated principal.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "mimir_core_memory_inject",
            "description": "Read owner-only CoreMemory injection for the authenticated agent.",
            "inputSchema": {"type": "object", "properties": {
                "agent_id": {"type": "string"}, "max_chars": {"type": "integer", "default": 2000},
            }, "required": ["agent_id"], "additionalProperties": False},
        },

        # ── v12 Insight tools (M2 recall funnel + M3 governance) ─────────
        {
            "name": "mimir_search_trace",
            "description": "v12 recall funnel trace: candidate pool, relevance gate, Jaccard dedup, Chronos decay, top-K.",
            "inputSchema": {"type": "object", "properties": {
                "text": {"type": "string"}, "limit": {"type": "integer", "default": 10},
                "dedup_threshold": {"type": "number", "default": 0.8},
                "candidate_limit": {"type": "integer", "default": 50},
            }, "required": ["text"], "additionalProperties": False},
        },
        {
            "name": "mimir_evolve_feedback",
            "description": "v12 EvolveMem: submit a useful / useless / correction signal on a search result.",
            "inputSchema": {"type": "object", "properties": {
                "query_text": {"type": "string"}, "fact_id": {"type": "string"},
                "signal": {"enum": ["useful", "useless", "correction"]},
                "user_principal": {"type": "string"},
            }, "required": ["query_text", "fact_id", "signal"], "additionalProperties": False},
        },
        {
            "name": "mimir_evolve_report",
            "description": "v12 EvolveMem: 7-day retrieval quality report (queries, hits, signals).",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "mimir_conflict_detect",
            "description": "v12: scan active facts for near-duplicate contradiction pairs.",
            "inputSchema": {"type": "object", "properties": {
                "threshold": {"type": "number", "default": 0.6},
            }, "additionalProperties": False},
        },
        {
            "name": "mimir_conflict_list",
            "description": "v12: list conflict resolutions by status.",
            "inputSchema": {"type": "object", "properties": {
                "status": {"enum": ["open", "resolved", "dismissed"], "default": "open"},
                "limit": {"type": "integer", "default": 50},
            }, "additionalProperties": False},
        },
        {
            "name": "mimir_conflict_resolve",
            "description": "v12: resolve a conflict; the loser is marked disputed (never deleted).",
            "inputSchema": {"type": "object", "properties": {
                "conflict_id": {"type": "string"}, "winner_fact_id": {"type": "string"},
                "reason": {"type": "string"},
            }, "required": ["conflict_id", "winner_fact_id"], "additionalProperties": False},
        },
        {
            "name": "mimir_conflict_dismiss",
            "description": "v12: close a conflict without changing any fact status.",
            "inputSchema": {"type": "object", "properties": {
                "conflict_id": {"type": "string"}, "reason": {"type": "string"},
            }, "required": ["conflict_id"], "additionalProperties": False},
        },
        {
            "name": "mimir_crystal_scan",
            "description": "v12: cluster recent facts by topic into skill-crystallization candidates.",
            "inputSchema": {"type": "object", "properties": {
                "window_days": {"type": "integer", "default": 7},
                "min_freq": {"type": "integer", "default": 3},
            }, "additionalProperties": False},
        },
        {
            "name": "mimir_crystal_list",
            "description": "v12: list skill-crystallization candidates by status.",
            "inputSchema": {"type": "object", "properties": {
                "status": {"enum": ["candidate", "approved", "dismissed"], "default": "candidate"},
                "limit": {"type": "integer", "default": 50},
            }, "additionalProperties": False},
        },
        {
            "name": "mimir_crystal_approve",
            "description": "v12: human approval materializes a crystallized skill as a pattern fact.",
            "inputSchema": {"type": "object", "properties": {
                "candidate_id": {"type": "string"}, "owner_principal": {"type": "string"},
            }, "required": ["candidate_id"], "additionalProperties": False},
        },
        {
            "name": "mimir_crystal_dismiss",
            "description": "v12: reject a skill-crystallization candidate.",
            "inputSchema": {"type": "object", "properties": {
                "candidate_id": {"type": "string"}, "reason": {"type": "string"},
            }, "required": ["candidate_id"], "additionalProperties": False},
        },

        # ── v12 Multi-modal assets (M4) ─────────────────────────────────
        {
            "name": "mimir_asset_attach",
            "description": "v12 multi-modal: attach a media asset reference (image/audio/document/file) to a publishable fact.",
            "inputSchema": {"type": "object", "properties": {
                "fact_id": {"type": "string"},
                "asset_kind": {"enum": ["image", "audio", "document", "file"]},
                "asset_ref": {"type": "string"},
            }, "required": ["fact_id", "asset_kind", "asset_ref"], "additionalProperties": False},
        },
        {
            "name": "mimir_asset_list",
            "description": "v12 multi-modal: list asset references attached to a fact.",
            "inputSchema": {"type": "object", "properties": {
                "fact_id": {"type": "string"},
            }, "required": ["fact_id"], "additionalProperties": False},
        },
    ]


class MimirMCPServer:
    def __init__(self, api_client: MimirAPIClient | None = None):
        self.api = api_client or MimirAPIClient()

    @staticmethod
    def _result(req_id, data):
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        }}

    @staticmethod
    def _error(req_id, code, message, data=None):
        return {"jsonrpc": "2.0", "id": req_id, "error": {
            "code": code, "message": message, "data": data,
        }}

    def handle_request(self, request: dict) -> dict:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": MCP_VERSION,
                "serverInfo": {"name": "mimir-v8-mcp", "version": MIMIR_VERSION},
                "capabilities": {"tools": {}, "resources": {}},
            }}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tool_definitions()}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/call":
            return self._call_tool(req_id, params)
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": [
                {"uri": "mimir://health", "name": "Mímir health", "mimeType": "application/json"},
                {"uri": "mimir://stats", "name": "Mímir visible stats", "mimeType": "application/json"},
            ]}}
        if method == "resources/read":
            uri = params.get("uri")
            try:
                data = self.api.health() if uri == "mimir://health" else self.api.stats() if uri == "mimir://stats" else None
            except APIClientError as exc:
                return self._error(req_id, exc.code, str(exc), exc.detail)
            if data is None:
                return self._error(req_id, "invalid_params", f"resource not found: {uri}")
            return {"jsonrpc": "2.0", "id": req_id, "result": {"contents": [
                {"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False)},
            ]}}
        return self._error(req_id, "method_not_found", f"method not found: {method}")

    def _call_tool(self, req_id, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "mimir_query":
                data = self.api.query(
                    args.get("text", ""), limit=args.get("limit", 10),
                    owner_principal=args.get("owner_principal"), domain=args.get("domain"),
                    fact_type=args.get("fact_type"), use_vector=args.get("use_vector", True),
                    use_fts=args.get("use_fts", True), use_graph=args.get("use_graph", True),
                )
            elif name == "mimir_write":
                data = self.api.create_fact({
                    "content": args["content"], "owner_principal": args.get("owner_principal", self.api.config.principal_id),
                    "domain": args["domain"], "fact_type": args["fact_type"], "summary": args.get("summary"),
                    "visibility": args.get("visibility", "all"), "sensitivity": args.get("sensitivity", "internal"),
                    "egress_policy": args.get("egress_policy", "local_only"), "idempotency_key": args.get("idempotency_key"),
                })
            elif name == "mimir_remember":
                data = self.api.remember({
                    "content": args["content"],
                    "owner_principal": args.get("owner_principal", self.api.config.principal_id),
                    "domain": args.get("domain", "personal"),
                    "fact_type": args.get("fact_type", "user_pref"),
                    "summary": args.get("summary"),
                    "retention_class": args.get("retention_class", "standard"),
                    "idempotency_key": args["idempotency_key"],
                })
            elif name == "mimir_ingest_conversation":
                data = self.api.ingest_conversation(args)
            elif name == "mimir_forget":
                data = self.api.forget(args)
            elif name == "mimir_correct":
                data = self.api.correct(args)
            elif name == "mimir_feedback":
                data = self.api.submit_feedback(args)
            elif name == "mimir_candidates":
                data = self.api.list_candidates(status=args.get("status"), limit=args.get("limit", 50))
            elif name == "mimir_review_candidate":
                data = self.api.review_candidate(args["candidate_id"], {
                    "action": args["action"], "reason": args["reason"],
                    "idempotency_key": args["idempotency_key"],
                })
            elif name == "mimir_commit_candidate":
                data = self.api.commit_candidate(args["candidate_id"], {
                    "idempotency_key": args["idempotency_key"],
                })
            elif name == "mimir_learning_status":
                data = self.api.learning_status()
            elif name == "mimir_awareness":
                data = self.api.awareness(args.get("agent_id"), args.get("hours"))
            elif name == "mimir_stats":
                data = self.api.stats()
            elif name == "mimir_core_memory_inject":
                data = self.api.core_memory_inject(args["agent_id"], args.get("max_chars", 2000))
            elif name == "mimir_search_trace":
                data = self.api.search_trace(
                    args["text"], limit=args.get("limit", 10),
                    dedup_threshold=args.get("dedup_threshold", 0.8),
                    candidate_limit=args.get("candidate_limit", 50),
                )
            elif name == "mimir_evolve_feedback":
                data = self.api.evolve_feedback(
                    args["query_text"], args["fact_id"], args["signal"],
                    user_principal=args.get("user_principal"),
                )
            elif name == "mimir_evolve_report":
                data = self.api.evolve_report()
            elif name == "mimir_conflict_detect":
                data = self.api.conflict_detect(threshold=args.get("threshold", 0.6))
            elif name == "mimir_conflict_list":
                data = self.api.conflict_list(status=args.get("status", "open"),
                                              limit=args.get("limit", 50))
            elif name == "mimir_conflict_resolve":
                data = self.api.conflict_resolve(
                    args["conflict_id"], args["winner_fact_id"],
                    reason=args.get("reason", ""),
                )
            elif name == "mimir_conflict_dismiss":
                data = self.api.conflict_dismiss(args["conflict_id"],
                                                 reason=args.get("reason", ""))
            elif name == "mimir_crystal_scan":
                data = self.api.crystal_scan(window_days=args.get("window_days", 7),
                                             min_freq=args.get("min_freq", 3))
            elif name == "mimir_crystal_list":
                data = self.api.crystal_list(status=args.get("status", "candidate"),
                                             limit=args.get("limit", 50))
            elif name == "mimir_crystal_approve":
                data = self.api.crystal_approve(args["candidate_id"],
                                                owner_principal=args.get("owner_principal"))
            elif name == "mimir_crystal_dismiss":
                data = self.api.crystal_dismiss(args["candidate_id"],
                                                reason=args.get("reason", ""))
            elif name == "mimir_asset_attach":
                data = self.api.asset_attach(args["fact_id"], args["asset_kind"],
                                             args["asset_ref"])
            elif name == "mimir_asset_list":
                data = self.api.asset_list(args["fact_id"])
            else:
                return self._error(req_id, "invalid_params", f"unknown tool: {name}")
            return self._result(req_id, data)
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(req_id, "invalid_params", str(exc))
        except APIClientError as exc:
            return self._error(req_id, exc.code, str(exc), exc.detail)

    def run_stdio(self, stream_in=None, stream_out=None):
        stream_in = stream_in or sys.stdin
        stream_out = stream_out or sys.stdout
        for line in stream_in:
            if not line.strip():
                continue
            request = None
            try:
                request = json.loads(line)
                response = self.handle_request(request)
            except json.JSONDecodeError:
                response = self._error(None, "parse_error", "invalid JSON")
            if request is None or request.get("id") is not None:
                stream_out.write(json.dumps(response, ensure_ascii=False) + "\n")
                stream_out.flush()


def main() -> int:
    MimirMCPServer().run_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
