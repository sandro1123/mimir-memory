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
