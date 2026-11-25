# v7 `/delete` 只要 `write`，绕过 `delete` 分权

> **状态（2026-11-25）**：已修复，见 #71（1.9.2）。


**标签**：security, auth
**严重度**：Medium
**影响版本**：v7 兼容层存在起

## 问题

同一件事有两条删除路径，权限要求不同：

- `DELETE /v8/facts/{fact_id}`（api.py 1510）：`scoped("delete")`
- `POST /v8/facts/{fact_id}/tombstone`（api.py 1498）：`scoped("delete")`
- `POST /v8/learning/forget`（api.py 1590）：`scoped("delete")`
- **`POST /delete`（api.py 2528，v7 兼容）：`scoped("write")`**

`/delete` 随后调用 `require_fact_permission(identity, fact, "delete")`，而
`store.can_access` 对自己拥有的事实 `owner_default` 放行 `delete`。也就是说：
**任何带 `write` 的 token 都能删掉自己名下的事实**，而 `delete` 这个独立 scope 形同虚设。

`mint_principal` 的默认 scope 正是 `("read", "write")`（auth.py:109），所以刚 onboarding
的 Agent 天然具备删除能力。

## 影响

- "删除是比写入更高一级的授权"这一模型被老接口绕过。
- 审计上删除了谁、用什么授权，都合法，看不出越权。

## 修复

`/delete` 改为 `scoped("delete")`。若担心破坏现有 v7 客户端，退一步也应：

- 在 `mint_principal` 里把默认 scope 收紧为 `("read",)`，让写与删都显式授予；
- 或对 v7 路由加一条"仅当 token 显式带 `write` 且当前事实 owner 匹配"的窄例外，并在文档写明。

## 验收

- 仅 `read+write` 的 token 调 `/delete` → 403 `missing_scope`。
- `read+write+delete` 的 token 调 `/delete` → 200（保持 v7 客户端可用）。
- `tests/test_p32_delete_route.py` 扩一条覆盖 v7 路径的 scope 断言。
