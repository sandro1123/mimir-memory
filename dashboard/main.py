"""Mímir Dashboard — Backend API 聚合层"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── 配置 ──────────────────────────────────────────────
MIMIR_API = os.environ.get("MIMIR_API", "http://127.0.0.1:8456")
CANONICAL_DB = Path(os.environ.get(
    "CANONICAL_DB",
    str(Path.home() / ".hermes/mimir/v9/production-v9.0-20260805_214614/canonical.db"),
))
ADMIN_TOKEN_FILE = Path(os.environ.get(
    "ADMIN_TOKEN_FILE",
    str(Path.home() / ".hermes/mimir/secrets/clients/admin.token"),
))
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", str(Path(__file__).parent.parent / "frontend")))
CACHE_TTL = 30  # seconds
STATIC_DIR = FRONTEND_DIR

# ── 监控水位阈值 (P0-2) ──────────────────────────────
# 投影器 checkpoint 落后 event head 的事件数水位。
# 注意：conversation/candidate/opinion/observation 类事件本就不投影，
# 少量落后属正常；阈值放宽以避免误报。
PROJECTOR_LAG_WARN = int(os.environ.get("PROJECTOR_LAG_WARN", "100"))
PROJECTOR_LAG_CRIT = int(os.environ.get("PROJECTOR_LAG_CRIT", "500"))
# 治理积压水位：human_review 候选数超过即告警。
HUMAN_REVIEW_WARN = int(os.environ.get("HUMAN_REVIEW_WARN", "50"))
# outbox 待投影积压水位。
PENDING_OUTBOX_WARN = int(os.environ.get("PENDING_OUTBOX_WARN", "100"))
# ──────────────────────────────────────────────────────

app = FastAPI(title="Mímir Dashboard", version="3.0.0")

# ── Auth middleware ────────────────────────────────────
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN") or ""
if not DASHBOARD_TOKEN:
    token_path = ADMIN_TOKEN_FILE
    if token_path.exists():
        DASHBOARD_TOKEN = token_path.read_text().strip()

import hashlib
import hmac

DASHBOARD_PASSWORD_FILE = Path(os.environ.get(
    "DASHBOARD_PASSWORD_FILE",
    str(Path.home() / ".hermes/mimir/secrets/dashboard_password.sha256"),
))


def _stored_password_hash() -> str:
    env = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
    if env:
        return env
    try:
        if DASHBOARD_PASSWORD_FILE.exists():
            return DASHBOARD_PASSWORD_FILE.read_text().strip()
    except Exception:
        pass
    return ""


def _session_secret() -> bytes:
    return hashlib.sha256(_stored_password_hash().encode() + b"mimir-dashboard-session-v1").digest()


SESSION_TTL = 7 * 24 * 3600


def _make_session_token() -> str:
    expiry = int(time.time()) + SESSION_TTL
    sig = hmac.new(_session_secret(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def _valid_session_token(token: str) -> bool:
    try:
        expiry_s, sig = token.split(".", 1)
        expiry = int(expiry_s)
    except Exception:
        return False
    if expiry < time.time():
        return False
    expect = hmac.new(_session_secret(), expiry_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)


def _request_authorized(request) -> bool:
    cookie = request.cookies.get("mimir_session", "")
    if cookie and _valid_session_token(cookie):
        return True
    bearer = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    return bool(bearer) and bool(DASHBOARD_TOKEN) and hmac.compare_digest(bearer, DASHBOARD_TOKEN)


_login_attempts: dict[str, tuple[int, float]] = {}


@app.post("/api/auth/login")
async def api_auth_login(request: Request, body: dict):
    """Password login -> signed HttpOnly session cookie."""
    ip = request.client.host if request.client else "?"
    cnt, window_start = _login_attempts.get(ip, (0, time.time()))
    if time.time() - window_start > 300:
        cnt, window_start = 0, time.time()
    if cnt >= 10:
        raise HTTPException(429, "too many attempts, try later")
    _login_attempts[ip] = (cnt + 1, window_start)

    password = str((body or {}).get("password", ""))
    stored = _stored_password_hash()
    ok = bool(stored) and bool(password) and hmac.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), stored)
    if not ok:
        raise HTTPException(401, "\u5bc6\u7801\u9519\u8bef")
    _login_attempts.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("mimir_session", _make_session_token(),
                    max_age=SESSION_TTL, httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/auth/logout")
async def api_auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("mimir_session", path="/")
    return resp


@app.get("/api/auth/check")
async def api_auth_check(request: Request):
    return {"ok": _request_authorized(request)}


@app.middleware("http")
async def auth_middleware(request, call_next):
    path = request.url.path
    if path in ("/", "/health", "/api/auth/login", "/api/auth/check"):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/v1"):
        if not _request_authorized(request):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

# 缓存
_cache: dict[str, tuple[float, Any]] = {}


import functools


def _cache_key(key: str, kwargs: dict) -> str:
    """按查询参数生成缓存键，避免不同过滤器/分页互相污染"""
    if not kwargs:
        return key
    qs = ",".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return f"{key}:{qs}"


def _invalidate(*prefixes: str):
    """清除含前缀的全部缓存条目（含带参子键）"""
    for p in prefixes:
        for k in list(_cache.keys()):
            if k == p or k.startswith(p + ":"):
                _cache.pop(k, None)


def _cached(key: str, ttl: int = CACHE_TTL):
    """缓存装饰器"""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            ck = _cache_key(key, kwargs)
            now = time.time()
            if ck in _cache and now - _cache[ck][0] < ttl:
                return _cache[ck][1]
            result = await func(*args, **kwargs)
            _cache[ck] = (now, result)
            return result
        return wrapper
    return decorator


def _get_admin_token() -> str | None:
    if ADMIN_TOKEN_FILE.exists():
        return ADMIN_TOKEN_FILE.read_text().strip()
    return None


async def _mimir_get(path: str) -> dict | None:
    """请求 Mímir API"""
    token = _get_admin_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{MIMIR_API}{path}", headers=headers)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


async def _mimir_post(path: str, data: dict) -> dict | None:
    """POST 请求 Mímir API"""
    token = _get_admin_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{MIMIR_API}{path}", headers=headers, json=data)
            if resp.status_code in (200, 201):
                return resp.json()
    except Exception:
        pass
    return None


# ── 工具函数 ──────────────────────────────────────────

def _db_query(query: str, params: tuple = ()) -> list[dict]:
    """只读 SQLite 查询"""
    if not CANONICAL_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(CANONICAL_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _db_query_one(query: str, params: tuple = ()) -> dict | None:
    rows = _db_query(query, params)
    return rows[0] if rows else None


# ── API 端点 ──────────────────────────────────────────

def _compute_alerts(overview: dict) -> list[dict]:
    """从 overview 数据推导监控水位告警 (P0-2)。

    返回 [{level, code, message, value, threshold}]，level ∈ warn/crit。
    """
    alerts: list[dict] = []

    # 服务本身不可达 → 最高级别告警
    if overview.get("status") != "ok":
        alerts.append({
            "level": "crit", "code": "service_down",
            "message": f"Mímir API 状态异常: {overview.get('status')}",
            "value": overview.get("status"), "threshold": "ok",
        })

    # 投影器 checkpoint 落后水位
    for p in overview.get("projectors", []):
        lag = p.get("lag", 0)
        if lag >= PROJECTOR_LAG_CRIT:
            alerts.append({
                "level": "crit", "code": "projector_lag",
                "message": f"投影器 {p.get('name')} 落后 {lag} 个事件 (≥{PROJECTOR_LAG_CRIT})",
                "value": lag, "threshold": PROJECTOR_LAG_CRIT,
            })
        elif lag >= PROJECTOR_LAG_WARN:
            alerts.append({
                "level": "warn", "code": "projector_lag",
                "message": f"投影器 {p.get('name')} 落后 {lag} 个事件 (≥{PROJECTOR_LAG_WARN})",
                "value": lag, "threshold": PROJECTOR_LAG_WARN,
            })
        if p.get("dead_letter"):
            alerts.append({
                "level": "crit", "code": "projector_dead_letter",
                "message": f"投影器 {p.get('name')} 存在死信",
                "value": p.get("dead_letter"), "threshold": 0,
            })

    # outbox 待投影积压
    pending = overview.get("pending", 0)
    if pending >= PENDING_OUTBOX_WARN:
        alerts.append({
            "level": "warn", "code": "outbox_pending",
            "message": f"outbox 待投影 {pending} 条 (≥{PENDING_OUTBOX_WARN})",
            "value": pending, "threshold": PENDING_OUTBOX_WARN,
        })

    # 死信队列非空
    dead_letters = overview.get("dead_letters", 0)
    if dead_letters:
        alerts.append({
            "level": "warn", "code": "dead_letters",
            "message": f"死信队列 {dead_letters} 条",
            "value": dead_letters, "threshold": 0,
        })

    # 治理积压：human_review 候选数
    governance = overview.get("governance", {}) or {}
    human_review = governance.get("human_review", 0)
    if human_review >= HUMAN_REVIEW_WARN:
        alerts.append({
            "level": "warn", "code": "human_review_backlog",
            "message": f"human_review 积压 {human_review} 条 (≥{HUMAN_REVIEW_WARN})",
            "value": human_review, "threshold": HUMAN_REVIEW_WARN,
        })

    return alerts


@app.get("/api/overview")
@_cached("overview")
async def api_overview():
    """总览页数据"""
    health = await _mimir_get("/health")
    ready = await _mimir_get("/ready")
    learning = await _mimir_get("/v8/learning/status")
    knowledge = await _mimir_get("/v9/knowledge/status")

    # 数据库统计
    facts = _db_query("SELECT domain, fact_type, owner_principal, COUNT(*) as cnt FROM facts WHERE status='active' GROUP BY domain")
    total_facts = sum(r["cnt"] for r in facts)
    
    event_head = 0
    pending = 0
    dead_letters = 0
    principals = 0
    projectors = []
    if ready:
        event_head = ready.get("event_head", 0)
        pending = ready.get("pending", 0)
        dead_letters = ready.get("dead_letters", 0)
        principals = ready.get("principals", 0)
        projectors = ready.get("projectors", [])
    
    # 学习引擎
    candidates = {}
    ingestion = {}
    extraction = {}
    if learning:
        candidates = learning.get("candidates", {})
        ingestion = learning.get("ingestion_runs", {})
        extraction = learning.get("extraction_runs", {})

    # 知识层
    knowledge_items = {}
    enabled_layers = []
    if knowledge:
        knowledge_items = knowledge.get("knowledge_items", {})
        enabled_layers = knowledge.get("enabled_layers", [])

    # 系统资源
    mem_info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    mem_info["total"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                elif "MemAvailable" in line:
                    mem_info["available"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                elif "MemFree" in line:
                    mem_info["free"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                if len(mem_info) >= 3:
                    break
    except Exception:
        pass

    # 磁盘
    db_size = "0"
    try:
        db_size = str(round(CANONICAL_DB.stat().st_size / 1024 / 1024, 1))
    except Exception:
        pass

    version = health.get("version", "?") if health else "?"
    schema_version = health.get("schema_version", "?") if health else "?"
    service_status = health.get("status", "down") if health else "down"

    overview = {
        "status": service_status,
        "version": version,
        "schema_version": schema_version,
        "total_facts": total_facts,
        "event_head": event_head,
        "pending": pending,
        "dead_letters": dead_letters,
        "principals": principals,
        "projectors": [
            {
                "name": p["projector_name"],
                "checkpoint": p["checkpoint_event_seq"],
                "status": p["status"],
                "lag": event_head - p["checkpoint_event_seq"],
                "dead_letter": p["dead_letter"],
            }
            for p in projectors
        ],
        "candidates": {
            "review_required": candidates.get("review_required", 0),
            "committed": candidates.get("committed", 0),
            "rejected": candidates.get("rejected", 0),
        },
        "ingestion": {
            "extracted": ingestion.get("extracted", 0),
            "stored": ingestion.get("stored", 0),
        },
        "extraction": {
            "completed": extraction.get("completed", 0),
            "cancelled": extraction.get("cancelled", 0),
        },
        "knowledge": {
            "items": knowledge_items,
            "layers": enabled_layers,
        },
        "memory": {
            "total_gb": mem_info.get("total", 0),
            "available_gb": mem_info.get("available", 0),
            "used_gb": round(mem_info.get("total", 0) - mem_info.get("available", 0), 1),
        },
        "db_size_mb": db_size,
        "facts_by_domain": {r["domain"]: r["cnt"] for r in facts},
        "governance": {
            "provisional": _db_query_one("SELECT COUNT(*) as cnt FROM candidate_facts WHERE status='provisional'")["cnt"],
            "human_review": _db_query_one("SELECT COUNT(*) as cnt FROM candidate_facts WHERE status='human_review'")["cnt"],
            "auto_rejected": _db_query_one("SELECT COUNT(*) as cnt FROM candidate_facts WHERE status='auto_rejected'")["cnt"],
        } if CANONICAL_DB.exists() else {},
    }
    # 监控水位告警 (P0-2)
    overview["alerts"] = _compute_alerts(overview)
    return overview


@app.get("/api/alerts")
@_cached("alerts")
async def api_alerts():
    """监控水位告警 (P0-2)：独立端点，便于轮询与外部监控接入。"""
    overview = await api_overview()
    alerts = overview.get("alerts", [])
    return {
        "alerts": alerts,
        "count": len(alerts),
        "critical": sum(1 for a in alerts if a["level"] == "crit"),
        "warning": sum(1 for a in alerts if a["level"] == "warn"),
        "thresholds": {
            "projector_lag_warn": PROJECTOR_LAG_WARN,
            "projector_lag_crit": PROJECTOR_LAG_CRIT,
            "human_review_warn": HUMAN_REVIEW_WARN,
            "pending_outbox_warn": PENDING_OUTBOX_WARN,
        },
    }


@app.get("/api/facts")
@_cached("facts")
async def api_facts(
    domain: str = None,
    fact_type: str = None,
    owner: str = None,
    q: str = None,
    limit: int = 50,
    offset: int = 0,
):
    """记忆页 — 事实列表"""
    where = ["status='active'"]
    params = []
    if domain:
        where.append("domain=?")
        params.append(domain)
    if fact_type:
        where.append("fact_type=?")
        params.append(fact_type)
    if owner:
        where.append("owner_principal=?")
        params.append(owner)
    if q:
        where.append("(content LIKE ? OR summary LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    where_clause = " AND ".join(where) if where else "1=1"
    
    # 总数
    count_row = _db_query_one(f"SELECT COUNT(*) as total FROM facts WHERE {where_clause}", tuple(params))
    total = count_row["total"] if count_row else 0

    # 列表
    facts = _db_query(
        f"SELECT fact_id, content, summary, domain, fact_type, owner_principal, decay_tier, confidence_score, "
        f"current_version, recorded_at, updated_at "
        f"FROM facts WHERE {where_clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )

    # 统计
    stats = _db_query(
        "SELECT domain, COUNT(*) as cnt FROM facts WHERE status='active' GROUP BY domain ORDER BY cnt DESC"
    )
    type_stats = _db_query(
        "SELECT fact_type, COUNT(*) as cnt FROM facts WHERE status='active' GROUP BY fact_type ORDER BY cnt DESC"
    )
    owner_stats = _db_query(
        "SELECT owner_principal, COUNT(*) as cnt FROM facts WHERE status='active' GROUP BY owner_principal ORDER BY cnt DESC"
    )

    # Provisional 候选（降权检索）
    provisional_facts = _db_query(
        "SELECT cf.candidate_id as fact_id, cf.content, cf.summary, cf.proposed_domain as domain, "
        "cf.proposed_fact_type as fact_type, cf.proposed_owner_principal as owner_principal, "
        "'1' as current_version, cf.created_at as recorded_at, "
        "cra.risk, cra.confidence, cra.recommendation, cf.status as governance_status "
        "FROM candidate_facts cf "
        "LEFT JOIN candidate_review_assessments cra ON cf.candidate_id = cra.candidate_id "
        "WHERE cf.status='provisional' ORDER BY cra.created_at DESC LIMIT ?",
        (limit,),
    )

    return {
        "total": total,
        "facts": facts,
        "stats": {
            "by_domain": {r["domain"]: r["cnt"] for r in stats},
            "by_type": {r["fact_type"]: r["cnt"] for r in type_stats},
            "by_owner": {r["owner_principal"]: r["cnt"] for r in owner_stats},
        },
        "provisional": provisional_facts,
    }


@app.get("/api/governance")
@_cached("governance")
async def api_governance():
    """治理页 — 评估记录、裁决记录、provisional 列表"""
    # provisional 候选
    provisional = _db_query(
        "SELECT cf.candidate_id, cf.content, cf.summary, cf.proposed_domain, cf.proposed_fact_type, "
        "cf.proposed_owner_principal, cf.created_at, "
        "cra.risk, cra.confidence, cra.recommendation, cra.summary as assessment_summary, "
        "cra.model, cra.created_at as assessed_at "
        "FROM candidate_facts cf "
        "LEFT JOIN candidate_review_assessments cra ON cf.candidate_id = cra.candidate_id "
        "WHERE cf.status='provisional' ORDER BY cra.created_at DESC"
    )

    # 最近评估统计
    stats = _db_query(
        "SELECT decision, COUNT(*) as cnt FROM governance_decisions "
        "GROUP BY decision ORDER BY cnt DESC"
    )
    
    # 待人工审核
    human_review = _db_query(
        "SELECT cf.candidate_id, cf.content, cf.summary, cf.proposed_domain, cf.proposed_fact_type, "
        "cra.risk, cra.confidence, cra.reasoning "
        "FROM candidate_facts cf "
        "LEFT JOIN candidate_review_assessments cra ON cf.candidate_id = cra.candidate_id "
        "WHERE cf.status='human_review' ORDER BY cf.created_at DESC"
    )

    return {
        "provisional": provisional,
        "human_review": human_review,
        "stats": {r["decision"]: r["cnt"] for r in stats},
    }


@app.get("/api/candidates")
@_cached("candidates")
async def api_candidates(status: str = "review_required", limit: int = 50):
    """审批页 — 候选列表"""
    # 新状态直接查数据库，旧状态走 Mímir API
    new_statuses = ("human_review", "provisional", "auto_rejected", "approved", "needs_more_evidence")
    if status in new_statuses:
        rows = _db_query(
            "SELECT candidate_id, content, summary, proposed_domain, proposed_fact_type, "
            "proposed_owner_principal, confidence_score, created_at, updated_at, status "
            "FROM candidate_facts WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        result = None
        candidates_data = rows
    else:
        result = await _mimir_get(f"/v8/learning/candidates?status={status}&limit={limit}")
        candidates_data = result.get("candidates", result if isinstance(result, list) else []) if result else []
    
    # 统计
    stats = _db_query(
        "SELECT status, COUNT(*) as cnt FROM candidate_facts GROUP BY status"
    )
    
    # 获取治理评估信息
    assessments = {}
    try:
        rows = _db_query(
            "SELECT candidate_id, risk, confidence, is_valuable, is_noise, recommendation, "
            "model, summary, reasoning, created_at "
            "FROM candidate_review_assessments ORDER BY created_at DESC"
        )
        for r in rows:
            cid = r["candidate_id"]
            if cid not in assessments:
                assessments[cid] = r
    except Exception:
        pass
    
    return {
        "candidates": candidates_data,
        "count": len(candidates_data),
        "stats": {r["status"]: r["cnt"] for r in stats},
        "assessments": assessments,
        "source": "api",
    }


@app.api_route("/api/candidates/{candidate_id}/review", methods=["GET", "POST"])
async def api_review_candidate(candidate_id: str, action: str = Query(...), reason: str = "dashboard review"):
    """审批操作"""
    token = _get_admin_token()
    if not token:
        raise HTTPException(403, "no admin token")
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ik = f"dashboard-{action}-{candidate_id[:8]}-{int(time.time())}"
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{MIMIR_API}/v8/candidates/{candidate_id}/review",
                headers=headers,
                json={"action": action, "reason": reason, "idempotency_key": ik},
            )
            if resp.status_code == 200:
                result = resp.json()
                # 如果 approve，自动 commit
                if action == "approve" and result.get("status") == "approved":
                    commit_ik = f"dashboard-commit-{candidate_id[:8]}-{int(time.time())}"
                    commit_resp = await client.post(
                        f"{MIMIR_API}/v8/candidates/{candidate_id}/commit",
                        headers=headers,
                        json={"candidate_id": candidate_id, "idempotency_key": commit_ik},
                    )
                    if commit_resp.status_code != 200:
                        result["commit_error"] = commit_resp.text[:300]
                # 清除缓存
                _invalidate("candidates", "overview")
                return result
            return {"error": resp.text}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/candidates/{candidate_id}/commit")
async def api_commit_candidate(candidate_id: str):
    """提交已批准候选（approved -> committed fact）。用于 auto-commit 失败后的补救。"""
    token = _get_admin_token()
    if not token:
        raise HTTPException(403, "no admin token")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ik = f"dashboard-commit-{candidate_id[:8]}-{int(time.time())}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{MIMIR_API}/v8/candidates/{candidate_id}/commit",
                headers=headers,
                json={"candidate_id": candidate_id, "idempotency_key": ik},
            )
            _invalidate("candidates", "overview")
            if resp.status_code == 200:
                return resp.json()
            return JSONResponse({"error": resp.text[:300]}, status_code=resp.status_code)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/conflicts")
async def api_conflicts(status: str = "open", limit: int = 50):
    """List fact conflicts (with both facts' content) for human adjudication."""
    data = await _mimir_get(f"/v12/conflicts?status={status}&limit={limit}")
    if data is None:
        return {"status": "error", "error": "mimir api unreachable"}
    return data


@app.post("/api/conflicts/{conflict_id}/resolve")
async def api_conflict_resolve(conflict_id: str, body: dict):
    """Resolve a conflict: winner stays active, loser becomes disputed."""
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{MIMIR_API}/v12/conflicts/{conflict_id}/resolve",
                headers=headers,
                json={"winner_fact_id": body.get("winner_fact_id", ""),
                      "reason": body.get("reason", "")},
            )
            _invalidate("conflicts")
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/conflicts/{conflict_id}/dismiss")
async def api_conflict_dismiss(conflict_id: str, body: dict | None = None):
    """Dismiss a conflict: both facts stay active."""
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{MIMIR_API}/v12/conflicts/{conflict_id}/dismiss",
                headers=headers, json={"reason": (body or {}).get("reason", "")},
            )
            _invalidate("conflicts")
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/agents")
@_cached("agents")
async def api_agents():
    """Agent 活动页"""
    # 各 Agent 事实统计
    stats = _db_query(
        "SELECT owner_principal, domain, fact_type, COUNT(*) as cnt "
        "FROM facts WHERE status='active' GROUP BY owner_principal, domain, fact_type"
    )
    
    # 聚合
    agents = {}
    for r in stats:
        agent = r["owner_principal"]
        if agent not in agents:
            agents[agent] = {"total": 0, "by_domain": {}, "by_type": {}}
        agents[agent]["total"] += r["cnt"]
        agents[agent]["by_domain"][r["domain"]] = agents[agent]["by_domain"].get(r["domain"], 0) + r["cnt"]
        agents[agent]["by_type"][r["fact_type"]] = agents[agent]["by_type"].get(r["fact_type"], 0) + r["cnt"]

    # 最近活动（过滤掉对话提取噪声，只保留重要事件）
    important_types = ("fact.created", "fact.migrated", "fact.tombstoned",
                       "candidate.created", "candidate.approved", "candidate.rejected",
                       "candidate.committed", "core_memory.promoted",
                       "knowledge.item_created", "candidate.assessed")
    placeholders = ",".join("?" for _ in important_types)
    recent = _db_query(
        f"SELECT event_type, occurred_at, event_seq FROM memory_events "
        f"WHERE event_type IN ({placeholders}) "
        f"ORDER BY event_seq DESC LIMIT 20",
        important_types,
    )

    # 今日新增事实
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    today_facts = _db_query(
        "SELECT owner_principal, COUNT(*) as cnt FROM facts "
        "WHERE status='active' AND recorded_at >= ? GROUP BY owner_principal",
        (today,),
    )

    # Gateway 实时状态 (user systemd units) + 各 profile 最近消息时间
    import subprocess
    gw_env = {**os.environ,
              "XDG_RUNTIME_DIR": "/run/user/1000",
              "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
    gw_map = [
        ("heimdallr", "hermes-gateway.service", Path.home() / ".hermes/state.db"),
        ("mentor", "hermes-gateway-mentor.service", Path.home() / ".hermes/profiles/mentor/state.db"),
        ("jarvis", "hermes-gateway-jarvis.service", Path.home() / ".hermes/profiles/jarvis/state.db"),
        ("quantmaster", "hermes-gateway-quantmaster.service", Path.home() / ".hermes/profiles/quantmaster/state.db"),
    ]
    gateways = []
    for principal, unit, dbp in gw_map:
        active = False
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", unit],
                               capture_output=True, text=True, timeout=5, env=gw_env)
            active = r.stdout.strip() == "active"
        except Exception:
            pass
        last_msg = None
        if dbp.exists():
            try:
                con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=5)
                row = con.execute("SELECT MAX(timestamp) FROM messages").fetchone()
                con.close()
                if row and row[0] is not None:
                    v = row[0]
                    try:
                        fv = float(v)
                        if fv > 1e12:
                            fv /= 1000.0
                        last_msg = datetime.fromtimestamp(fv, tz=timezone(timedelta(hours=8))).isoformat()
                    except (ValueError, TypeError):
                        last_msg = str(v)
            except Exception:
                pass
        gateways.append({"principal": principal, "unit": unit,
                         "active": active, "last_message": last_msg})

    return {
        "agents": agents,
        "recent_events": recent,
        "today_facts": {r["owner_principal"]: r["cnt"] for r in today_facts},
        "gateways": gateways,
    }


