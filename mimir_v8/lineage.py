# -*- coding: utf-8 -*-
"""谱系哈希链（1.1.0）— fact_versions 的 tamper-evident 链式咬合。

对标 aduMEI v20.5 的密码学谱系（Roadmap 差距#6）。设计要点：

* **单列成链**：只加 `fact_versions.previous_version_hash` 一列。第 n 版
  该列存 lineage_hash(n-1)，即上一版快照的链值。verify 逐版重算即可
  定位断裂点——不需要额外哈希列表，也不改既有 content_hash 语义
  （content_hash 仍是内容哈希，被全库投影/去重消费，动它会波及面过大）。
* **不可变表不倒填**：fact_versions 有 immutability 触发器（UPDATE/DELETE
  一律 ABORT）。因此**不回填**旧行——回填本身即篡改历史，破坏事件溯源
  不变量。旧行链值留 NULL，语义是「此链在 1.1.0 之前从未封存」，如实
  披露而非伪造。触发器反过来保证：攻击者无法把已封存链接改回 NULL。
* **三态判语**：verify 不返回布尔二态。无任何封存链接时返回
  ``chain_status="unsealed"``——**空集必须显式报警，不得静默输出零**
  （绿灯不制造虚假信心）。三种态：sealed_ok / broken / unsealed。

链值定义：lineage_hash(prev, snapshot_json) = sha256(f"{prev or ''}:{snapshot_json}")。
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

#: 链已封存且逐环验证通过
STATUS_SEALED_OK = "sealed_ok"
#: 存在断裂环（tamper-evident 命中）
STATUS_BROKEN = "broken"
#: 无任何封存链接（1.1.0 之前的旧事实）——不是「完好」，是「无从验证」
STATUS_UNSEALED = "unsealed"


def compute_lineage_hash(previous_hash: str | None, snapshot_json: str) -> str:
    """第 n 版链值 = sha256(prev_lineage + ':' + snapshot_json)。

    ``previous_hash`` 为空（genesis 或 1.1.0 前的旧行）时按空串参与，
    保证确定性。
    """
    seed = f"{previous_hash or ''}:{snapshot_json}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def lineage_hash_for_new_version(
    connection: sqlite3.Connection, fact_id: str, version: int
) -> str:
    """新写第 ``version`` 版时应存的链值 = lineage_hash(version-1)。

    genesis（version<=1）返回空串（链首无前驱）。前驱行缺失（异常态）
    返回空串——宁可留一个可被 verify 指认的断点，也不编造链值。
    """
    if version <= 1:
        return ""
    row = connection.execute(
        "SELECT snapshot_json, previous_version_hash FROM fact_versions "
        "WHERE fact_id=? AND version=?",
        (fact_id, version - 1),
    ).fetchone()
    if row is None:
        return ""
    return compute_lineage_hash(row["previous_version_hash"], row["snapshot_json"])


def verify_lineage_chain(store: Any, fact_id: str) -> dict:
    """逐版本重算链值，比对存值，定位破链版本号。

    返回 ``{fact_id, chain_status, chain_intact, breaks, sealed_links,
    versions}``：

    * ``chain_status``：三态判语（sealed_ok / broken / unsealed）
    * ``chain_intact``：仅当 sealed_ok 为真（unsealed 时**不为真**——无从
      验证不等于完好）
    * ``breaks``：对不上的版本号（该版存值与前一版重算值不符）
    * ``sealed_links``：带链值的版本数（genesis 不计）
    * ``versions``：总版本数

    genesis（v1）与其后直至第一个带链值的版本之间的 NULL 段视为「未封存
    前缀」，不计断裂；一旦遇到封存链接，其后逐环必须咬合。
    """
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT version, snapshot_json, previous_version_hash "
            "FROM fact_versions WHERE fact_id=? ORDER BY version",
            (fact_id,),
        ).fetchall()

    breaks: list[int] = []
    sealed_links = 0
    chain_open = False
    prev_lineage: str | None = None
    for row in rows:
        version = row["version"]
        stored = row["previous_version_hash"]
        if version > 1 and stored:
            sealed_links += 1
            chain_open = True
        if chain_open and version > 1:
            expected = prev_lineage or ""
            if (stored or "") != expected:
                breaks.append(version)
        prev_lineage = compute_lineage_hash(stored, row["snapshot_json"])

    if breaks:
        status = STATUS_BROKEN
    elif sealed_links == 0:
        status = STATUS_UNSEALED
    else:
        status = STATUS_SEALED_OK
    total = len(rows)
    return {
        "fact_id": fact_id,
        "chain_status": status,
        "chain_intact": status == STATUS_SEALED_OK,
        "breaks": breaks,
        "sealed_links": sealed_links,
        "versions": total,
    }