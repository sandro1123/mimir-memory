"""Operational workers for Mímir v8.2 production."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .governance import run_governance_once, fast_track_commit_all
from .opinion import OpinionService
from .classifier import classify
from .config import MimirPaths
from .connectors import HermesStateCDC
from .evaluator import Evaluator
from .extraction import EvidenceInput, ExtractionService
from .learning import ConversationEnvelope, ConversationMessage, LearningService
from .collectors import RSSCollector, WebCollector, WebCrawler
from .review import ReviewQueue
from .reporting import ReportGenerator, DeepReader
from .retention import RetentionService
from .trust import TrustManager, TrustScore, SIGNAL_WEIGHTS
from .store import CanonicalStore, new_id, sha256_text, utc_now
from .evolve import EvolveMemService


TZ = timezone(timedelta(hours=8))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mímir v8.2 governed worker")
    sub = parser.add_subparsers(dest="command", required=True)
    cdc = sub.add_parser("hermes-cdc")
    cdc.add_argument("--state-db", default=os.environ.get("MIMIR_HERMES_STATE_DB", ""))
    cdc.add_argument("--connector-id", default=os.environ.get("MIMIR_CONNECTOR_ID", "hermes-mentor"))
    cdc.add_argument("--owner", default=os.environ.get("MIMIR_CONNECTOR_OWNER", "mentor"))
    cdc.add_argument("--actor", default=os.environ.get("MIMIR_CONNECTOR_ACTOR", "service:hermes_collector"))
    cdc.add_argument("--limit", type=int, default=int(os.environ.get("MIMIR_CDC_LIMIT", "500")))
    retention = sub.add_parser("retention")
    retention.add_argument("--actor", default=os.environ.get("MIMIR_RETENTION_ACTOR", "service:retention_worker"))
    retention.add_argument("--limit", type=int, default=int(os.environ.get("MIMIR_RETENTION_LIMIT", "50")))
    extraction = sub.add_parser("extraction")
    extraction.add_argument("--actor", default=os.environ.get("MIMIR_EXTRACTION_ACTOR", "service:extraction_worker"))
    extraction.add_argument("--limit", type=int, default=int(os.environ.get("MIMIR_EXTRACTION_LIMIT", "20")))
    llm_ext = sub.add_parser("llm-extraction")
    llm_ext.add_argument("--actor", default=os.environ.get("MIMIR_LLM_EXTRACTION_ACTOR", "service:llm_extraction_worker"))
    llm_ext.add_argument("--limit", type=int, default=int(os.environ.get("MIMIR_LLM_EXTRACTION_LIMIT", "10")))
    llm_ext.add_argument("--salience-threshold", type=float, default=float(os.environ.get("MIMIR_LLM_SALIENCE_THRESHOLD", "0.3")))
    review = sub.add_parser("review-reminder")
    review.add_argument("--owner", default=os.environ.get("MIMIR_REVIEW_OWNER", ""))
    review.add_argument("--output", choices=["json", "feishu", "obsidian"], default="json")
    report = sub.add_parser("daily-report")
    report.add_argument("--output", choices=["json", "feishu", "obsidian", "all"], default="json")
    report.add_argument("--obsidian-dir", default=os.environ.get("MIMIR_REPORT_OBSIDIAN_DIR", ""))
    deep = sub.add_parser("deep-reading")
    decay = sub.add_parser("decay-scan")
    decay.add_argument("--actor", default="service:decay_worker")
    trust = sub.add_parser("trust-update")
    trust.add_argument("--dry-run", action="store_true", default=False)
    deep.add_argument("--content", default="")
    deep.add_argument("--source", default="manual")
    deep.add_argument("--title", default="")
    collect = sub.add_parser("collect-all")
    collect.add_argument("--actor", default=os.environ.get("MIMIR_COLLECTOR_ACTOR", "service:collector_worker"))

    # ── v10 new commands ──────────────────────────────────────────────────
    gov = sub.add_parser("governance")
    gov.add_argument("--actor", default=os.environ.get("MIMIR_GOVERNANCE_ACTOR", "service:governance"))
    gov.add_argument("--dry-run", action="store_true", default=False)

    ft = sub.add_parser("fast-track")
    ft.add_argument("--actor", default=os.environ.get("MIMIR_GOVERNANCE_ACTOR", "service:governance"))

    consolidate = sub.add_parser("consolidate")
    consolidate.add_argument("--owner", default="mentor")

    opinion_writes = sub.add_parser("opinion-set")
    opinion_writes.add_argument("--fact-id", required=True)
    opinion_writes.add_argument("--topic", required=True)
    opinion_writes.add_argument("--stance", default="neutral", choices=["support", "oppose", "neutral"])
    opinion_writes.add_argument("--confidence", type=float, default=0.5)
    opinion_writes.add_argument("--owner", default="mentor")
    opinion_writes.add_argument("--evidence-id", default=None)

    # ── v12 new command: EvolveMem ───────────────────────────────────────
    evolve = sub.add_parser("evolve")
    evolve.add_argument("--actor", default=os.environ.get("MIMIR_EVOLVE_ACTOR", "service:evolve"))

    # ── v12 new command: Skill crystallization ──────────────────────────
    crystallize = sub.add_parser("crystallize")
    crystallize.add_argument("--window-days", type=int, default=7)
    crystallize.add_argument("--min-freq", type=int, default=3)
    crystallize.add_argument("--actor", default=os.environ.get("MIMIR_CRYSTALLIZE_ACTOR", "service:crystallize"))

    # ── P0-1 fix: requeue stuck human_review candidates ────────────────
    requeue = sub.add_parser("review-requeue")
    requeue.add_argument("--actor", default=os.environ.get("MIMIR_REQUEUE_ACTOR", "service:maintenance"))
    requeue.add_argument("--dry-run", action="store_true")

    # ── loop closure: scheduled conflict detection ─────────────────────
    conflict = sub.add_parser("conflict-detect")
    conflict.add_argument("--threshold", type=float, default=0.6)
    conflict.add_argument("--actor", default=os.environ.get("MIMIR_CONFLICT_ACTOR", "service:conflict"))

    return parser


def extract_once(store: CanonicalStore, actor_principal: str, *, limit: int = 20) -> dict:
    """Conservatively turn explicit preference/rule utterances into review-only Candidates."""
    if limit < 1 or limit > 200:
        raise ValueError("extraction limit must be between 1 and 200")
    with contextlib.closing(store.connect()) as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT r.run_id,r.source_id,s.owner_principal,m.message_id,m.content_redacted
            FROM ingestion_runs r
            JOIN conversation_sources s ON s.source_id=r.source_id
            JOIN conversation_messages m ON m.message_id=(
                SELECT m2.message_id FROM conversation_messages m2
                WHERE m2.source_id=s.source_id AND m2.role='user'
                  AND (m2.content_redacted LIKE '%我偏好%'
                    OR m2.content_redacted LIKE '%我希望%'
                    OR m2.content_redacted LIKE '%请记住%'
                    OR m2.content_redacted LIKE '%以后请%'
                    OR m2.content_redacted LIKE '%必须%'
                    OR m2.content_redacted LIKE '%不要%')
                ORDER BY m2.ordinal LIMIT 1
            )
            WHERE r.status='stored' AND s.source_category='conversation'
            ORDER BY r.started_at LIMIT ?""",
            (limit,),
        ).fetchall()]
    service = ExtractionService(store)
    created, skipped, failed = [], [], []
    markers = ("我偏好", "我希望", "请记住", "以后请", "必须", "不要")
    seen_runs: set[str] = set()
    for row in rows:
        run_id = row["run_id"]
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        content = str(row["content_redacted"] or "").strip()
        if not content or not any(marker in content for marker in markers):
            skipped.append(run_id)
            continue
        try:
            with contextlib.closing(store.connect()) as connection:
                duplicate = connection.execute(
                    """SELECT candidate_id FROM candidate_facts
                    WHERE proposed_owner_principal=? AND content=?
                      AND status NOT IN ('rejected') LIMIT 1""",
                    (row["owner_principal"], content),
                ).fetchone()
                fact_duplicate = connection.execute(
                    """SELECT fact_id FROM facts
                    WHERE owner_principal=? AND content=? AND status='active' LIMIT 1""",
                    (row["owner_principal"], content),
                ).fetchone()
            if duplicate or fact_duplicate:
                now = utc_now()
                with store.transaction() as connection:
                    connection.execute(
                        """INSERT INTO extraction_runs(
                            extraction_id,run_id,extractor_principal,policy_version,status,
                            candidate_count,started_at,completed_at,error_code
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (new_id(), run_id, actor_principal,
                         "v8.1-explicit-preference-rules-1", "cancelled", 0,
                         now, now, "exact_duplicate"),
                    )
                    connection.execute(
                        "UPDATE ingestion_runs SET status='extracted' WHERE run_id=? AND status='stored'",
                        (run_id,),
                    )
                skipped.append({"run_id": run_id, "reason": "exact_duplicate"})
                continue
            result = service.extract_candidate(
                run_id=run_id,
                source_id=row["source_id"],
                actor_principal=actor_principal,
                content=content,
                summary=content[:200],
                owner_principal=row["owner_principal"],
                domain="personal",
                fact_type="user_pref",
                idempotency_key=f"auto-extract:{run_id}:{sha256_text(content)}",
                evidence=(EvidenceInput(
                    source_id=row["source_id"], message_id=row["message_id"],
                    quote_text=content, start_offset=0, end_offset=len(content),
                ),),
                policy_version="v8.1-explicit-preference-rules-1",
            )
            created.append(result["candidate"]["candidate_id"])
        except Exception as exc:
            failed.append({"run_id": run_id, "error": type(exc).__name__})
    return {"created": created, "skipped": skipped, "failed": failed, "count": len(created)}


def llm_extract_once(store: CanonicalStore, actor_principal: str, *, limit: int = 10, salience_threshold: float = 0.3) -> dict:
    """LLM-powered extraction with higher recall than keyword rules."""
    if limit < 1 or limit > 50:
        raise ValueError("llm extraction limit must be between 1 and 50")
    with contextlib.closing(store.connect()) as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT r.run_id, r.source_id, s.owner_principal, s.memory_mode
            FROM ingestion_runs r
            JOIN conversation_sources s ON s.source_id = r.source_id
            WHERE r.status = 'stored' AND s.source_category = 'conversation'
            ORDER BY r.started_at LIMIT ?""",
            (limit,),
        ).fetchall()]
    if not rows:
        return {"created": [], "skipped": [], "failed": [], "count": 0, "evaluator_mode": "no_data"}

    evaluator = Evaluator()
    service = ExtractionService(store)
    created, skipped, failed = [], [], []
    seen_runs: set[str] = set()

    for row in rows:
        run_id = row["run_id"]
        if run_id in seen_runs:
            continue
        seen_runs.add(run_id)

        with contextlib.closing(store.connect()) as connection:
            messages = [dict(m) for m in connection.execute(
                "SELECT content_redacted FROM conversation_messages WHERE source_id=? AND role='user' ORDER BY ordinal LIMIT 20",
                (row["source_id"],),
            ).fetchall()]

        combined = " ".join(str(m.get("content_redacted", "") or "") for m in messages if m.get("content_redacted"))
        if not combined:
            # mark extracted so empty sessions do not clog the queue forever
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE ingestion_runs SET status='extracted' WHERE run_id=? AND status='stored'",
                    (run_id,),
                )
            skipped.append({"run_id": run_id, "reason": "no_user_content"})
            continue

        try:
            eval_result = evaluator.evaluate(combined)
            decision = evaluator.decide(eval_result)

            if decision.action in ("discard", "reject"):
                now = utc_now()
                with store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO extraction_runs(extraction_id,run_id,extractor_principal,policy_version,status,candidate_count,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                        (new_id(), run_id, actor_principal, eval_result.policy_version, "cancelled", 0, now, now),
                    )
                    connection.execute(
                        "UPDATE ingestion_runs SET status='extracted' WHERE run_id=? AND status='stored'",
                        (run_id,),
                    )
                skipped.append({"run_id": run_id, "reason": decision.action, "salience": eval_result.salience})
                continue

            with contextlib.closing(store.connect()) as connection:
                dup = connection.execute(
                    "SELECT candidate_id FROM candidate_facts WHERE proposed_owner_principal=? AND content=? AND status NOT IN ('rejected') LIMIT 1",
                    (row["owner_principal"], eval_result.summary or combined[:200]),
                ).fetchone()
                fact_dup = connection.execute(
                    "SELECT fact_id FROM facts WHERE owner_principal=? AND content=? AND status='active' LIMIT 1",
                    (row["owner_principal"], eval_result.summary or combined[:200]),
                ).fetchone()
            if dup or fact_dup:
                now = utc_now()
                with store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO extraction_runs(extraction_id,run_id,extractor_principal,policy_version,status,candidate_count,started_at,completed_at,error_code) VALUES(?,?,?,?,?,?,?,?,?)",
                        (new_id(), run_id, actor_principal, eval_result.policy_version, "cancelled", 0, now, now, "exact_duplicate"),
                    )
                    connection.execute(
                        "UPDATE ingestion_runs SET status='extracted' WHERE run_id=? AND status='stored'",
                        (run_id,),
                    )
                skipped.append({"run_id": run_id, "reason": "exact_duplicate"})
                continue

            result = service.extract_candidate(
                run_id=run_id,
                source_id=row["source_id"],
                actor_principal=actor_principal,
                content=eval_result.summary or combined[:200],
                summary=eval_result.summary[:200],
                owner_principal=row["owner_principal"],
                domain=eval_result.domain,
                fact_type=eval_result.fact_type,
                idempotency_key=f"llm-extract:{run_id}:{sha256_text(combined[:500])}",
                policy_version=eval_result.policy_version,
            )
            created.append({"candidate_id": result["candidate"]["candidate_id"], "salience": eval_result.salience, "risk": eval_result.risk})
        except Exception as exc:
            failed.append({"run_id": run_id, "error": type(exc).__name__})
    return {"created": created, "skipped": skipped, "failed": failed, "count": len(created), "evaluator_mode": "llm"}