# ── CDC connector → state.db mapping for pipeline pulse ──
STATE_DBS = {
    "hermes-heimdallr-production": Path.home() / ".hermes/state.db",
    "hermes-mentor-profile": Path.home() / ".hermes/profiles/mentor/state.db",
    "hermes-jarvis-profile": Path.home() / ".hermes/profiles/jarvis/state.db",
    "hermes-quantmaster-profile": Path.home() / ".hermes/profiles/quantmaster/state.db",
}


@app.get("/api/pipeline")
@_cached("pipeline", ttl=15)
async def api_pipeline():
    """管线脉搏: CDC 连接器水位 vs 源库最大消息 id + mimir timers."""
    # owner derived from connector_id (checkpoints table has no owner column)
    owner_by_cid = {
        "hermes-heimdallr-production": "heimdallr",
        "hermes-mentor-profile": "mentor",
        "hermes-jarvis-profile": "jarvis",
        "hermes-quantmaster-profile": "quantmaster",
    }
    connectors = []
    for r in _db_query("SELECT connector_id, cursor_json FROM connector_checkpoints"):
        cid = r["connector_id"]
        try:
            wm = int(json.loads(r["cursor_json"] or "{}").get("last_message_rowid", 0))
        except Exception:
            wm = 0
        dbp = STATE_DBS.get(cid)
        src_max = 0
        if dbp and dbp.exists():
            try:
                con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=5)
                src_max = con.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
                con.close()
            except Exception:
                pass
        lag = max(0, src_max - wm) if src_max else 0
        connectors.append({
            "connector_id": cid, "owner": owner_by_cid.get(cid, "?"), "watermark": wm,
            "source_max": src_max, "lag": lag,
            "status": "ok" if lag < 500 else "lag",
        })
    import subprocess
    timers = []
    try:
        result = subprocess.run(["systemctl", "list-timers", "--no-pager"],
                                capture_output=True, text=True, timeout=10)
        for line in result.stdout.split("\n"):
            if "mimir" in line.lower() and "timer" in line:
                parts = line.split()
                if len(parts) >= 6:
                    timers.append({"next": parts[0] + " " + parts[1], "left": parts[2],
                                   "last": parts[3] + " " + parts[4], "ago": parts[5],
                                   "unit": parts[-1]})
    except Exception:
        pass
    return {"connectors": connectors, "timers": timers}


