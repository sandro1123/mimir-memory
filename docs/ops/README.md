# Mimir Ops（仓内协同入口）

- 跨项目运维骨架与模板：`~/work/ops/`（权威模板入口）
- SSOT/发布-部署映射：`~/work/docs/SSOT_AND_RELEASE_MAP.md`（§3 Mimir）
- 盘点基线：`~/work/docs/MIMIR_SSOT_INVENTORY_2026-11-19.md`

## 本目录用途
- 存放 Mimir 项目特定的运维说明：服务/timer 拓扑、`releases/current` 与 venv/data-dir/token 路径约定（不含密钥值）、release 切换与回滚补充、排障记录。
- 通用流程优先复用 `~/work/ops/` 模板；项目差异在此补充并回链。

## 当前关键点（2026-11-19）
- SSOT（开发）：`~/work/mimir`（GitHub `sandro1123/mimir-memory` `master`，`v1.7.0` 基线；注意本地 EOL 伪修改待清理）
- 1.8.0 候选：`feat/1.8.0@6cacd56`（`~/mimir-1.8.0-dev` / GitHub）
- 生产运行/部署：`~/.hermes/mimir`，`releases/current` 与 `venvs/current` 均指向 `1.9.2-20261125_1215`（两者同名、一起切换，见 RELEASE.md）；`mimir.service`/`mimir-dashboard.service` 及多 timer/worker 运行中
- 待处置（需方案+确认）：systemd unit/drop-in 变更未 reload；生产根仓脏改/无 remote 的治理与分流