def review_reminder(store: CanonicalStore, owner: str | None = None) -> dict:
    """Check for pending candidates and return summary for notification."""
    queue = ReviewQueue(store)
    summary = queue.summarize(owner=owner)
    return {
        "total": summary.total,
        "by_risk": summary.by_risk,
        "by_domain": summary.by_domain,
        "oldest_wait_hours": summary.oldest_wait_hours,
        "has_pending": summary.total > 0,
        "parse_error_count": len(summary.parse_errors),
        "parse_errors": summary.parse_errors[:20],
        "items": [
            {
                "candidate_id": i.candidate_id,
                "summary": i.summary,
                "risk": i.risk,
                "domain": i.proposed_domain,
                "wait_hours": i.wait_hours,
            }
            for i in summary.items[:10]
        ],
    }


def daily_report(store: CanonicalStore, output: str = "json", obsidian_dir: str = "") -> dict:
    """Generate daily memory report."""
    gen = ReportGenerator(store)
    report = gen.generate()
    result = {
        "date": report.date,
        "ingestion": report.ingestion,
        "candidates": report.candidates,
        "reviews": report.reviews,
        "governance": report.governance,
        "system": report.system,
    }
    if output in ("feishu", "all"):
        result["feishu_card"] = gen.to_feishu_card(report)
    if output in ("obsidian", "all"):
        md = gen.to_obsidian(report)
        if obsidian_dir:
            path = Path(obsidian_dir) / f"mimir-daily-report-{report.date}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(md, encoding="utf-8")
            result["obsidian_path"] = str(path)
        result["obsidian_markdown"] = md
    return result


