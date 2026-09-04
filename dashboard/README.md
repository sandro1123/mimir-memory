# Mímir Dashboard · 看板

> A 13-tab web UI for Mímir — monitor memory, review candidates, manage skills, watch the CRDT federation.
> Mímir 的 13 标签页 Web 界面 —— 监控记忆、审核候选、管理技能、观察 CRDT 联邦。
> English · 中文双语

## What it is · 这是什么

A FastAPI + Alpine.js single-page dashboard that proxies Mímir's HTTP API and
renders a visual overview of the memory system.

一个 FastAPI + Alpine.js 单页看板，代理 Mímir 的 HTTP API，以可视化方式呈现
记忆系统的概览。

**13 tabs · 13 个标签页**：overview / pipeline / memory / review / sources /
agents / opinions / skills / insight / system / symbolic / codegraph / federation

**v3 (Mímir v14 适配) 新增 · New in v3**:
- **技能 skills** — AutoSkill 候选主题（成功 ≥3 次且零负反馈）、主题台账、
  已晋升 L3 技能列表，一键晋升审批 (`/api/skills`)
- **联邦 federation** — 跨节点 CRDT 联邦只读普查：节点注册表、事件账本、
  Lamport 水位 (`/api/federation`)
- **检索页投影预览** — 同一检索词在 claude/deepseek/local-small 三档模型下的
  注入块与预算占用对比 (`/api/projection`)

**v3.0.1 稳定性修刀 · Stability fixes in v3.0.1 (2026-09-04)**:
- 「活动」tab 之后 7 面板被吞 — 相邻两行完全相同的 grid 开标签，
  活动面板永不闭合
- 60s 周期性 Chart.js 崩溃（fullSize/RangeError）根治 — Chart 实例存
  Alpine reactive 状态会被深层 Proxy 化，打断 Chart.js 内部以 raw 实例
  为键的插件查找；实例搬至组件状态外 + upsert 复用。**框架级教训：
  重型第三方实例勿入 Alpine/Vue reactive 状态。**
- 黑板页改直查 v13 `blackboard.db`（此前 `/api/blackboard` 404）

## Production form · 生产部署形态

生产以 systemd 常驻（推荐）· production runs under systemd:

```ini
# /etc/systemd/system/mimir-dashboard.service
[Service]
WorkingDirectory=/home/<user>/mimir-dashboard
ExecStart=<venv>/bin/python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8800
Restart=always
RestartSec=10
```

> 若需局域网访问，`--host` 用 `0.0.0.0`（`127.0.0.1` 只听本机回环），
> 并在防火墙放行 8800（建议仅对内网段）。手工 `manage.sh`/nohup 启动的
> 进程不进 systemd——两套并存会在端口上 crash-loop，二选一。
> For LAN access bind `0.0.0.0` and open the firewall for 8800 (LAN
> segment only recommended). Do not run a manual nohup process alongside
> systemd — they crash-loop on the port. Pick one supervisor.

## Run · 运行

```bash
# 依赖 · deps
pip install -r requirements.txt

# 启动 · start (默认 8800)
./manage.sh start

# 访问 · open http://localhost:8800
```

Environment variables · 环境变量：

| Var | Default | Purpose 用途 |
|---|---|---|
| `MIMIR_API` | `http://127.0.0.1:8456` | Mímir API 地址 · Mímir API URL |
| `MIMIR_DATA_DIR` | `~/.hermes/mimir/data` | 数据目录 · data dir |
| `MIMIR_SECRETS_DIR` | `~/.hermes/mimir/secrets` | 密钥目录 · secrets dir |

## Docker

```bash
docker compose up -d
```

## Note · 说明

The dashboard reads the admin token from
`$MIMIR_SECRETS_DIR/clients/admin.token`. Run `../scripts/init.sh` first to
bootstrap tokens and config.

看板从 `$MIMIR_SECRETS_DIR/clients/admin.token` 读取 admin token。请先运行
`../scripts/init.sh` 初始化 token 与配置。
