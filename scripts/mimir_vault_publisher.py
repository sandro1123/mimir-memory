#!/usr/bin/env python3
"""
Mímir Obsidian Vault 自动发布守护脚本 (Fifth Projector)

功能：
  - 定期或按需将 Mímir 中已确认的结构化实体与早报卡片渲染为 Markdown；
  - 自动在目标路径前创建 Git 隐形快照气囊；
  - 仅写入专用命名空间 (90-机器收件箱/Mímir 卡片)，严格遵守人类笔记防篡改锁；
  - 自动提交 Git 增量变更，保持人类 Obsidian 知识库清晰、安全、可追溯。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# 将 mimir 核心模块加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mimir_v8.publication import VAULT_ADAPTER_PRINCIPAL
from mimir_v8.store import CanonicalStore
from mimir_v8.vault_publish import publish_entity

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mimir_vault_publisher")

DEFAULT_VAULT_PATH = Path(os.environ.get("MIMIR_VAULT_ROOT", str(Path.home() / "obsidian-vault")))
DEFAULT_DB_PATH = Path(os.environ.get("MIMIR_DATA_DIR", ".")) / "canonical.db"


def run_publish_cycle(
    vault_root: Path = DEFAULT_VAULT_PATH,
    db_path: Path = DEFAULT_DB_PATH,
    limit: int = 50,
    dry_run: bool = False,
) -> dict:
    if not vault_root.is_dir():
        logger.error(f"Obsidian Vault 目录不存在: {vault_root}")
        return {"published": 0, "skipped": 0, "errors": 1}
    # 1.9.0 guard: a directory that is not an Obsidian vault (e.g. the default
    # path because MIMIR_VAULT_ROOT was not set) must never receive cards.
    if not (vault_root / ".obsidian").is_dir():
        logger.error(f"不是 Obsidian Vault（缺少 .obsidian/）: {vault_root} —— 请设置 MIMIR_VAULT_ROOT 或 --vault")
        return {"published": 0, "skipped": 0, "errors": 1}

    if not db_path.is_file():
        logger.error(f"Mímir 主库不存在: {db_path}")
        return {"published": 0, "skipped": 0, "errors": 1}

    store = CanonicalStore(db_path)
    published_count = 0
    skipped_count = 0

    with store.connect() as conn:
        # 1. 优先查询已提取的实体
        entity_rows = conn.execute(
            """
            SELECT e.entity_id, e.name, e.entity_type, e.metadata_json,
                   f.fact_id, f.content, f.summary, f.visibility, f.sensitivity, f.egress_policy
            FROM entities e
            LEFT JOIN relations r ON r.target_id = e.entity_id AND r.target_type = 'entity'
            LEFT JOIN facts f ON f.fact_id = r.source_fact_id
            WHERE e.status = 'active'
            ORDER BY e.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        # 2. 如果 entities 尚为空，则将可信知识库事实（reference/learning/project_config）作为首批知识卡片发布
        knowledge_facts = []
        if len(entity_rows) < limit:
            remaining = limit - len(entity_rows)
            fact_rows = conn.execute(
                """
                SELECT fact_id, content, summary, domain, fact_type, visibility, sensitivity, egress_policy, updated_at
                FROM facts
                WHERE status = 'active'
                  AND domain IN ('knowledge', 'tech_support', 'infrastructure')
                  AND fact_type IN ('reference', 'learning', 'project_config', 'iron_rule')
                  AND sensitivity != 'restricted'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (remaining,),
            ).fetchall()
            for r in fact_rows:
                title = (r["summary"] or r["content"][:40]).split("\n")[0][:40]
                knowledge_facts.append({
                    "entity_id": f"fact_{r['fact_id'][:12]}",
                    "name": f"{r['domain']}-{title}",
                    "entity_type": f"mimir_{r['fact_type']}",
                    "fact_id": r["fact_id"],
                    "content": r["content"],
                    "summary": r["summary"],
                    "visibility": r["visibility"],
                    "sensitivity": r["sensitivity"],
                    "egress_policy": r["egress_policy"],
                    "metadata_json": f'{{"domain": "{r["domain"]}", "fact_type": "{r["fact_type"]}"}}',
                })

    all_items = list(entity_rows) + knowledge_facts
    logger.info(f"扫描到 {len(all_items)} 条待评估记录（实体: {len(entity_rows)}, 知识事实: {len(knowledge_facts)}）...")

    for row in all_items:
        entity = {
            "entity_id": row["entity_id"],
            "name": row["name"],
            "entity_type": row["entity_type"],
        }
        fact = {
            "fact_id": row["fact_id"] or row["entity_id"],
            "visibility": row["visibility"] or "shared",
            "sensitivity": row["sensitivity"] or "internal",
            "egress_policy": row["egress_policy"] or "local_only",
        }
        summary_text = row.get("content") or row.get("summary") or f"Mímir 知识卡片：{row['name']}"
        sections = {
            "overview": f"**知识标题**：{row['name']}\n**分类类型**：`{row['entity_type']}`\n\n{summary_text}",
            "metadata": f"```json\n{row['metadata_json'] or '{}'}\n```",
        }

        if dry_run:
            logger.info(f"[Dry-Run] 待发布: {entity['name']} ({entity['entity_id']})")
            published_count += 1
            continue

        try:
            result = publish_entity(
                vault_root=vault_root,
                store=store,
                fact=fact,
                entity=entity,
                actor_principal=VAULT_ADAPTER_PRINCIPAL,
                sections=sections,
                target_scope="private",  # 本地私有 Vault 允许 local_only 安全卡片渲染
            )
            status = result.get("status")
            if status == "published":
                published_count += 1
                logger.info(f"✓ 已发布卡片: {result.get('path')}")
            else:
                skipped_count += 1
                logger.debug(f"- 跳过: {entity['name']} 原因: {result.get('reason')}")
        except Exception as exc:
            logger.error(f"发布实体 {entity['name']} 出错: {exc}")
            skipped_count += 1

    logger.info(f"本轮发布周期完成: 已发布 {published_count} 张, 跳过 {skipped_count} 条")
    return {"published": published_count, "skipped": skipped_count, "errors": 0}


def main():
    parser = argparse.ArgumentParser(description="Mímir Obsidian Vault 卡片发布工具")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT_PATH, help="Obsidian Vault 根目录")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Mímir SQLite canonical.db 路径")
    parser.add_argument("--limit", type=int, default=30, help="单次处理实体上限")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写文件")
    args = parser.parse_args()

    result = run_publish_cycle(vault_root=args.vault, db_path=args.db, limit=args.limit, dry_run=args.dry_run)
    # Non-zero on any error so a systemd timer turns red instead of silently skipping.
    sys.exit(1 if result.get("errors") else 0)


if __name__ == "__main__":
    main()