def _load_config_feeds(config_path: str | Path | None = None) -> list[dict]:
    """Load RSS feeds from the explicitly configured Mímir config file."""
    config_path = Path(config_path) if config_path else MimirPaths.from_env().config_file
    if not config_path.exists():
        return []
    try:
        import yaml
        with open(config_path) as f:
            config = yaml.safe_load(f)
        return config.get("collector", {}).get("rss_feeds", [])
    except Exception:
        return []


#: Source types the unified ingestion pipeline can dispatch to.
SOURCE_TYPES = ("rss", "web", "vault")


def load_source_registry(config_path: str | Path | None = None) -> list[dict]:
    """Load the unified source registry from mimir_config.yaml.

    Reads ``collector.sources`` (list of {name, type, ...params}). When
    the config file or the section is missing, returns an empty list —
    the caller (collect_all) then falls back to legacy RSS-only
    behavior, so existing deployments keep working unchanged.

    Raises ValueError when a source declares an unknown ``type``: a
    silently-skipped source would look like a working pipeline.
    """
    config_path = Path(config_path) if config_path else MimirPaths.from_env().config_file
    if not config_path.exists():
        return []
    try:
        import yaml

        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        return []
    sources = (config.get("collector", {}) or {}).get("sources", [])
    if not isinstance(sources, list):
        return []
    for source in sources:
        source_type = source.get("type")
        if source_type not in SOURCE_TYPES:
            raise ValueError(
                f"collector source '{source.get('name', '?')}' has unknown type "
                f"'{source_type}' (known: {', '.join(SOURCE_TYPES)})"
            )
    return sources


