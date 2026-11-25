# `core_memory` 不在 `KNOWN_SCOPES`，该 scope 无法签发

> **状态（2026-11-25）**：已修复，见 #73 / PR #75（1.9.2）。


**标签**：bug, auth
**严重度**：Medium
**影响版本**：1.7.0（#30 落地后）

## 问题

`mimir_v8/auth.py` 第 49 行：

```python
KNOWN_SCOPES = frozenset({"read", "write", "delete", "ingest", "review", "manage", "admin"})
```

注释写着"服务实际校验的 scope 集合，签发集合外的 scope 等于什么都没给，因此 fail-closed"。

但 `mimir_v8/api.py` 第 1791、1801 行用了 `scoped("core_memory")`：

- `POST /v8/core-memory/promotions`
- `POST /v8/core-memory/items/{item_id}/retire`

后果：

1. `POST /v8/principals/register` 带 `scopes: ["core_memory"]` 会被 `mint_principal` 以 `invalid_scope` 拒绝（400）。
2. 于是 Core Memory 的写入口只能被 `admin` 使用（`Principal.require` 对 admin 直通），"给某个 Agent 单独授予 core memory 写权限"这件事**无法通过接口完成**。
3. `KNOWN_SCOPES` 作为"实际校验面"的不变量被打破——这正是那条注释要防的漂移。

## 复现

```bash
curl -X POST http://127.0.0.1:8456/v8/principals/register \
  -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"principal_id":"cursor","scopes":["read","core_memory"]}'
# 期望 201；实际 400 invalid_scope
```

## 修复

在 `KNOWN_SCOPES` 中加入 `"core_memory"`，并加一条测试：签发的 principal 能调用
`/v8/core-memory/promotions`（自 agent 或 admin），非 owner 且无该 scope 时 403。

## 顺带

`scripts/init.sh` 与 `docs/` 里没有提到该 scope；若决定不暴露，则应改为
`scoped("manage")` 并删除这条隐式 admin-only 语义，二选一，别留半截。
