# -*- coding: utf-8 -*-
"""MEX v1 — Mímir 记忆交换格式（1.1.0，Roadmap 差距#2）。

跨系统导入导出的开放信封。借 Cognee 四家适配器格局反向受益：任何
外部生态只要吐出 MEX v1 就能进 Mímir，Mímir 也能被任何支持 JSON 的
系统消费。

信封（json，UTF-8）：
    {"mex_version": 1,
     "source_node": str,        # 导出节点标识（审计用）
     "exported_at": iso8601,
     "integrity": {section: sha256(canonical_json(section))},
     "counts": {facts, fact_versions, evidence, candidates, sources},
     "facts": [全列 dict…],
     "fact_versions": [{fact_id, version, snapshot_json, change_type,
                        change_reason, actor_principal, source_event_id,
                        created_at, content_hash, previous_version_hash}],
     "evidence": [candidate_evidence 中挂在入包 fact 上的行（经
                  candidate_facts.committed_fact_id 二跳取，带 fact_id 别名列）],
     "candidates": [被 evidence 引用的 candidate_facts 行（导入需先建，
                    证据外键才指得到）],
     "sources": [被 evidence 引用的 conversation_sources 行…],
     "legacy_sources": [candidate_facts.source_id 引用的旧 sources 行…]}

**v1 刻意不含 memory_events**：事件账本是节点私有真相源，跨节点事件属
联邦协议（CRDT+LWW 已管），塞进交换格式会暗示「事件可导入」并污染目标
库的 verify_canonical 重算——宁缺勿脏用在格式层。

导入语义——**宁缺勿脏**（脏数据比缺数据贵得多：PK 撞上会永久挤掉正确行）：
* 同 fact_id + 同 content_hash → skip（幂等重放，安全再来一遍）
* 同 fact_id + 异 content_hash → skip 并记入 conflicts（本地优先，绝不覆盖）
* 新 fact_id → 原样插 facts + fact_versions + evidence（保 fact_id/version
  身份与谱系链，导入后本地可继续验证/追加）
* 任一段 integrity 校验失败 → 整包拒绝（ValidationError）——半包入库
  比拒收危险得多。

COGX 适配器（cogx_to_mex）：外部最小样本 → MEX。映射表：
    type MemoryFact→fact；category: user_preference→user_pref/personal,
    project_config→project_config/project（未知 category→event/system，
    原文入 summary 标注来源）；原节点 id → legacy_id（审计回溯）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .schema import SCHEMA_VERSION
from .store import CanonicalStore, canonical_json

MEX_VERSION = 1
_DATA_SECTIONS = ("facts", "fact_versions", "evidence", "candidates",
                  "sources", "legacy_sources")

#: COGX category → (fact_type, domain)；未列出者走兜底 (event, system)。
#: category → (fact_type, domain)；domain 必须落在 schema 注册集
#: （infrastructure/knowledge/personal/quant/system/tech_support）内。
_COGX_CATEGORY_MAP = {
    "user_preference": ("user_pref", "personal"),
    "project_config": ("project_config", "knowledge"),
    "event": ("event", "system"),
}


class ValidationError(ValueError):
    """信封非法：版本不认识 / 完整性对不上 / 结构缺失。"""


def _sha(section: Any) -> str:
    return hashlib.sha256(canonical_json(section).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── export ────────────────────────────────────────────────────────────────

def _fact_columns(store: CanonicalStore) -> list[str]:
    with store.connect() as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(facts)")]


def mex_export(store: CanonicalStore, *, node_id: str,
               since: str | None = None, external: bool = False) -> dict:
    """导出 facts（updated_at ≥ since）+ 其版本史 + 其证据 + 被引来源。

    ``external=True``＝外发模式：只导 egress_policy='external_allowed' 的
    事实（redacted_external 在 v1 也排除——MEX 侧脱敏管道未接线，保守
    拒发比漏发便宜）。默认 False＝本机管理员离线导出，全量。
    """
    columns = _fact_columns(store)
    with store.connect() as connection:
        where, params = [], []
        if since:
            where.append("updated_at >= ?")
            params.append(since)
        if external:
            where.append("egress_policy = 'external_allowed'")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        fact_rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM facts{clause} ORDER BY fact_id",
            params).fetchall()
        fact_ids = [row["fact_id"] for row in fact_rows]
        id_set = set(fact_ids)
        versions: list[dict] = []
        evidence: list[dict] = []
        sources: list[dict] = []
        for fact_id in fact_ids:
            versions.extend(dict(row) for row in connection.execute(
                "SELECT fact_id, version, snapshot_json, change_type, "
                "change_reason, actor_principal, source_event_id, created_at, "
                "content_hash, previous_version_hash FROM fact_versions "
                "WHERE fact_id=? ORDER BY version", (fact_id,)))
            evidence.extend(dict(row) for row in connection.execute(
                "SELECT e.evidence_id, e.candidate_id, e.source_id, "
                "e.message_id, e.quote_text_redacted, e.start_offset, "
                "e.end_offset, e.evidence_hash, e.added_by, e.created_at, "
                "cf.committed_fact_id AS fact_id "
                "FROM candidate_evidence e "
                "JOIN candidate_facts cf ON cf.candidate_id = e.candidate_id "
                "WHERE cf.committed_fact_id=?", (fact_id,)))
        source_ids = sorted({row.get("source_id") for row in evidence
                             if row.get("source_id")})
        for source_id in source_ids:
            row = connection.execute(
                "SELECT * FROM conversation_sources WHERE source_id=?",
                (source_id,)).fetchone()
            if row:
                sources.append(dict(row))
        # 证据依赖闭包：candidate 行 + 旧 sources 行（candidate_facts 的
        # source_id 引用旧表，两腿来源都在包内才算完整——半包导入必 FK 炸）
        candidate_ids = sorted({row["candidate_id"] for row in evidence})
        candidates: list[dict] = []
        for candidate_id in candidate_ids:
            row = connection.execute(
                "SELECT * FROM candidate_facts WHERE candidate_id=?",
                (candidate_id,)).fetchone()
            if row:
                candidates.append(dict(row))
        legacy_ids = sorted({cand.get("source_id") for cand in candidates
                             if cand.get("source_id")})
        legacy_sources: list[dict] = []
        for legacy_id in legacy_ids:
            row = connection.execute(
                "SELECT * FROM sources WHERE source_id=?", (legacy_id,)).fetchone()
            if row:
                legacy_sources.append(dict(row))
    facts_payload = [dict(row) for row in fact_rows]
    envelope: dict[str, Any] = {
        "mex_version": MEX_VERSION,
        "source_node": node_id,
        "exported_at": _utc_now(),
        "integrity": {},
        "counts": {"facts": len(facts_payload),
                   "fact_versions": len(versions),
                   "evidence": len(evidence),
                   "candidates": len(candidates),
                   "sources": len(sources),
                   "legacy_sources": len(legacy_sources)},
        "facts": facts_payload,
        "fact_versions": versions,
        "evidence": evidence,
        "candidates": candidates,
        "sources": sources,
        "legacy_sources": legacy_sources,
    }
    envelope["integrity"] = {s: _sha(envelope[s]) for s in _DATA_SECTIONS}
    return envelope


# ── import ────────────────────────────────────────────────────────────────

def validate_envelope(payload: dict) -> None:
    """版本 + 结构 + 段完整性三重校验；不过即 ValidationError。"""
    if not isinstance(payload, dict):
        raise ValidationError("MEX 信封必须是对象")
    if payload.get("mex_version") != MEX_VERSION:
        raise ValidationError(
            f"不认识的 mex_version: {payload.get('mex_version')!r}")
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict):
        raise ValidationError("信封缺 integrity 段")
    for section in _DATA_SECTIONS:
        if section not in payload:
            raise ValidationError(f"信封缺数据段: {section}")
        expected = integrity.get(section)
        if expected != _sha(payload[section]):
            raise ValidationError(
                f"段完整性校验失败: {section}——传输中被篡改或截断")


def _existing(connection: sqlite3.Connection, fact_id: str) -> dict | None:
    row = connection.execute(
        "SELECT fact_id, content_hash FROM facts WHERE fact_id=?",
        (fact_id,)).fetchone()
    return dict(row) if row else None


def mex_import(store: CanonicalStore, payload: dict, *,
               actor_principal: str, dry_run: bool = False) -> dict:
    """导入 MEX 信封；三分法 skip/conflict/import（见模块 docstring）。

    返回报告 {imported, skipped, conflicts: [{fact_id, reason}],
    would_import}（dry_run 时计数在 would_import，不落库）。
    """
    validate_envelope(payload)
    facts = payload["facts"]
    versions = payload["fact_versions"]
    evidence = payload["evidence"]
    candidates = payload["candidates"]
    sources = payload["sources"]
    legacy_sources = payload["legacy_sources"]
    columns = _fact_columns(store)
    with store.connect() as connection:
        # 本地态快照必须在**任何落库之前**取：否则阶段一 OR-IGNORE 进来的
        # 新 fact 会被阶段二误判成「本地已有」而 skip 掉（时序耦合，实锤过）。
        local = {row["fact_id"]: row["content_hash"]
                 for row in connection.execute(
                     "SELECT fact_id, content_hash FROM facts")}
        # ── 三分法分类（先于任何写；dry_run 据此直接算报告，零落库）。
        imported_ids: list[str] = []
        skipped = 0
        conflicts: list[dict] = []
        for fact in facts:
            fact_id = fact["fact_id"]
            if fact_id in local:
                if local[fact_id] == fact["content_hash"]:
                    skipped += 1
                else:
                    conflicts.append({
                        "fact_id": fact_id,
                        "reason": "content_hash 不符——本地优先，不覆盖"})
            else:
                imported_ids.append(fact_id)
        if dry_run:
            return {"imported": 0, "skipped": skipped,
                    "conflicts": conflicts, "would_import": len(imported_ids)}
        to_import = set(imported_ids)

        # ── 阶段一：依赖闭包按**外键序**落库（半包/乱序必 FK 炸）。
        # 正确序（由表引用实测定，别想当然）：
        #   sources / conversation_sources（无内部依赖）
        #   → facts（candidate 与 evidence 都最终引用它）
        #   → candidate_facts（committed_fact_id→facts, source_id→sources）
        #   → candidate_evidence（candidate_id→candidate_facts,
        #                         source_id→conversation_sources）
        # 支撑表全用 INSERT OR IGNORE：PK 撞上=本地已有，不动它。
        for table, rows in (("sources", legacy_sources),
                            ("conversation_sources", sources)):
            for row in rows:
                connection.execute(
                    f"INSERT OR IGNORE INTO {table} "
                    f"({', '.join(row.keys())}) "
                    f"VALUES ({', '.join('?' * len(row))})",
                    tuple(row.values()))
        # 新 fact 落库（skip/conflict 的不动）：candidate/evidence 的 FK 靶子。
        fact_columns = ", ".join(columns)
        fact_marks = ", ".join("?" * len(columns))
        for fact in facts:
            if fact["fact_id"] not in to_import:
                continue
            present = {col: fact.get(col) for col in columns}
            if "schema_version" in present:
                # schema_version 以目标库运行时为准（源/COGX 不代表这里）
                present["schema_version"] = SCHEMA_VERSION
            connection.execute(
                f"INSERT INTO facts ({fact_columns}) VALUES ({fact_marks})",
                tuple(present[col] for col in columns))
        # candidates：只导引用了本次新 fact 的那批（committed_fact_id 必须在
        # 包内且已在库；OR IGNORE 兜住 source/supersedes 越界 FK）
        for cand in candidates:
            if cand.get("committed_fact_id") not in to_import:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO candidate_facts "
                f"({', '.join(cand.keys())}) "
                f"VALUES ({', '.join('?' * len(cand))})",
                tuple(cand.values()))

        # ── 阶段二：为新 fact 落版本史 + 证据。
        by_fact: dict[str, list[dict]] = {}
        for version in versions:
            by_fact.setdefault(version["fact_id"], []).append(version)
        evid_by_fact: dict[str, list[dict]] = {}
        for item in evidence:
            evid_by_fact.setdefault(item["fact_id"], []).append(item)
        for fact_id in imported_ids:
            for version in sorted(by_fact.get(fact_id, []),
                                  key=lambda v: v["version"]):
                connection.execute(
                    "INSERT INTO fact_versions (fact_id, version, "
                    "snapshot_json, change_type, change_reason, "
                    "actor_principal, source_event_id, created_at, "
                    "content_hash, previous_version_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (version["fact_id"], version["version"],
                     version["snapshot_json"], version["change_type"],
                     version["change_reason"], version["actor_principal"],
                     version["source_event_id"], version["created_at"],
                     version["content_hash"],
                     version.get("previous_version_hash")))
            for item in evid_by_fact.get(fact_id, []):
                cols = [c for c in (
                    "evidence_id", "candidate_id", "source_id", "message_id",
                    "quote_text_redacted", "start_offset", "end_offset",
                    "evidence_hash", "added_by", "created_at")
                    if c in item]
                connection.execute(
                    "INSERT OR IGNORE INTO candidate_evidence "
                    f"({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    tuple(item[c] for c in cols))
        connection.commit()
    return {"imported": len(imported_ids), "skipped": skipped,
            "conflicts": conflicts, "would_import": None}


# ── COGX 适配器 ───────────────────────────────────────────────────────────

def cogx_to_mex(cogx: dict, *, source_node: str) -> dict:
    """COGX 节点图 → MEX v1（只取 MemoryFact；其余节点 v1 不映射）。"""
    facts = []
    for node in cogx.get("nodes", []):
        if node.get("type") != "MemoryFact":
            continue
        props = node.get("properties", {})
        content = (props.get("text") or "").strip()
        if not content:
            continue
        fact_type, domain = _COGX_CATEGORY_MAP.get(
            props.get("category", ""), ("event", "system"))
        facts.append({
            "fact_id": f"mex-cogx-{node['id']}",
            "legacy_id": node["id"],
            "current_version": 1,
            "status": "active",
            "content": content,
            "summary": content[:200],
            "domain": domain,
            "fact_type": fact_type,
            "owner_principal": "mex-import",
            "project_id": None,
            "visibility": "all",
            "sensitivity": "internal",
            "egress_policy": "local_only",
            "human_status": "unreviewed",
            "confidence_score": None,
            "valid_from": None,
            "valid_to": None,
            "recorded_at": props.get("created_at") or _utc_now(),
            "updated_at": props.get("created_at") or _utc_now(),
            "last_verified_at": None,
            "tombstoned_at": None,
            "decay_tier": "L3_event",
            "decayed_at": None,
            "schema_version": 0,  # import 前由导入端盖当前运行时版本
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        })
    # 版本史合成：每 fact 一条 genesis（快照=facts 行的投影）
    versions = [{
        "fact_id": fact["fact_id"], "version": 1,
        "snapshot_json": canonical_json(
            {k: v for k, v in fact.items() if k != "schema_version"}),
        "change_type": "fact.created",
        "change_reason": "COGX 导入合成 genesis",
        "actor_principal": "mex-import",
        "source_event_id": f"cogx-{fact['legacy_id']}",
        "created_at": fact["recorded_at"],
        "content_hash": fact["content_hash"],
        "previous_version_hash": "",
    } for fact in facts]
    envelope: dict[str, Any] = {
        "mex_version": MEX_VERSION,
        "source_node": source_node,
        "exported_at": _utc_now(),
        "integrity": {},
        "counts": {"facts": len(facts), "fact_versions": len(versions),
                   "evidence": 0, "candidates": 0,
                   "sources": 0, "legacy_sources": 0},
        "facts": facts, "fact_versions": versions,
        "evidence": [], "candidates": [],
        "sources": [], "legacy_sources": [],
    }
    envelope["integrity"] = {s: _sha(envelope[s]) for s in _DATA_SECTIONS}
    return envelope