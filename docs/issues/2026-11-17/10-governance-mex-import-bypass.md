# MEX 导入直写 `facts`，绕过候选评审；列名来自载荷

> **状态（2026-11-25）**：已修复，见 #70（1.9.2，列名白名单；MEX 作为本地迁移通道保留）。


**标签**：security, governance
**严重度**：Medium（本地 CLI 路径；但违反"候选先行"判词）
**影响版本**：1.1.0 起

## 问题一：绕过治理管线

`mimir_v8/mex.py` 的 `mex_import`（200 起）把信封里的 `facts` 直接 `INSERT INTO facts`，
`candidate_facts` 与 `candidate_evidence` 只是"顺带补齐的 FK 靶子"，没有任何评审：

```python
for fact in facts:
    ...
    connection.execute(f"INSERT INTO facts ({fact_columns}) VALUES ({fact_marks})", ...)
```

`actor_principal` 参数读完整个函数只用于审计/事件字段，**不参与任何授权判定**
（`rg actor_principal mimir_v8/mex.py` 只有 6 处，全是写入字段）。

后果：一条 `mimir-cli mex-import` 就能把任意 `owner_principal`、任意 `status='active'`、
任意 `visibility='all'` 的事实写进 canonical 库。这与项目最核心的判词
"自动获取 ≠ 自动入库 / 一切新记忆先过候选与三档判重"冲突，也让
"UPDATE/DELETE 永不自动"之外的 ADD 侧失去了唯一的闸门。

目前没有 HTTP 路由暴露这条路（只在 CLI），所以严重度是 Medium 而非 High；但它是
"治理可以绕过"的既有事实，一旦有人加了个 `/v8/mex/import` 端点，就成了 High。

## 问题二：列名插值

阶段一写 `sources` / `conversation_sources` 时：

```python
connection.execute(
    f"INSERT OR IGNORE INTO {table} "
    f"({', '.join(row.keys())}) "
    f"VALUES ({', '.join('?' * len(row))})",
    tuple(row.values()))
```

`row` 来自信封载荷，`row.keys()` 未经过任何白名单校验。值走参数化（安全），
**列名是拼进 SQL 的**。一个精心构造的 MEX 文件可以借此改写语句结构。
`validate_envelope` 只校验版本与段哈希，不校验字段名集合。

## 修复

1. 导入默认走候选：`mex_import(..., mode="candidate")` 把每条事实转成 `CreateCandidate`，
   只有显式 `--direct-write`（要求 admin + 审计理由）才直写，且必须写 `memory_events`。
2. 列名改为"取目标表 PRAGMA 列与载荷键的交集"，交集为空或含未知键 → 拒绝整包。
3. `actor_principal` 参与授权：直写模式要求 `is_admin=True`。

## 验收

- 默认导入后 `facts` 不变、`candidate_facts` 增加，且候选可被评审界面看到。
- 载荷里塞 `"x) VALUES (1); DROP TABLE facts; --"` 作为键 → 整包拒绝，库无变化。
- `tests/test_mex_roundtrip.py` 扩两条上述用例。