@app.get("/api/sources")
@_cached("sources")
async def api_sources():
    """来源与研读页"""
    learning = await _mimir_get("/v8/learning/status")
    knowledge = await _mimir_get("/v9/knowledge/status")

    # 来源统计
    sources = _db_query(
        "SELECT connector_type, COUNT(*) as cnt, MAX(ingested_at) as last_seen "
        "FROM conversation_sources GROUP BY connector_type"
    )

    # 知识项
    knowledge_items = _db_query(
        "SELECT item_id, layer, item_type, status, title, created_at, updated_at "
        "FROM knowledge_items WHERE status != 'archived' ORDER BY created_at DESC"
    )

    return {
        "sources": sources,
        "knowledge_items": knowledge_items,
        "learning": {
            "extracted": learning.get("ingestion_runs", {}).get("extracted", 0) if learning else 0,
            "stored": learning.get("ingestion_runs", {}).get("stored", 0) if learning else 0,
            "candidates_created": learning.get("extraction_runs", {}).get("completed", 0) if learning else 0,
        } if learning else {},
        "knowledge_layers": knowledge.get("knowledge_items", {}) if knowledge else {},
    }


@app.post("/api/knowledge/create")
async def api_knowledge_create(body: dict):
    """创建知识项"""
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}

    title = body.get("title", "")
    content = body.get("content", "")
    layer = body.get("layer", "wiki")
    domain = body.get("domain", "system")

    if not title or not content:
        return {"status": "error", "error": "title and content required"}

    import hashlib
    source_hash = hashlib.sha256(title.encode()).hexdigest()
    ik = f"dashboard-knowledge-{source_hash[:16]}-{int(time.time())}"

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "connector_type": "file",
        "title": title,
        "content": content,
        "owner_principal": "mentor",
        "domain": domain,
        "source_hash": source_hash,
        "idempotency_key": ik,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{MIMIR_API}/v9/knowledge/items",
                headers=headers,
                json=payload,
            )
            if resp.status_code in (200, 201):
                _invalidate("sources")
                return {"status": "ok", "result": resp.json()}
            return {"status": "error", "error": f"API error: {resp.text}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/knowledge/delete")
