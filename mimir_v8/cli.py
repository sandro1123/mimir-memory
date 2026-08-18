"""Mímir v8 CLI using the same REST contract as MCP."""

from __future__ import annotations

import argparse
import json
import sys

from .client import APIClientError, MimirAPIClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mímir v8 federated memory CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="search canonical facts")
    query.add_argument("text")
    query.add_argument("--limit", "-n", type=int, default=10)
    query.add_argument("--owner")
    query.add_argument("--domain")
    query.add_argument("--type", dest="fact_type")
    query.add_argument("--no-vector", action="store_true")
    query.add_argument("--no-fts", action="store_true")
    query.add_argument("--no-graph", action="store_true")

    write = sub.add_parser("write", help="create a canonical fact")
    write.add_argument("content")
    write.add_argument("--agent", required=False)
    write.add_argument("--domain", default="system")
    write.add_argument("--type", dest="fact_type", default="event")
    write.add_argument("--summary")
    write.add_argument("--visibility", choices=("all", "shared", "owner_only"), default="all")
    write.add_argument("--sensitivity", choices=("internal", "confidential", "restricted"), default="internal")
    write.add_argument("--egress-policy", choices=("local_only", "redacted_external", "external_allowed"), default="local_only")
    write.add_argument("--idempotency-key")

    remember = sub.add_parser("remember", help="propose an explicit memory through DLP and review")
    remember.add_argument("content")
    remember.add_argument("--agent")
    remember.add_argument("--domain", default="personal")
    remember.add_argument("--type", dest="fact_type", default="user_pref")
    remember.add_argument("--summary")
    remember.add_argument("--retention-class", default="standard", choices=("session", "short", "standard", "permanent", "legal_hold"))
    remember.add_argument("--idempotency-key", required=True)

    ingest = sub.add_parser("ingest-conversation", help="ingest a conversation envelope JSON file")
    ingest.add_argument("file")

    forget = sub.add_parser("forget", help="tombstone an owned canonical fact")
    forget.add_argument("fact_id")
    forget.add_argument("--expected-version", type=int, required=True)
    forget.add_argument("--reason", required=True)
    forget.add_argument("--idempotency-key", required=True)

    correct = sub.add_parser("correct", help="propose a reviewed correction for a fact")
    correct.add_argument("fact_id")
    correct.add_argument("corrected_content")
    correct.add_argument("--expected-version", type=int, required=True)
    correct.add_argument("--summary")
    correct.add_argument("--reason", required=True)
    correct.add_argument("--idempotency-key", required=True)

    feedback = sub.add_parser("feedback", help="submit learning feedback")
    target = feedback.add_mutually_exclusive_group(required=True)
    target.add_argument("--candidate-id")
    target.add_argument("--fact-id")
    feedback.add_argument("--type", dest="feedback_type", required=True,
                          choices=("useful", "incorrect", "stale", "duplicate", "harmful", "withdraw"))
    feedback.add_argument("--text", dest="feedback_text", required=True)
    feedback.add_argument("--idempotency-key", required=True)

    candidates = sub.add_parser("candidates", help="list memory candidates")
    candidates.add_argument("--status")
    candidates.add_argument("--limit", type=int, default=50)

    review = sub.add_parser("review-candidate", help="review a memory candidate")
    review.add_argument("candidate_id")
    review.add_argument("action", choices=("approve", "reject", "needs_more_evidence"))
    review.add_argument("--reason", required=True)
    review.add_argument("--idempotency-key", required=True)

    commit = sub.add_parser("commit-candidate", help="commit an approved candidate")
    commit.add_argument("candidate_id")
    commit.add_argument("--idempotency-key", required=True)

    sub.add_parser("learning-status", help="show ingestion and learning watermarks")

    awareness = sub.add_parser("awareness", help="show ACL-filtered cross-agent updates")
    awareness.add_argument("--agent")
    awareness.add_argument("--hours", type=int)
    sub.add_parser("stats", help="show visible statistics")
    sub.add_parser("health", help="show service health")
    sub.add_parser("ready", help="show readiness and projector state")
    inject = sub.add_parser("core-memory", help="render CoreMemory injection")
    inject.add_argument("agent")
    inject.add_argument("--max-chars", type=int, default=2000)
    return parser


def run(args, client: MimirAPIClient) -> int:
    if args.command == "query":
        result = client.query(
            args.text, limit=args.limit, owner_principal=args.owner,
            domain=args.domain, fact_type=args.fact_type,
            use_vector=not args.no_vector, use_fts=not args.no_fts,
            use_graph=not args.no_graph,
        )
    elif args.command == "write":
        result = client.create_fact({
            "content": args.content, "owner_principal": args.agent or client.config.principal_id,
            "domain": args.domain, "fact_type": args.fact_type, "summary": args.summary,
            "visibility": args.visibility, "sensitivity": args.sensitivity,
            "egress_policy": args.egress_policy, "idempotency_key": args.idempotency_key,
        })
    elif args.command == "remember":
        result = client.remember({
            "content": args.content,
            "owner_principal": args.agent or client.config.principal_id,
            "domain": args.domain,
            "fact_type": args.fact_type,
            "summary": args.summary,
            "retention_class": args.retention_class,
            "idempotency_key": args.idempotency_key,
        })
    elif args.command == "ingest-conversation":
        with open(args.file, "r", encoding="utf-8") as stream:
            result = client.ingest_conversation(json.load(stream))
    elif args.command == "forget":
        result = client.forget({
            "fact_id": args.fact_id,
            "expected_version": args.expected_version,
            "reason": args.reason,
            "idempotency_key": args.idempotency_key,
        })
    elif args.command == "correct":
        result = client.correct({
            "fact_id": args.fact_id,
            "expected_version": args.expected_version,
            "corrected_content": args.corrected_content,
            "summary": args.summary,
            "reason": args.reason,
            "idempotency_key": args.idempotency_key,
        })
    elif args.command == "feedback":
        result = client.submit_feedback({
            "candidate_id": args.candidate_id,
            "fact_id": args.fact_id,
            "feedback_type": args.feedback_type,
            "feedback_text": args.feedback_text,
            "idempotency_key": args.idempotency_key,
        })
    elif args.command == "candidates":
        result = client.list_candidates(status=args.status, limit=args.limit)
    elif args.command == "review-candidate":
        result = client.review_candidate(args.candidate_id, {
            "action": args.action,
            "reason": args.reason,
            "idempotency_key": args.idempotency_key,
        })
    elif args.command == "commit-candidate":
        result = client.commit_candidate(args.candidate_id, {"idempotency_key": args.idempotency_key})
    elif args.command == "learning-status":
        result = client.learning_status()
    elif args.command == "awareness":
        result = client.awareness(args.agent, args.hours)
    elif args.command == "stats":
        result = client.stats()
    elif args.command == "health":
        result = client.health()
    elif args.command == "ready":
        result = client.ready()
    elif args.command == "core-memory":
        result = client.core_memory_inject(args.agent, args.max_chars)
    else:
        raise ValueError(f"unknown command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return run(args, MimirAPIClient())
    except APIClientError as exc:
        print(json.dumps({
            "error": {"code": exc.code, "message": str(exc), "status_code": exc.status_code,
                      "details": exc.detail},
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": {"code": "cli_error", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
