# 可撤销最后一个 admin；注册表读改写有并发丢更新

**标签**：bug, auth, ops
**严重度**：Medium
**影响版本**：1.7.0（#30 落地后）

## 问题一：只防了"删空"，没防"删光管理员"

`mimir_v8/auth.py` 的 `revoke_principal`（132–156）只在 `remaining` 为空时拒绝：

```python
if not remaining:
    raise AuthError("cannot revoke the last principal ...", 409, "last_principal")
```

但如果还留着若干非 admin 主体，撤销最后一个 admin 会被放行。此后所有 admin 路由
——`/v8/principals/register`、`/revoke`、`/v8/facts/{id}/grants`、`/v8/projectors`、
`/v9/knowledge/status`——一律 403，而 `/health`、`/ready` 仍然 ok，监控看不出异常。
恢复只能上机器改 `tokens.json`。

## 问题二：注册表读改写没有跨进程锁

`mint_principal` 与 `revoke_principal` 都是"读整个 JSON → 改 → `atomic_write_registry`"。
原子写只保证不出现半截文件，**不保证不丢更新**：两个并发签发请求各自基于同一份旧表，
后写者覆盖前者记录，而前者的 token 已经返回给调用方了——该 token 永久无效且无痕迹。

在多 worker / 多进程部署下这条是真的会发生（`TokenStore` 是每进程热加载，写侧没有互斥）。

## 问题三：principal id 规范化不一致

- 写侧 `mint_principal`：`str(principal_id).strip().lower()`，并用 `PRINCIPAL_ID_RE` 校验。
- 读侧 `TokenStore._load`（200–211）：`str(item.get("id", "")).strip()`，**不小写、不校验字符集**。

手改过的注册表可以同时存在 `Mentor` 与 `mentor`，于是跨 Agent 共享、别名继承
（`PRINCIPAL_ALIASES`）与 ACL 判定各认一套。这不是假想：运维大规模改注册表是常见操作。

## 修复

1. `revoke_principal` 增加"剩余主体中至少一个 admin"的守卫，否则 409 并在消息里说明。
2. 写侧加跨进程互斥：`tokens.json.lock` + `fcntl.flock`（或写前重读并校验 `version` 字段，乐观并发），冲突时报 409 而不是静默覆盖。
3. 读侧对 id 做与写侧相同的规范化；发现非规范 id 时 **fail-closed**（与文件里其它非法字段一致），而不是静默接受。

## 验收

- 撤销最后一个 admin → 409，且 registry 不变。
- 并发 10 个签发 → 10 个主体全部存在、token 全部可用（或明确的 409）。
- 注册表里出现 `Mentor` → `TokenStore.validate()` 报 `auth_unavailable`，不静默放行。