async def api_knowledge_delete(body: dict):
    """删除知识项（直接标记为 archived）"""
    item_id = body.get("item_id", "")
    if not item_id:
        return {"status": "error", "error": "item_id required"}

    try:
        conn = sqlite3.connect(str(CANONICAL_DB))
        conn.execute("UPDATE knowledge_items SET status='archived' WHERE item_id=?", (item_id,))
        conn.commit()
        conn.close()
        _invalidate("sources")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/source/add")
async def api_source_add(body: dict):
    """记录信息来源（暂存到知识库，后续开发自动采集）"""
    source_type = body.get("type", "unknown")
    url = body.get("url", "")
    if not url:
        return {"status": "error", "error": "url required"}

    import hashlib
    source_hash = hashlib.sha256(url.encode()).hexdigest()
    ik = f"dashboard-source-{source_hash[:16]}-{int(time.time())}"

    # 作为知识项存入 wiki 层
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 尝试获取页面标题
    title = f"{'RSS' if source_type=='rss' else '网页'}: {url[:60]}"
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mimir/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read(65536).decode("utf-8", errors="ignore")
            import re
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if m:
                t = m.group(1).strip()[:100]
                if t:
                    title = t
    except Exception:
        pass

    payload = {
        "connector_type": "file",
        "title": title,
        "content": f"来源类型: {source_type}\n地址: {url}\n录入时间: {datetime.now(timezone.utc).isoformat()}\n\n待开发自动采集功能。",
        "owner_principal": "mentor",
        "domain": "system",
        "source_hash": source_hash,
        "idempotency_key": ik,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{MIMIR_API}/v9/knowledge/items",
                headers=headers,
                json=payload,
            )
            if resp.status_code in (200, 201):
                _invalidate("sources")
                return {"status": "ok", "result": resp.json()}
            return {"status": "error", "error": f"API error: {resp.text}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/system")