def _ingest_collect_result(
    learning: LearningService,
    items: list[dict],
    connector_type: str,
    actor_principal: str,
    key_fn,
) -> tuple[int, list[str]]:
    """Feed collected items through the learning pipeline.

    ``key_fn(item)`` builds the per-item idempotency key: RSS results
    carry many items and must dedupe per item (not per feed), web/vault
    carry one payload per result.
    """
    ingested = 0
    errors: list[str] = []
    for item in items:
        content = item.get("content", "")
        if not content:
            continue
        env = ConversationEnvelope(
            connector_type=connector_type,
            connector_id=f"{connector_type}-collector",
            session_id=None,
            owner_principal="mentor",
            memory_mode="observe",
            retention_class="standard",
            messages=(ConversationMessage(role="user", content=content),),
            source_uri=item.get("url", ""),
            title=item.get("title", ""),
            idempotency_key=key_fn(item),
        )
        try:
            learning.ingest_conversation(env, actor_principal)
            ingested += 1
        except Exception as e:
            errors.append(f"ingest:{(item.get('title') or '?')[:30]}:{type(e).__name__}")
    return ingested, errors


def collect_all(
    store: CanonicalStore,
    actor_principal: str,
    *,
    config_path: str | Path | None = None,
    vault_root: Path | None = None,
) -> dict:
    """Run all enabled collectors across every registered source type.

    v12.1.0: dispatches over the config-driven source registry (rss /
    web / vault). With no registry the legacy RSS-only path runs, so
    existing deployments are unaffected until they opt in.
    """
    from .schema import RETENTION_CLASSES, MEMORY_MODES  # noqa: F401  (kept for callers)

    learning = LearningService(store)
    results: dict[str, list] = {"rss": [], "web": [], "vault": [], "errors": []}

    registry = load_source_registry(config_path)

    if not registry:
        # Legacy fallback: one implicit RSS source over all config feeds.
        registry = [{"name": "rss-default", "type": "rss", "feeds": None}]

    for source in registry:
        source_name = source.get("name", "unnamed")
        source_type = source.get("type")
        try:
            if source_type == "rss":
                feeds = source.get("feeds")
                if feeds is None:
                    feeds = _load_config_feeds(config_path)
                collector = RSSCollector(feeds=feeds)
                for r in collector.collect():
                    items = collector.get_items(r)
                    ingested, errors = _ingest_collect_result(
                        learning, items, "rss", actor_principal,
                        key_fn=lambda item: f"rss:{sha256_text(item.get('url') or item.get('title', ''))}",
                    )
                    results["rss"].append({
                        "source": source_name,
                        "title": r.title,
                        "url": r.url,
                        "items": r.items_collected,
                        "ingested": ingested,
                        "errors": errors,
                    })
            elif source_type == "web":
                from .collectors import WebCollector

                url = source.get("url", "")
                if not url:
                    raise ValueError(f"web source '{source_name}' has no url")
                collector = WebCollector()
                r = collector.collect_url(url, category=source.get("category", "knowledge"))
                items = [{"title": r.title, "url": r.url, "content": r.content}]
                ingested, errors = _ingest_collect_result(
                    learning, items, "web", actor_principal,
                    key_fn=lambda item: f"web:{sha256_text(url)}",
                )
                results["web"].append({
                    "source": source_name,
                    "title": r.title,
                    "url": r.url,
                    "items": r.items_collected,
                    "ingested": ingested,
                    "errors": errors,
                })
            elif source_type == "vault":
                from .collectors import VaultCollector

                root = source.get("vault_root") or vault_root
                collector = VaultCollector(vault_root=root)
                for r in collector.collect():
                    key = collector.idempotency_key(r.source_id)
                    items = [{"title": r.title, "url": "", "content": r.content}]
                    ingested, errors = _ingest_collect_result(
                        learning, items, "vault", actor_principal,
                        key_fn=lambda item, k=key: k,
                    )
                    results["vault"].append({
                        "source": source_name,
                        "title": r.title,
                        "path": r.source_id,
                        "items": r.items_collected,
                        "ingested": ingested,
                        "errors": errors,
                    })
        except Exception as e:
            results["errors"].append(f"{source_name}: {type(e).__name__}: {e}")

    return results


