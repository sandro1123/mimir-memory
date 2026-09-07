# -*- coding: utf-8 -*-
"""P41 Dashboard v4 聚合端点测试
spec: docs/plans/2026-09-05-dashboard-v4-customer-redesign-design.md §3.2
plan: docs/plans/2026-09-05-dashboard-v4-implementation.md Task 1/2

三态覆盖：正常形状 / partial 降级 / 空态（今日端点）
+ library 卡片与筛选 / health-light 绿黄红
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest


def _load_main():
    """独立加载 dashboard/main.py（绕 sys.modules 缓存，测试间互不污染）。"""
    spec = importlib.util.spec_from_file_location(
        "dash_main_p41", str(Path(__file__).resolve().parents[1] / "dashboard" / "backend" / "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _async_ret(v):
    async def f(path=None):
        return v
    return f


class TestTodayEndpoint:
    def test_normal_shape(self, monkeypatch):
        mod = _load_main()

        async def fake_get(path):
            if path.startswith("/v8/learning/candidates"):
                return {"candidates": [{
                    "candidate_id": "c1", "content": "用户偏好夜间部署",
                    "summary": "用户偏好夜间部署", "proposed_domain": "ops",
                    "confidence_score": 0.4, "created_at": "2026-09-05T10:00:00+08:00",
                    "status": "review_required"}]}
            return None
        monkeypatch.setattr(mod, "_mimir_get", fake_get)

        learned_row = {"domain": "quantstar", "summary": "铁律：qs 与 mimir 分离",
                       "confidence_score": 1.0, "source_name": "导师对话",
                       "recorded_at": "2026-09-05T09:00:00+08:00",
                       "fact_id": "f1"}
        decayed_row = {"summary": "旧备份策略",
                       "recorded_at": "2026-08-20T00:00:00+08:00"}

        def fake_q(query, params=(), database=None):
            if "decay_tier" in query:
                return [decayed_row]
            if "date(created_at)" in query or "date('now'" in query:
                return [learned_row]
            return []
        monkeypatch.setattr(mod, "_db_query", fake_q)

        result = asyncio.run(mod.api_dashboard_today())
        g = result["groups"][0]
        assert g["kind"] == "today"
        # 学到：置信 1.0 → 很确定
        assert g["learned"][0]["confidence_label"] == "很确定"
        assert g["learned"][0]["source_name"] == "导师对话"
        # 待审：置信 0.4 → 待观察（<0.5 档），candidate_id 透传
        assert g["pending"][0]["candidate_id"] == "c1"
        assert g["pending"][0]["confidence_label"] == "待观察"
        # 淡忘：summary 透传
        assert g["decayed"][0]["summary"] == "旧备份策略"
        assert result["degraded"] is False
        assert result["error_sources"] == []

    def test_partial_degraded(self, monkeypatch):
        mod = _load_main()

        async def dead_get(path):
            return None  # Mímir API 全断
        monkeypatch.setattr(mod, "_mimir_get", dead_get)

        def dead_q(query, params=(), database=None):
            raise RuntimeError("sqlite locked")  # DB 也断
        monkeypatch.setattr(mod, "_db_query", dead_q)

        result = asyncio.run(mod.api_dashboard_today())
        assert result["degraded"] is True
        assert len(result["error_sources"]) >= 2  # candidates + facts 至少各标一
        # 降级不白屏：groups 骨架仍在
        assert result["groups"][0]["kind"] == "today"

    def test_empty_state_not_blank(self, monkeypatch):
        mod = _load_main()

        async def ok_get(path):
            if path.startswith("/v8/learning/candidates"):
                return {"candidates": []}
            return None
        monkeypatch.setattr(mod, "_mimir_get", ok_get)
        monkeypatch.setattr(mod, "_db_query", lambda q, p=(), database=None: [])

        result = asyncio.run(mod.api_dashboard_today())
        g = result["groups"][0]
        assert g["learned"] == [] and g["pending"] == [] and g["decayed"] == []
        assert result["degraded"] is False  # 空数据≠降级


class TestLibraryEndpoint:
    def test_cards_and_three_tier_labels(self, monkeypatch):
        mod = _load_main()
        monkeypatch.setattr(mod, "_mimir_get", _async_ret({"candidates": []}))

        card_row = {"domain": "ops", "summary": "卡片一", "confidence_score": 0.62,
                    "source_name": "导师对话", "recorded_at": "2026-09-05T08:00:00+08:00",
                    "fact_id": "f9"}

        def fake_q(query, params=(), database=None):
            if "COUNT" in query.upper():
                return [{"cnt": 7}]
            return [card_row]
        monkeypatch.setattr(mod, "_db_query", fake_q)

        r = asyncio.run(mod.api_dashboard_library())
        assert r["cards"][0]["confidence_label"] == "还行"  # 0.62 → 0.5~0.8 档
        assert r["total"] == 7
        assert r["pending_count"] == 0
        assert r["degraded"] is False

    def test_filter_disputed_and_pending(self, monkeypatch):
        mod = _load_main()
        monkeypatch.setattr(mod, "_mimir_get", _async_ret({"candidates": []}))
        calls = []

        def fake_q(query, params=(), database=None):
            calls.append(query)
            if "COUNT" in query.upper():
                return [{"cnt": 1}]
            return [{"domain": "x", "summary": "筛选卡", "confidence_score": 0.5,
                     "source_name": "双源", "recorded_at": "2026-09-04T08:00:00+08:00",
                     "fact_id": "f2"}]
        monkeypatch.setattr(mod, "_db_query", fake_q)

        r1 = asyncio.run(mod.api_dashboard_library(filter="disputed"))
        assert r1["cards"][0]["fact_id"] == "f2"
        assert any("disputed" in q for q in calls)

        calls.clear()
        r2 = asyncio.run(mod.api_dashboard_library(filter="pending"))
        # pending = 已入库但 human_status='unreviewed'（真键——2026-09-07 对表钉死）
        assert any("unreviewed" in q for q in calls)


class TestHealthLightEndpoint:
    def _ready_ok(self):
        return {"status": "ready", "dead_letters": 0, "pending": 0,
                "projectors": [{"projector_name": "fts", "status": "idle",
                                "last_error_code": None, "pending": 0, "dead_letter": 0}]}

    def test_green(self, monkeypatch):
        mod = _load_main()

        async def fake_get(path):
            if path == "/ready":
                return self._ready_ok()
            if path == "/v8/learning/status":
                return {"candidates": {"review_required": 2}}
            return None
        monkeypatch.setattr(mod, "_mimir_get", fake_get)

        r = asyncio.run(mod.api_dashboard_health_light())
        assert r["light"] == "green"
        assert r["reasons"] == []

    def test_yellow_pending_backlog(self, monkeypatch):
        mod = _load_main()

        async def fake_get(path):
            if path == "/ready":
                return self._ready_ok()
            if path == "/v8/learning/status":
                # 真键 human_review（review_required 生产恒空——2026-09-07 对表钉死）
                return {"candidates": {"human_review": 31}}  # >30 待审 → 黄
            return None
        monkeypatch.setattr(mod, "_mimir_get", fake_get)

        r = asyncio.run(mod.api_dashboard_health_light())
        assert r["light"] == "yellow"
        assert any("待审" in x for x in r["reasons"])

    def test_red_api_down(self, monkeypatch):
        mod = _load_main()

        async def dead(path):
            return None
        monkeypatch.setattr(mod, "_mimir_get", dead)

        r = asyncio.run(mod.api_dashboard_health_light())
        assert r["light"] == "red"
        assert r["reasons"]