@_cached("system")
async def api_system():
    """系统页"""
    health = await _mimir_get("/health")
    ready = await _mimir_get("/ready")
    projectors_data = await _mimir_get("/v8/projectors")

    # 数据库大小
    db_files = {}
    for f in ["canonical.db", "fts.db", "graph.db", "core_memory.db"]:
        p = CANONICAL_DB.parent / f
        if p.exists():
            db_files[f] = round(p.stat().st_size / 1024 / 1024, 1)

    chroma_path = CANONICAL_DB.parent / "chroma" / "chroma.sqlite3"
    if chroma_path.exists():
        db_files["chroma"] = round(chroma_path.stat().st_size / 1024 / 1024, 1)

    # 事件统计
    event_types = _db_query(
        "SELECT event_type, COUNT(*) as cnt FROM memory_events GROUP BY event_type ORDER BY cnt DESC"
    )

    # 投影器
    projectors = []
    if projectors_data:
        projectors = projectors_data.get("projectors", [])
    elif ready:
        projectors = ready.get("projectors", [])

    return {
        "version": health.get("version") if health else "?",
        "schema_version": health.get("schema_version") if health else "?",
        "service": health.get("service") if health else "?",
        "status": health.get("status") if health else "?",
        "facts": ready.get("facts", 0) if ready else 0,
        "event_head": ready.get("event_head", 0) if ready else 0,
        "principals": ready.get("principals", 0) if ready else 0,
        "pending": ready.get("pending", 0) if ready else 0,
        "dead_letters": ready.get("dead_letters", 0) if ready else 0,
        "projectors": projectors,
        "db_files": db_files,
        "event_types": {r["event_type"]: r["cnt"] for r in event_types},
    }