def decay_scan(store: CanonicalStore, actor_principal: str = "service:decay_worker") -> dict:
    """Scan active facts and mark decayed ones based on half-life and valid_to."""
    from .schema import DECAY_TIER_MAP, DECAY_HALF_LIFE
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    marked = 0
    skipped = 0
    errors = []
    with contextlib.closing(store.connect()) as connection:
        rows = connection.execute(
            "SELECT fact_id, fact_type, decay_tier, valid_to, updated_at, content_hash FROM facts WHERE status='active'"
        ).fetchall()
    for row in rows:
        try:
            fact_id = row["fact_id"]
            fact_type = row["fact_type"]
            decay_tier = row["decay_tier"] or DECAY_TIER_MAP.get(fact_type, "L4_temporary")
            decay_half_life = DECAY_HALF_LIFE.get(decay_tier, 30)
            if decay_half_life is None:
                skipped += 1
                continue
            updated_str = row["updated_at"]
            if not updated_str:
                skipped += 1
                continue
            try:
                updated = datetime.fromisoformat(updated_str)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except Exception:
                skipped += 1
                continue
            elapsed_days = (now - updated).days
            if elapsed_days < decay_half_life * 3:
                skipped += 1
                continue
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE facts SET decayed_at=?, status='archived' WHERE fact_id=? AND status='active'",
                    (now.isoformat(), fact_id),
                )
                from .store import new_id, sha256_text, canonical_json
                # payload_hash must be computed over the exact bytes stored in
                # payload_json. Build the payload once via canonical_json (as the
                # rest of the event pipeline does) and hash that same string, so
                # integrity re-verification of fact.decayed events succeeds.
                payload_json = canonical_json({"decay_tier": decay_tier, "elapsed_days": elapsed_days})
                connection.execute(
                    "INSERT INTO memory_events(event_id,aggregate_type,aggregate_id,aggregate_version,event_type,actor_principal,occurred_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                    (new_id(), "fact", fact_id, row["current_version"] if "current_version" in row else 1, "fact.decayed", actor_principal, now.isoformat(), payload_json, sha256_text(payload_json)),
                )
            marked += 1
        except Exception as e:
            errors.append({"fact_id": row.get("fact_id", "?"), "error": type(e).__name__})
    return {"marked": marked, "skipped": skipped, "errors": errors, "total_processed": marked + skipped + len(errors)}


