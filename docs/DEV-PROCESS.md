# Mímir 开发流程（权威版）

- 版本：2026-11-26。适用于任何在本仓库工作的 agent 或人（Thor、Odin、mirasim、Codex、Claude Code……）
- 本文件是流程的唯一真相。别处（CLAUDE.md、记忆、聊天记录）与本文冲突时，以本文为准
- 发版与部署细节见 `docs/ops/RELEASE.md`；LLM 配置见 `docs/ops/MAGPIE_LLM.md`；身份与权限见 `docs/ops/ROLES.md`

---

## 0. 开工前必读（每次）

1. 读跨项目状态：`fleet-docs` 仓的 `STATUS.md`、`HANDOFF.md`（n100 上是 `~/work/docs/`）
2. 查 Mímir：`mimir -d system "<主题>"`，已有结论不重复排查
3. 确认没人占着部署：读 `fleet-docs/DEPLOY_LOCK.md`
4. 不确定的事实先查现场（git、systemd、`/health`、日志），不凭印象

## 1. 位置与边界

| 位置 | 用途 | 规则 |
|:--|:--|:--|
| GitHub `sandro1123/mimir-memory` master | 代码真相 | 只认合并且打 tag 的提交 |
| n100 `~/work/mimir` | 开发主仓 | 只作基线，不在上面直接改 |
| n100 `~/work/wt/mimir-<topic>` | 每个任务一个 worktree | 任务结束即删 |
| n100 `~/.hermes/mimir` | **生产** | 不改代码；数据目录 `v9/production-*` 不直接动；`releases/current` 和 `venvs/current` 只在部署时一起切 |

## 2. 主流程

1. **需求**：来自先生、审计、告警或 backlog。复杂改动先写方案到 `docs/plans/`：问题、范围与不做项、验收标准、风险与回滚、测试清单、是否涉及 schema 迁移
2. **先生确认方案**：没有确认不写实现
3. **开分支**：`git worktree add -b <type>/<topic> ~/work/wt/mimir-<topic> origin/master`；`type` ∈ feat/fix/docs/ops/test/chore
4. **测试先行**：先写能暴露问题的测试（RED），再写实现（GREEN）。每个 fix 至少一个回归测试
5. **本地门禁**：`scripts/test-gate.sh quick`（见第 3 节）
6. **独立审查**：由不参与编写的子代理或其他 agent 只读 diff 给意见；有阻断项就回到第 4 步
7. **PR**：推分支、开 PR，描述写清范围、测试结果、影响、是否动生产
8. **CI**：等 GitHub Actions 全绿（test 3.11/3.12、ruff、security、release-scan、version-sweep）。CI 不绿不合并
9. **合并 + tag**：semver `vX.Y.Z`，热修 `vX.Y.Z-hotfix.N`；更新 `CHANGELOG.md`
10. **部署**：按 `docs/ops/RELEASE.md` 建 release 目录和同名 venv。属于生产变更，先出四要素卡（改什么、影响、回滚点、怎么验证），等先生单次授权；在 `DEPLOY_LOCK.md` 登记后执行
11. **验证门**：见第 5 节。任一项不过立即回滚，再汇报
12. **收尾**：更新 `fleet-docs/STATUS.md`；非显而易见的根因写入 Mímir
13. **观察**：最多一到两次定时复查，不持续轮询

涉及 schema 迁移的改动：部署前先备份生产数据目录，回滚步骤写进四要素卡。

## 3. 测试分层（硬规则）

| 层 | 在哪跑 | 命令 | 用时 |
|:--|:--|:--|--:|
| 1 提交前 | n100 本地 | `scripts/test-gate.sh quick` | 1–3 分钟 |
| 2 PR | GitHub Actions | 自动 | 约 3–4 分钟 |
| 3 按需 | n100 后台 | `scripts/test-gate.sh full` | 视规模 |

- `quick` 跑 ruff、`release_scan` 和改动相关测试；动到 hooks、adapters、API 时，相关集成测试（`test_hooks_*`、`*_e2e`）一并跑
- **任何超过 10 分钟的命令不得挂在 agent 会话下**。用 `scripts/test-gate.sh full` 或 `systemd-run --user --collect` 起独立任务，结果写文件，跑完读一次
- CI 结果用 `gh run list -R sandro1123/mimir-memory -b <branch>` 或 `gh pr checks` 查

## 4. 分支、提交、PR

- 禁止直接提交 master；一个分支一个主题、同一时刻一个提交者
- Conventional Commits：`type(scope): summary`；一个提交一个逻辑
- 一个 PR 只做一件事，diff 超过约 800 行就拆
- 提交前：`git diff --check`；禁止提交 token、`.env`、`*.db`、chroma 数据、个人绝对路径（`release_scan` 会拦一部分，不能只靠它）
- 本仓库是公开仓：任何生产密钥、内部主机名以外的私密信息、用户数据都不得入库

## 5. 部署验证门

- `/health` 返回正确版本号和 schema
- 手动跑一次 `mimir-v9.2-governance` 和 `mimir-v12-extract`：exit 0，`llm_failures=0`，`llm_unavailable=0`
- `magpie_llm.env` 中 `MIMIR_EVALUATOR_MAX_TOKENS`、`MIMIR_GOVERNANCE_MAX_TOKENS` 保持 2048
- 回滚点持久化在 `releases/` 里，不放 `/tmp`

## 6. 禁止事项

- 未获先生明确授权：不合并、不部署、不重启服务、不改 systemd、不改令牌、不迁移 schema
- 一个动作的授权不扩展到其他动作
- governance 只走 Magpie 且必须有 fallback 链，不为省事改成直连
- 名字带 v8、v9.2、v12 的定时任务是现役的，不要按名字当成废弃删除
- 不把"测试通过"建立在没执行的命令或其他环境的结果上；失败和跳过如实报告
- 不输出或保存密钥、token、生产数据样本

## 7. 汇报

- 结论先行；需要先生拍板的事单独列出
- 写明实际状态：已推送/未推送、已合并/未合并、已部署/未部署
- 卡片排版按 Thor/Odin 的飞书卡片叙事标准
