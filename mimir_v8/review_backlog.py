"""Batch triage for the human_review backlog (1.9.1).

``review-backlog list`` is read-only. ``review-backlog apply`` is a dry run
unless ``--execute`` is given. Every real decision goes through
``CandidateService.review_candidate``, the same audited path the HTTP review
endpoint uses, so memory_events, review_actions and audit_log stay consistent.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .candidates import CandidateService, ReviewCandidate
from .learning import redact_text
from .review import ReviewQueue
from .store import CanonicalStore, sha256_text

REVIEW_ACTIONS = ("approve", "reject")
RISK_LEVELS = ("critical", "high", "medium", "low")
SENSITIVE_LEVELS = frozenset({"confidential", "restricted"})
PREVIEW_CHARS = 80

BRIDGE_NOISE_TAGS = ("bridge_context", "bridge_instructions", "tool_call", "system-reminder")
_NOISE_PREFIXES = tuple(f"<{tag}>" for tag in BRIDGE_NOISE_TAGS)
_NOISE_BLOCK_RE = re.compile(
    r"<(" + "|".join(re.escape(tag) for tag in BRIDGE_NOISE_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([hd])\s*$")

# Coarse buckets for the free-text reason. fast-track writes candidate_facts.review_reason;
# governance only writes governance_decisions.reason, so load_backlog falls back to the latest one.
_REASON_CODES = (
    ("判重", "duplicate_gate"),
    ("高风险", "high_risk"),
    ("评估不可用", "llm_unavailable"),
    ("无价值", "low_confidence_noise"),
    ("需人工确认", "needs_human_confirmation"),
)


def is_bridge_noise(content: str) -> bool:
    """True when a candidate is raw bridge scaffolding, not something a human said.

    Matches when the text starts with one of the bridge tags, or when closed
    tagged blocks make up more than half of it. Filtering only: nothing is acted on.
    """
    text = (content or "").lstrip()
    if not text:
        return False
    if text.startswith(_NOISE_PREFIXES):
        return True
    if _NOISE_BLOCK_RE.search(text) is None:
        return False
    remainder = _NOISE_BLOCK_RE.sub("", text).strip()
    return len(remainder) * 2 < len(text)


def reason_code(reason: str) -> str:
    if not reason:
        return "unspecified"
    for needle, code in _REASON_CODES:
        if needle in reason:
            return code
    return "other"


def parse_older_than(raw: str) -> float | None:
    """'72h' -> 72.0, '3d' -> 72.0. Empty means no age filter."""
    if not raw:
        return None
    match = _DURATION_RE.match(raw)
    if not match:
        raise ValueError(f"--older-than must look like 72h or 3d, got {raw!r}")
    amount, unit = int(match.group(1)), match.group(2)
    return float(amount * (24 if unit == "d" else 1))


def _preview(content: str, sensitivity: str) -> str:
    """Short preview that respects the candidate's sensitivity.

    confidential/restricted content is never shown; everything else passes
    through the shared learning.redact_text rules before it is truncated.
    """
    if sensitivity in SENSITIVE_LEVELS:
        return f"[redacted: sensitivity={sensitivity}]"
    text = " ".join(redact_text(content or "").text.split())
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


@dataclass(frozen=True)
class BacklogItem:
    candidate_id: str
    owner: str
    domain: str
    fact_type: str
    risk: str
    sensitivity: str
    source: str
    reason: str
    reason_code: str
    wait_hours: float
    bridge_noise: bool
    preview: str


def load_backlog(store: CanonicalStore) -> list[BacklogItem]:
    """Every candidate currently in human_review, oldest first."""
    now = datetime.now(timezone.utc)
    with closing(store.connect()) as connection:
        rows = connection.execute(
            """SELECT c.candidate_id, c.content, c.proposed_owner_principal,
                      c.proposed_domain, c.proposed_fact_type, c.proposed_sensitivity,
                      c.uncertainty_json,
                      COALESCE(NULLIF(c.review_reason, ''),
                               (SELECT g.reason FROM governance_decisions g
                                 WHERE g.candidate_id = c.candidate_id
                                 ORDER BY g.created_at DESC LIMIT 1)) AS review_reason,
                      c.created_at,
                      COALESCE(s.source_kind, 'unknown') AS source_kind
               FROM candidate_facts c
               LEFT JOIN sources s ON s.source_id = c.source_id
               WHERE c.status = 'human_review'
               ORDER BY c.created_at ASC"""
        ).fetchall()

    items: list[BacklogItem] = []
    for row in rows:
        fact_type = str(row["proposed_fact_type"] or "")
        domain = str(row["proposed_domain"] or "")
        sensitivity = str(row["proposed_sensitivity"] or "internal")
        try:
            uncertainty = ReviewQueue._parse_uncertainty(row["uncertainty_json"])
        except ValueError:
            uncertainty = {}
        try:
            created = ReviewQueue._parse_created_at(row["created_at"])
            wait = max(0.0, (now - created).total_seconds() / 3600)
        except ValueError:
            wait = 0.0
        content = str(row["content"] or "")
        reason = str(row["review_reason"] or "")
        items.append(BacklogItem(
            candidate_id=str(row["candidate_id"]),
            owner=str(row["proposed_owner_principal"] or ""),
            domain=domain,
            fact_type=fact_type,
            risk=ReviewQueue._infer_risk(fact_type, domain, uncertainty),
            sensitivity=sensitivity,
            source=str(row["source_kind"]),
            reason=reason,
            reason_code=reason_code(reason),
            wait_hours=round(wait, 1),
            bridge_noise=is_bridge_noise(content),
            preview=_preview(content, sensitivity),
        ))
    return items


def select_items(
    items: list[BacklogItem],
    *,
    owner: str = "",
    source: str = "",
    older_than_h: float | None = None,
    risk: str = "",
    code: str = "",
    bridge_noise: bool = False,
) -> list[BacklogItem]:
    selected = []
    for item in items:
        if owner and item.owner != owner:
            continue
        if source and item.source != source:
            continue
        if older_than_h is not None and item.wait_hours < older_than_h:
            continue
        if risk and item.risk != risk:
            continue
        if code and item.reason_code != code:
            continue
        if bridge_noise and not item.bridge_noise:
            continue
        selected.append(item)
    return selected


def summarize(items: list[BacklogItem]) -> dict:
    return {
        "total": len(items),
        "by_source": dict(Counter(item.source for item in items)),
        "by_reason": dict(Counter(item.reason_code for item in items)),
        "bridge_noise": sum(1 for item in items if item.bridge_noise),
        "oldest_hours": max((item.wait_hours for item in items), default=0.0),
    }


def _age(hours: float) -> str:
    return f"{hours / 24:.1f}d" if hours >= 24 else f"{hours:.0f}h"


def apply_decisions(
    store: CanonicalStore,
    candidate_ids: list[str],
    *,
    action: str,
    reason: str,
    actor: str,
    previews: dict[str, str],
    execute: bool,
) -> list[dict]:
    """Run one review decision per candidate. A failing item is recorded and the batch continues."""
    service = CandidateService(store)
    reason_hash = sha256_text(reason)[:16]
    results: list[dict] = []
    for candidate_id in candidate_ids:
        if not execute:
            results.append({"candidate_id": candidate_id, "outcome": "dry_run",
                            "action": action, "preview": previews.get(candidate_id, "")})
            continue
        try:
            outcome = service.review_candidate(
                ReviewCandidate(
                    candidate_id=candidate_id,
                    action=action,
                    reason=reason,
                    idempotency_key=f"backlog-{action}-{candidate_id}-{reason_hash}",
                ),
                actor,
            )
        except Exception as exc:  # noqa: BLE001 - isolate per item; reported and fails the run
            results.append({"candidate_id": candidate_id, "outcome": "failed",
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        fact_id = outcome.get("fact_id")
        if action == "approve" and outcome.get("status") == "approved":
            # Nothing else commits an approved candidate; governance does the
            # same review -> commit pair (governance.py fast-track).
            try:
                committed = service.commit_approved(
                    candidate_id, actor, f"backlog-commit-{candidate_id}-{reason_hash}")
            except Exception as exc:  # noqa: BLE001 - isolate per item; reported and fails the run
                results.append({"candidate_id": candidate_id, "outcome": "failed",
                                "status": "approved",
                                "error": f"commit failed: {type(exc).__name__}: {exc}"})
                continue
            fact_id = committed.get("fact_id")
        results.append({
            "candidate_id": candidate_id,
            "outcome": "applied",
            "action": action,
            "status": "committed" if action == "approve" and fact_id else outcome.get("status"),
            "fact_id": fact_id,
            "idempotent_replay": bool(outcome.get("idempotent_replay")),
        })
    return results


def _filters_from(args) -> dict:
    return {
        "owner": args.owner,
        "source": args.source,
        "risk": args.risk,
        "code": args.reason_code,
        "bridge_noise": args.bridge_noise,
    }


def _run_list(store: CanonicalStore, args) -> int:
    items = select_items(load_backlog(store), older_than_h=parse_older_than(args.older_than),
                         **_filters_from(args))
    summary = summarize(items)
    shown = items[: args.limit]
    if args.json:
        print(json.dumps({"command": "review-backlog list", **summary,
                          "items": [asdict(item) for item in shown]}, ensure_ascii=False))
        return 0
    print(f"review backlog: {summary['total']} item(s), bridge_noise={summary['bridge_noise']}, "
          f"oldest={_age(summary['oldest_hours'])}")
    for label in ("by_source", "by_reason"):
        for key, count in sorted(summary[label].items(), key=lambda kv: -kv[1]):
            print(f"  {label[3:]}={key}: {count}")
    for item in shown:
        print(f"{item.candidate_id[:12]}  {_age(item.wait_hours):>6}  owner={item.owner}  "
              f"source={item.source}  reason={item.reason_code}  risk={item.risk}  "
              f"noise={'y' if item.bridge_noise else 'n'}  {item.preview}")
    return 0


def resolve_reviewer(args) -> tuple[str, str]:
    """Return ``(actor, error)``; the actor comes from a token, never from a flag.

    1.9.2: ``--actor`` used to be trusted as typed, so any local process could
    file review decisions under any principal (2026-11-25: Thor's approved
    backlog cleanup was recorded as ``mentor``). The reviewer is now whoever
    owns the presented token, and that principal must hold the ``review`` scope.
    ``--actor`` may still be passed, but only as an assertion that must match.
    """
    from .auth import AuthError, TokenStore

    registry = (getattr(args, "registry", "") or "").strip()
    token_file = (getattr(args, "token_file", "") or "").strip()
    token = os.environ.get("MIMIR_REVIEW_TOKEN", "").strip()
    if not token and token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            return "", f"cannot read --token-file: {exc}"
    if not registry or not token:
        return "", ("a reviewer token is required: pass --registry (or MIMIR_V8_TOKEN_FILE) and "
                     "--token-file (or MIMIR_REVIEW_TOKEN); the reviewer is the token's principal")
    try:
        principal = TokenStore(registry).authenticate(f"Bearer {token}")
        principal.require("review")
    except AuthError as exc:
        return "", f"reviewer token rejected: {exc}"
    claimed = (getattr(args, "actor", "") or "").strip()
    if claimed and claimed != principal.principal_id:
        return "", f"--actor {claimed!r} does not match the token's principal {principal.principal_id!r}"
    return principal.principal_id, ""


def _run_apply(store: CanonicalStore, args) -> int:
    reason = (args.reason or "").strip()
    if not reason:
        print("error: --reason must not be empty", file=sys.stderr)
        return 2
    actor, err = resolve_reviewer(args)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    explicit_ids = list(dict.fromkeys(x.strip() for x in args.ids.split(",") if x.strip()))
    filters = _filters_from(args)
    has_filter = bool(args.owner or args.source or args.older_than or args.risk
                      or args.reason_code or args.bridge_noise)
    if not explicit_ids and not has_filter:
        print("error: refusing to act on the whole backlog; pass --ids or a filter", file=sys.stderr)
        return 2
    try:
        older_than_h = parse_older_than(args.older_than)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    backlog = load_backlog(store)
    by_id = {item.candidate_id: item for item in backlog}
    selected = select_items(backlog, older_than_h=older_than_h, **filters)
    selected_ids = {item.candidate_id for item in selected}

    results: list[dict] = []
    if explicit_ids:
        ids: list[str] = []
        for candidate_id in explicit_ids:
            if candidate_id not in by_id:
                results.append({"candidate_id": candidate_id, "outcome": "failed",
                                "error": "not in the human_review backlog"})
            elif has_filter and candidate_id not in selected_ids:
                results.append({"candidate_id": candidate_id, "outcome": "skipped",
                                "error": "excluded by filters"})
            else:
                ids.append(candidate_id)
    else:
        ids = [item.candidate_id for item in selected]

    previews = {cid: by_id[cid].preview for cid in ids}
    results.extend(apply_decisions(store, ids, action=args.action, reason=reason,
                                   actor=actor, previews=previews, execute=args.execute))
    failed = sum(1 for r in results if r["outcome"] == "failed")
    applied = sum(1 for r in results if r["outcome"] == "applied")
    print(json.dumps({
        "command": "review-backlog apply",
        "action": args.action,
        "execute": bool(args.execute),
        "selected": len(ids),
        "applied": applied,
        "failed": failed,
        "results": results,
    }, ensure_ascii=False))
    return 1 if failed else 0


def run_review_backlog(store: CanonicalStore, args) -> int:
    if args.backlog_command == "list":
        try:
            return _run_list(store, args)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return _run_apply(store, args)


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner", default="", help="proposed owner principal")
    parser.add_argument("--source", default="", help="sources.source_kind (unknown if no source row)")
    parser.add_argument("--older-than", default="", help="minimum age, e.g. 72h or 3d")
    parser.add_argument("--risk", default="", choices=("",) + RISK_LEVELS)
    parser.add_argument("--reason-code", default="",
                        help="duplicate_gate | high_risk | llm_unavailable | low_confidence_noise | needs_human_confirmation | other | unspecified")
    parser.add_argument("--bridge-noise", action="store_true",
                        help="only candidates that look like raw bridge scaffolding")


def add_review_backlog_parser(sub) -> None:
    """Attach the `review-backlog` subcommand to the worker's subparsers."""
    backlog = sub.add_parser("review-backlog", help="triage the human_review backlog")
    backlog_sub = backlog.add_subparsers(dest="backlog_command", required=True)

    listing = backlog_sub.add_parser("list", help="read-only listing with summary counts")
    _add_filters(listing)
    listing.add_argument("--limit", type=int, default=50, help="max items to print (default 50)")
    listing.add_argument("--json", action="store_true", help="machine-readable output")

    apply = backlog_sub.add_parser("apply", help="batch approve/reject; dry run unless --execute")
    _add_filters(apply)
    apply.add_argument("--ids", default="", help="comma-separated candidate ids")
    apply.add_argument("--action", required=True, choices=REVIEW_ACTIONS)
    apply.add_argument("--reason", required=True, help="recorded on every review decision")
    apply.add_argument("--actor", default=os.environ.get("MIMIR_REVIEW_ACTOR", ""),
                       help="optional assertion; must equal the token's principal")
    apply.add_argument("--registry", default=os.environ.get("MIMIR_V8_TOKEN_FILE", ""),
                       help="hash-only token registry the API uses (default: MIMIR_V8_TOKEN_FILE)")
    apply.add_argument("--token-file", default=os.environ.get("MIMIR_REVIEW_TOKEN_FILE", ""),
                       help="reviewer token file; or set MIMIR_REVIEW_TOKEN")
    apply.add_argument("--execute", action="store_true",
                       help="actually write decisions; without this nothing is changed")
