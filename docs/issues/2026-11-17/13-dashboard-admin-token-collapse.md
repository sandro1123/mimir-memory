# Dashboard 用 admin token 打所有写操作，密码即全权管理员

**标签**：security, dashboard, design
**严重度**：Medium（部署内网时更接近 High）
**影响版本**：Dashboard v4/v4.1

## 问题

`dashboard/backend/main.py`：

- 87–91 行：`DASHBOARD_TOKEN` 默认读 `admin.token`（即 **admin 主体**的明文 token）。
- 254–261 行：中间件对 `/api/*`、`/v1/*` 只要求「session cookie 有效」或「bearer == DASHBOARD_TOKEN」。
- 写/审批类路由（`@app.post`，逐条核实于 2026-11-17 的 `main.py`）：`/api/candidates/{id}/review`、
  `/api/candidates/{id}/commit`、`/api/conflicts/{id}/resolve|dismiss`、`/api/crystals/scan`、
  `/api/crystals/{id}/approve|dismiss`、`/api/observations/consolidate`、`/api/governance/run`、
  `/api/knowledge/create|delete`、`/api/source/add`、`/api/skills/promote`、`/api/projection`，
  以及 `/api/search/trace`、`/api/query/trace` 这两条带查询副作用的 POST。
  共 50 条 `@app` 装饰器里，写类约 15 条（上一版"44 个里 12 个"的计数不准确，已更正）。

这些路由转发给 MIMIR API 时携带的都是那把 admin token，所以：

1. **密码等价于 admin**：知道 dashboard 密码（或拿到 cookie）的人，可以做任何 admin 动作——
   但 MIMIR 那边的 scope 模型（`review` / `manage` / `delete` / `admin` 分开）在 dashboard 面
   被抹平了。
2. **审计失真**：所有操作在 MIMIR 侧都记成 `admin`，看不出是哪个看板用户、哪个人工审批。
   这与「治理管线必须可审计」的判词冲突。
3. **直连数据库**：dashboard 还以 `CANONICAL_DB` 直读 `canonical.db`（`_db_query`），
   绕过 API 的 ACL。它自己是单用户工具时问题不大，一旦被多人访问就是全库只读泄露。
4. 附加风险：compose 把 `./dashboard/backend` 挂进镜像，跑的不是构建进去的代码；
   登录限流是进程内字典（多 worker 各算 10 次）。

## 修复建议

1. 给 dashboard 单独签发一个**非 admin** principal（`read` + `review` + `manage`，
   视需要加 `write`），只把 dashboard 真正需要的路由放开；`/v8/principals/*`、
   `/v8/facts/{id}/grants` 等 admin-only 路由应显式拒绝。
2. 审批类动作携带**操作者身份**（dashboard 用户 id）写进 `audit_log`，而不是统一 `admin`。
3. 读路径尽量走 API（`/v8/query`、`/api/aggregates`），确需直读 DB 时以只读 URI 打开并记录。
4. 登录限流改为按账号 + IP，且落共享存储（或至少用 `--workers 1` 明确约束）。

## 验收

- dashboard 以非 admin token 启动时，12 个写路由仍可用，`POST /v8/principals/register` 被拒。
- 审批一次后 `audit_log` 里 actor 是 dashboard 用户名，不是 `admin`。
- 手工测试记录写进 PR 描述（这条没有自动化 CI 覆盖）。
