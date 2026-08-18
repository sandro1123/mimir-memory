# Mímir Dashboard · 看板

> A 9-tab web UI for Mímir — monitor memory, review candidates, manage opinions.
> Mímir 的 9 标签页 Web 界面 —— 监控记忆、审核候选、管理意见。
> English · 中文双语

## What it is · 这是什么

A FastAPI + Alpine.js single-page dashboard that proxies Mímir's HTTP API and
renders a visual overview of the memory system.

一个 FastAPI + Alpine.js 单页看板，代理 Mímir 的 HTTP API，以可视化方式呈现
记忆系统的概览。

**9 tabs · 9 个标签页**：overview / memory / review / sources / agents /
opinions / system / symbolic / codegraph

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
