# AI Agent 入口（先读这里）

任何 agent（Thor、Odin、mirasim、Codex、Claude Code、Cursor……）在本仓库工作前，按顺序读：

1. **`docs/DEV-PROCESS.md`**：开发流程、测试分层、分支与 PR、部署验证门、禁止事项。**这是流程的唯一真相**
2. `docs/ops/RELEASE.md`（发版与部署）、`docs/ops/ROLES.md`（身份与权限）、`docs/INVARIANTS.md`（不变量）
3. 跨项目状态：`fleet-docs` 仓的 `STATUS.md`、`HANDOFF.md`、`DEPLOY_LOCK.md`

最常用的三条：

- 提交前跑 `scripts/test-gate.sh quick`；长任务用 `scripts/test-gate.sh full`（后台独立任务），不挂在会话下
- 不在生产目录 `~/.hermes/mimir` 改代码或跑测试；每个任务用独立 worktree `~/work/wt/mimir-<topic>`
- 改生产、合并、部署、重启、改令牌前，先列"改什么 + 影响 + 回滚点 + 怎么验证"，等先生授权

> 本仓库为公开仓：不得提交任何密钥、token、生产数据或用户数据。