@app.get("/api/decay")

async def api_decay():
    """Decay curve statistics — tier distribution and decayed facts."""
    tiers = _db_query(
        "SELECT decay_tier, COUNT(*) as cnt FROM facts WHERE status='active' GROUP BY decay_tier"
    )
    decayed = _db_query(
        "SELECT COUNT(*) as cnt FROM facts WHERE status='archived' AND decayed_at IS NOT NULL"
    )
    events = _db_query(
        "SELECT event_type, COUNT(*) as cnt FROM memory_events WHERE event_type='fact.decayed' GROUP BY event_type"
    )
    return {
        "tier_distribution": {r["decay_tier"]: r["cnt"] for r in tiers},
        "total_decayed": decayed[0]["cnt"] if decayed else 0,
        "decay_events": events[0]["cnt"] if events else 0,
    }


@app.get("/api/trust")

async def api_trust():
    """Trust score distribution."""
    scores = _db_query(
        "SELECT confidence_score, COUNT(*) as cnt FROM facts WHERE status='active' GROUP BY confidence_score ORDER BY confidence_score"
    )
    high = sum(r["cnt"] for r in scores if r["confidence_score"] is not None and r["confidence_score"] >= 0.7)
    mid = sum(r["cnt"] for r in scores if r["confidence_score"] is not None and 0.3 <= r["confidence_score"] < 0.7)
    low = sum(r["cnt"] for r in scores if r["confidence_score"] is not None and r["confidence_score"] < 0.3)
    none_cnt = sum(r["cnt"] for r in scores if r["confidence_score"] is None)
    return {"high": high, "medium": mid, "low": low, "none": none_cnt, "total": high + mid + low + none_cnt}


