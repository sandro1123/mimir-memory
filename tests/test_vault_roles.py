"""Tests for the declarative vault document-role table."""
from pathlib import Path

import pytest

from mimir_v8.vault_roles import (
    ROLE_HUB,
    ROLE_MACHINE_LOG,
    ROLE_REFERENCE_ONLY,
    ROLE_UNKNOWN,
    ROLE_VERSIONED,
    ROLE_WORKLOG,
    load_role_table,
    resolve_role,
)


def _table():
    return load_role_table()


def test_index_filename_is_hub():
    role, reason = resolve_role("30-知识库/GitHub 项目参考/索引.md", {}, _table())
    assert role == ROLE_HUB
    assert "filename" in reason


def test_sensitive_dir_is_reference_only_and_wins_over_everything():
    role, reason = resolve_role("60-敏感信息/API 密钥管理.md", {}, _table())
    assert role == ROLE_REFERENCE_ONLY
    assert "reference_only" in reason


def test_machine_frontmatter_type_is_machine_log():
    role, _ = resolve_role(
        "10-项目/x/运维记录/mimir-daily-report-2026-10-26.md",
        {"type": "mimir-daily-report"},
        _table(),
    )
    assert role == ROLE_MACHINE_LOG


def test_version_snapshot_filename_is_versioned():
    role, _ = resolve_role(
        "10-项目/Mímir 联邦记忆系统/白皮书/"
        "Mímir-v9.1-联邦记忆系统技术白皮书-2026-08-06.md",
        {},
        _table(),
    )
    assert role == ROLE_VERSIONED


def test_worklog_dir_is_worklog():
    role, _ = resolve_role(
        "10-项目/QuantStar/逐层审计/01-数据层审计-2026-08-18.md", {}, _table()
    )
    assert role == ROLE_WORKLOG


def test_unknown_falls_back_to_unknown():
    role, reason = resolve_role("随便/某个笔记.md", {}, _table())
    assert role == ROLE_UNKNOWN
    assert "default" in reason


def test_table_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_role_table(Path("/nonexistent/vault_roles.yaml"))


def test_weekly_reflect_is_machine_log():
    role, _ = resolve_role("x/mimir-weekly-reflect-2026-10-04.md", {}, _table())
    assert role == ROLE_MACHINE_LOG
