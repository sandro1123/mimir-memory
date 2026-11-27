"""Clean up everything one connector fed into Mímir (1.9.3).

``source-purge --connector-id X`` is a dry run that lists what X produced:
conversation sources, messages, open candidates and committed facts.
``--execute`` then
  - rejects the open candidates through ``CandidateService.review_candidate``
    (the audited path the HTTP review endpoint uses), and
  - schedules retention jobs, due now, for X's conversation sources so the
    normal retention worker deletes them.
Committed facts are only listed, never touched here: retracting a fact is a
per-fact decision (``/v8/facts/{id}/tombstone``).
"""

from __future__ import annotations

import json
import sys
from contextlib import closing
from datetime import datetime, timezone

from .candidates import CandidateService, ReviewCandidate
from .retention import RetentionSchedule, RetentionService
from .store import CanonicalStore, sha256_text

CLOSED_CANDIDATE_STATUSES = ("committed", "rejected", "auto_rejected")


def _source_ids(connection, connector_id: str) -> list[str]:
    return [r[0] for r in connection.execute(
        "SELECT source_id FROM conversation_sources WHERE connector_id=? ORDER BY ingested_at",
        (connector_id,)).fetchall()]


def _in(ids: list[str]) -> str:
    return ",".join("?" for _ in ids)


def survey(store: CanonicalStore, connector_id: str) -> dict:
    """Read-only: what this connector produced."""
    with closing(store.connect()) as connection:
        sources = _source_ids(connection, connector_id)
        if not sources:
            return {"connector_id": connector_id, "sources": [], "message_count": 0,
                    "open_candidates": [], "committed_facts": []}
        ph = _in(sources)
        messages = connection.execute(
            f"SELECT count(*) FROM conversation_messages WHERE source_id IN ({ph})",
            tuple(sources)).fetchone()[0]
        rows = connection.execute(
            f"""SELECT DISTINCT c.candidate_id, c.status, c.committed_fact_id
                FROM candidate_facts c
                LEFT JOIN candidate_evidence e ON e.candidate_id = c.candidate_id
                WHERE c.source_id IN ({ph}) OR e.source_id IN ({ph})""",
            tuple(sources) * 2).fetchall()
    open_candidates = sorted(r[0] for r in rows if r[1] not in CLOSED_CANDIDATE_STATUSES)
    committed = sorted({r[2] for r in rows if r[1] == "committed" and r[2]})
    return {"connector_id": connector_id, "sources": sources, "message_count": messages,
            "open_candidates": open_candidates, "committed_facts": committed}


def purge(store: CanonicalStore, connector_id: str, *, reason: str, actor: str,
          execute: bool) -> dict:
    report = survey(store, connector_id)
    result = {"command": "source-purge", "execute": execute, "reason": reason,
              "connector_id": connector_id,
              "counts": {"sources": len(report["sources"]),
                         "messages": report["message_count"],
                         "open_candidates": len(report["open_candidates"]),
                         "committed_facts": len(report["committed_facts"])},
              "committed_facts": report["committed_facts"],
              "rejected": [], "scheduled": [], "already_scheduled": [], "failed": []}
    if not execute:
        return result
    key_salt = sha256_text(f"{connector_id}|{reason}")[:16]
    candidates = CandidateService(store)
    for candidate_id in report["open_candidates"]:
        try:
            candidates.review_candidate(ReviewCandidate(
                candidate_id=candidate_id, action="reject", reason=reason,
                idempotency_key=f"source-purge-{candidate_id}-{key_salt}"), actor)
            result["rejected"].append(candidate_id)
        except Exception as exc:  # noqa: BLE001 - isolate per item; reported and fails the run
            result["failed"].append({"resource": candidate_id, "error": f"{type(exc).__name__}: {exc}"})
    retention = RetentionService(store)
    due = datetime.now(timezone.utc).isoformat()
    with closing(store.connect()) as connection:
        already = {r[0] for r in connection.execute(
            "SELECT resource_id FROM retention_jobs WHERE resource_type='conversation_source'").fetchall()}
    for source_id in report["sources"]:
        if source_id in already:
            result["already_scheduled"].append(source_id)
            continue
        try:
            retention.schedule(RetentionSchedule(
                resource_type="conversation_source", resource_id=source_id, due_at=due,
                reason=reason, idempotency_key=f"source-purge-{source_id}-{key_salt}"), actor)
            result["scheduled"].append(source_id)
        except Exception as exc:  # noqa: BLE001
            result["failed"].append({"resource": source_id, "error": f"{type(exc).__name__}: {exc}"})
    return result


def run_source_purge(store: CanonicalStore, args) -> int:
    if args.execute and not args.reason.strip():
        print("error: --reason is required with --execute", file=sys.stderr)
        return 2
    result = purge(store, args.connector_id, reason=args.reason.strip() or "dry run",
                   actor=args.actor, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


def add_source_purge_parser(sub) -> None:
    p = sub.add_parser("source-purge", help="clean up everything one connector produced; dry run unless --execute")
    p.add_argument("--connector-id", required=True, help="conversation_sources.connector_id, e.g. mirasim-x1e")
    p.add_argument("--reason", default="", help="recorded on every rejection and retention job")
    p.add_argument("--actor", default="admin", help="principal recorded as the actor (default admin)")
    p.add_argument("--execute", action="store_true", help="actually reject candidates and schedule deletion")