@app.post("/api/search/trace")
async def api_search_trace(body: dict):
    """v12 recall funnel trace (proxies POST /v12/search/trace)."""
    text = (body or {}).get("text", "")
    if not text:
        return {"status": "error", "error": "text required"}
    limit = int((body or {}).get("limit", 10))
    dedup_threshold = float((body or {}).get("dedup_threshold", 0.8))
    token = _get_admin_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"text": text, "limit": limit, "candidate_limit": 50}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{MIMIR_API}/v12/search/trace?dedup_threshold={dedup_threshold}",
                headers=headers, json=payload,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/quality")
@_cached("quality")
async def api_quality():
    """v12 retrieval quality board: 7-day signals, quality_metrics, queued feedback."""
    metrics = _db_query(
        "SELECT date, query_count, hit_count, avg_score, zero_hit_count, "
        "useful_signals, useless_signals, evolved_at "
        "FROM quality_metrics ORDER BY date DESC LIMIT 14"
    )
    signals = _db_query(
        "SELECT signal, COUNT(*) as cnt FROM search_feedback GROUP BY signal"
    )
    recent_feedback = _db_query(
        "SELECT feedback_id, query_text, fact_id, signal, user_principal, created_at "
        "FROM search_feedback ORDER BY created_at DESC LIMIT 20"
    )
    evolved_events = _db_query(
        "SELECT payload_json, occurred_at FROM memory_events "
        "WHERE event_type='fact.evolved' ORDER BY event_seq DESC LIMIT 10"
    )
    total_feedback = sum(r["cnt"] for r in signals)
    return {
        "window_days": 7,
        "signals": {r["signal"]: r["cnt"] for r in signals},
        "total_feedback": total_feedback,
        "recent_cycles": metrics,
        "recent_feedback": recent_feedback,
        "evolved_events": [
            {"payload": r["payload_json"], "occurred_at": r["occurred_at"]}
            for r in evolved_events
        ],
    }


@app.get("/api/crystals")
async def api_crystals(status: str = "candidate"):
    """v12 skill crystallization candidates (M3b)"""
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{MIMIR_API}/v12/crystals?status={status}", headers=headers
            )
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/crystals/scan")
async def api_crystals_scan(body: dict | None = None):
    """Trigger a v12 crystal scan (clusters recent facts by topic)."""
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}
    window_days = int((body or {}).get("window_days", 7))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{MIMIR_API}/v12/crystals/scan?window_days={window_days}",
                headers=headers, json={},
            )
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/crystals/{candidate_id}/approve")
async def api_crystals_approve(candidate_id: str, body: dict | None = None):
    """Human approve a crystallization candidate -> materializes a pattern fact."""
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}
    owner = (body or {}).get("owner_principal") or "mentor"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{MIMIR_API}/v12/crystals/{candidate_id}/approve",
                headers=headers, json={"owner_principal": owner},
            )
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/crystals/{candidate_id}/dismiss")
async def api_crystals_dismiss(candidate_id: str, body: dict | None = None):
    """Reject a crystallization candidate."""
    token = _get_admin_token()
    if not token:
        return {"status": "error", "error": "no admin token"}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{MIMIR_API}/v12/crystals/{candidate_id}/dismiss",
                headers=headers, json={"reason": (body or {}).get("reason", "")},
            )
            if resp.status_code in (200, 201):
                return resp.json()
            return {"status": "error", "error": f"API: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/api/opinions")
async def api_opinions(owner: str | None = None, limit: int = Query(default=50)):
    """v10 Opinions 列表"""
    params = {"limit": limit}
    if owner:
        params["owner"] = owner
    return await _mimir_get_params("/v10/opinions", params)


@app.get("/api/observations")
async def api_observations(owner: str | None = None, limit: int = Query(default=20)):
    """v10 Observations 列表"""
    params = {"limit": limit}
    if owner:
        params["owner"] = owner
    return await _mimir_get_params("/v10/observations", params)


@app.get("/v10/opinions")
@app.get("/v10/observations")
async def _mimir_get_params(path: str, params: dict) -> dict:
    """辅助函数：带参数访问 Mímir API"""
    import urllib.parse
    token = _get_admin_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    suffix = ""
    if params:
        suffix = "?" + urllib.parse.urlencode(params)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{MIMIR_API}{path}{suffix}", headers=headers)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {}


@app.get("/api/timers")
async def api_timers():
    """Systemd timer status for Mimir workers."""
    import subprocess
    result = subprocess.run(
        ["systemctl", "list-timers", "--no-pager"],
        capture_output=True, text=True, timeout=10
    )
    lines = result.stdout.split("\n")
    timers = []
    for line in lines:
        if "mimir" in line.lower() and "timer" in line:
            parts = line.split()
            if len(parts) >= 5:
                timers.append({
                    "next": parts[0] + " " + parts[1] if len(parts) > 1 else "?",
                    "left": parts[2] if len(parts) > 2 else "?",
                    "last": parts[3] + " " + parts[4] if len(parts) > 4 else "?",
                    "ago": parts[5] if len(parts) > 5 else "?",
                    "unit": parts[-1] if parts else "?",
                })
    return {"timers": timers}

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Mímir Dashboard</h1><p>Frontend not built yet.</p>")


@app.post("/api/observations/consolidate")
async def api_consolidate_observations():
    """通过 v10 API 触发 observation 汇总"""
    token = _get_admin_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{MIMIR_API}/v10/observations/consolidate", headers=headers, json={})
            return resp.json()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/governance/run")
async def api_governance_run(body: dict):
    """通过 v10 API 触发治理流水线"""
    token = _get_admin_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{MIMIR_API}/v10/governance/run", headers=headers, json=body or {"dry_run": False})
            return resp.json()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/governance/decisions")