def review_requeue(store: CanonicalStore, actor_principal: str, *, dry_run: bool = False, only_unassessed: bool = False) -> dict:
    """P0-1 fix: requeue stuck human_review candidates back to review_required.

    governance routes candidates into human_review, but no consumer accepts that
    status (review_candidate / ReviewQueue / run_governance_once all only read
    review_required), so they deadlock. This puts them back into the pipeline so
    governance re-evaluates: noise/duplicates get auto-rejected, low-risk value
    goes provisional -> fast_track, only genuinely ambiguous ones return to
    human_review (now consumable via the review endpoint).

    Every change writes a candidate.requeued memory event (event-sourcing rule:
    never silently UPDATE history).
    """
    sql = "SELECT candidate_id FROM candidate_facts WHERE status='human_review'"
    if only_unassessed:
        # P0-2 loop closure: only retry candidates that never got a successful
        # governance assessment (e.g. LLM unreachable under the old AF_UNIX-only
        # sandbox). Candidates with a successful assessment are genuinely waiting
        # for a human and must not bounce.
        sql += (" AND NOT EXISTS (SELECT 1 FROM candidate_review_assessments a"
                " WHERE a.candidate_id=candidate_facts.candidate_id AND a.success=1)")
    sql += " ORDER BY created_at"
    with contextlib.closing(store.connect()) as connection:
        rows = [dict(r) for r in connection.execute(sql).fetchall()]
    if dry_run:
        return {"command": "review-requeue", "dry_run": True, "requeued": 0, "pending": len(rows)}
    requeued = 0
    for row in rows:
        candidate_id = row["candidate_id"]
        now = utc_now()
        event_id = new_id()
        payload = {
            "candidate_id": candidate_id,
            "from_status": "human_review",
            "to_status": "review_required",
            "reason": "P0-1 state-machine gap: human_review had no consumer; requeue for governance re-evaluation",
        }
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with store.transaction() as connection:
            current = connection.execute(
                "SELECT status FROM candidate_facts WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not current or current["status"] != "human_review":
                continue  # status changed between scan and update; skip
            connection.execute(
                """INSERT INTO memory_events(
                    event_id, aggregate_type, aggregate_id, aggregate_version,
                    event_type, actor_principal, request_id, correlation_id,
                    occurred_at, payload_json, payload_hash, idempotency_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, "candidate", candidate_id, 2, "candidate.requeued", actor_principal,
                 event_id, event_id, now, payload_json, sha256_text(payload_json),
                 f"requeue-{event_id}"),
            )
            connection.execute(
                "UPDATE candidate_facts SET status='review_required', updated_at=? WHERE candidate_id=?",
                (now, candidate_id),
            )
        requeued += 1
    return {"command": "review-requeue", "dry_run": False, "requeued": requeued, "pending": len(rows)}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    production = os.environ.get("MIMIR_ENV", "").strip().lower() == "production"
    paths = MimirPaths.from_env(production=production)
    store = CanonicalStore(paths.data_dir / "canonical.db")
    if args.command == "hermes-cdc":
        state_db = args.state_db or (
            str(paths.connector_hermes_state_db) if paths.connector_hermes_state_db else ""
        )
        if not state_db:
            raise SystemExit(
                "MIMIR_HERMES_STATE_DB, MIMIR_CONNECTOR_HERMES_STATE_DB, or --state-db is required"
            )
        result = HermesStateCDC(
            store, LearningService(store), state_db,
            connector_id=args.connector_id, owner_principal=args.owner,
        ).collect_once(actor_principal=args.actor, limit=args.limit)
    elif args.command == "retention":
        result = RetentionService(store).execute_due(args.actor, limit=args.limit)
    elif args.command == "extraction":
        result = extract_once(store, args.actor, limit=args.limit)
    elif args.command == "llm-extraction":
        result = llm_extract_once(store, args.actor, limit=args.limit, salience_threshold=args.salience_threshold)
    elif args.command == "review-reminder":
        result = review_reminder(store, owner=args.owner)
    elif args.command == "daily-report":
        result = daily_report(store, output=args.output, obsidian_dir=args.obsidian_dir)
    elif args.command == "decay-scan":
        result = decay_scan(store, actor_principal=args.actor)
    elif args.command == "trust-update":
        manager = TrustManager(store)
        result = manager.update_from_signals(dry_run=args.dry_run)
    elif args.command == "deep-reading":
        reader = DeepReader(store)
        result = reader.read(args.content, source=args.source, title=args.title)
    elif args.command == "collect-all":
        result = collect_all(store, args.actor)
    elif args.command == "governance":
        from .candidates import CandidateService
        svc = CandidateService(store)
        # P0-2 loop closure: retry unassessed human_review candidates, evaluate
        # the review_required queue, then auto-commit low-risk provisional ones.
        requeue_result = review_requeue(
            store, actor_principal=args.actor, dry_run=args.dry_run, only_unassessed=True
        )
        result = run_governance_once(store, svc, dry_run=args.dry_run, actor=args.actor)
        result["requeued_unassessed"] = requeue_result.get("requeued", 0)
        if not args.dry_run:
            ft = fast_track_commit_all(store, svc, actor=args.actor)
            result["fast_track_committed"] = ft.get("committed", 0)
            if ft.get("errors"):
                result["fast_track_errors"] = ft["errors"]
    elif args.command == "conflict-detect":
        from .conflict import ConflictService
        result = ConflictService(store).detect(threshold=args.threshold, actor_principal=args.actor)
    elif args.command == "fast-track":
        from .candidates import CandidateService
        svc = CandidateService(store)
        result = fast_track_commit_all(store, svc, actor=args.actor)
    elif args.command == "consolidate":
        from .opinion import OpinionService
        result = OpinionService(store).consolidate_observations()
    elif args.command == "opinion-set":
        from .opinion import OpinionService
        result = OpinionService(store).set_opinion(
            fact_id=args.fact_id, topic=args.topic, stance=args.stance,
            confidence=args.confidence, owner_principal=args.owner,
        )
    elif args.command == "evolve":
        result = EvolveMemService(store).evolve(actor_principal=args.actor)
    elif args.command == "crystallize":
        from .crystallize import CrystalService
        result = CrystalService(store).scan(
            window_days=args.window_days, min_freq=args.min_freq,
            actor_principal=args.actor,
        )
    elif args.command == "review-requeue":
        result = review_requeue(store, args.actor, dry_run=args.dry_run)
    else:
        result = extract_once(store, args.actor, limit=20)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