async def api_governance_decisions(limit: int = Query(default=50, ge=1, le=200)):
    """查询治理裁决记录"""
    rows = _db_query(
        "SELECT decision_id, candidate_id, decision, reason, previous_status, new_status, created_at "
        "FROM governance_decisions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return {"decisions": rows, "count": len(rows)}


# ── v13/v14: skills / federation / blackboard / projection ───────────────

@app.get("/api/skills")
@_cached("skills")
async def api_skills():
    """v14 AutoSkill: competent wiki candidates + already-promoted skills.

    Dual view from one endpoint: candidates come from the /v14 API;
    promoted skills are read straight off the canonical facts table
    (fact_type='skill').
    """
    api_resp = await _mimir_get("/v14/skills/candidates") or {}
    candidates = api_resp.get("candidates", [])
    promoted = _db_query(
        "SELECT fact_id, content, summary, owner_principal, "
        "domain, status, created_at FROM facts "
        "WHERE fact_type='skill' ORDER BY created_at DESC LIMIT 200"
    )
    ledger = _db_query(
        "SELECT topic, success_count, skill_fact_id, last_success_at "
        "FROM skill_topics ORDER BY success_count DESC, topic LIMIT 500"
    )
    return {
        "status": "ok",
        "candidates": candidates,
        "promoted": promoted,
        "ledger": ledger,
    }


@app.post("/api/skills/promote")
async def api_skills_promote(body: dict):
    """One-click approval: materialize a competent topic as an L3 skill."""
    result = await _mimir_post("/v14/skills/promote", {
        "topic": (body or {}).get("topic", ""),
    })
    if result is None:
        return {"status": "error",
                "error": "promote failed (below threshold or API down)"}
    _invalidate("skills")
    return result


@app.get("/api/federation")
@_cached("federation")
async def api_federation():
    """v14 cross-node CRDT federation: peer registry + event ledger stats.

    Federation has no REST surface by design (it is a node-to-node
    protocol); the dashboard reads the two additive tables directly.
    """
    peers = _db_query(
        "SELECT node_id, fingerprint, registered_at "
        "FROM federation_peers ORDER BY registered_at"
    )
    events_total = _db_query_one(
        "SELECT COUNT(*) AS n FROM federation_events"
    )
    events_by_op = _db_query(
        "SELECT op, COUNT(*) AS count FROM federation_events GROUP BY op"
    )
    lamport_max = _db_query_one(
        "SELECT MAX(lamport) AS m FROM federation_events"
    )
    recent = _db_query(
        "SELECT seq, event_id, crdt_key, lamport, node_id, op, recorded_at "
        "FROM federation_events ORDER BY seq DESC LIMIT 50"
    )
    return {
        "status": "ok",
        "configured": bool(peers) or bool(events_total and events_total["n"]),
        "peers": peers,
        "events_total": events_total["n"] if events_total else 0,
        "events_by_op": events_by_op,
        "lamport_max": lamport_max["m"] if lamport_max else 0,
        "recent_events": recent,
    }


@app.get("/api/blackboard")
@_cached("blackboard")
async def api_blackboard():
    """v13 shared blackboards: board list + entry counts (read-only view).

    The active board surface stays on the /v13 API; this endpoint gives
    the dashboard a census across all boards.
    """
    boards = _db_query(
        "SELECT board_id, title, participants, status, created_at, ended_at "
        "FROM blackboards ORDER BY created_at DESC LIMIT 200"
    )
    counts = _db_query(
        "SELECT board_id, COUNT(*) AS entries FROM blackboard_entries "
        "GROUP BY board_id"
    )
    count_map = {c["board_id"]: c["entries"] for c in counts}
    for board in boards:
        board["entry_count"] = count_map.get(board["board_id"], 0)
    active_entries = _db_query(
        "SELECT e.seq, e.board_id, e.author, e.content, e.created_at "
        "FROM blackboard_entries e JOIN blackboards b "
        "ON e.board_id=b.board_id "
        "WHERE b.status='active' ORDER BY e.seq DESC LIMIT 50"
    )
    return {
        "status": "ok",
        "boards": boards,
        "active_entries": active_entries,
    }


@app.post("/api/projection")
async def api_projection(body: dict):
    """v14 cross-model projection preview: search → tier injection blocks.

    Proxies /v14/projection so the dashboard UI can show what each model
    tier would receive for a given query, without exposing the admin
    token to the browser.
    """
    payload = {
        "text": (body or {}).get("text", ""),
        "tier": (body or {}).get("tier", "claude"),
        "limit": int((body or {}).get("limit", 10)),
    }
    result = await _mimir_post("/v14/projection", payload)
    if result is None:
        return {"status": "error",
                "error": "projection failed (bad tier or API down)"}
    return result


# ── v11: symbolic memory + code graph proxy ──────────────────────────────@app.post("/v11/symbolic/offload")
async def v11_symbolic_offload(body: dict):
    """转发符号记忆卸载到 Mímir v11 API"""
    return await _mimir_post("/v11/symbolic/offload", dict(body))


@app.get("/v11/symbolic/canvas")
async def v11_symbolic_canvas(session_key: str = "dashboard"):
    """转发符号画布查询"""
    return await _mimir_get(f"/v11/symbolic/canvas?session_key={session_key}")


@app.get("/v11/symbolic/{node_id}")
async def v11_symbolic_recall(node_id: str):
    """转发符号原文召回"""
    return await _mimir_get(f"/v11/symbolic/{node_id}")


@app.get("/v11/code/search")
async def v11_code_search(q: str, limit: int = Query(default=30, le=50)):
    """转发代码符号搜索"""
    return await _mimir_get(f"/v11/code/search?q={q}&limit={limit}")


@app.get("/v11/code/impact/{symbol_id}")
async def v11_code_impact(symbol_id: str):
    """转发代码影响分析"""
    return await _mimir_get(f"/v11/code/impact/{symbol_id}")


# Static files are served via nginx/cloudflare; API only here


# ── 启动入口 ──────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8800"))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=True)