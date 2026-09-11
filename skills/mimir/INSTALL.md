# Mímir for Claude Code — 安装指南

两件套：MCP server（工具层）+ Skill（使用知识层）。

## 1. MCP server（提供 27 个 mimir_* 工具）

```bash
# 设备上安装后（pip install -e . 或发布树 venv），二选一：

# 方式 A：本项目脚本入口
claude mcp add mimir -- /home/sandro1123/.hermes/mimir/venvs/current/bin/mimir-mcp

# 方式 B：模块直跑（无需 console script）
claude mcp add mimir -- /home/sandro1123/.hermes/mimir/venvs/current/bin/python3 -m mimir_v8.mcp
```

需要环境变量（Mímir API 地址与 token）：

```bash
claude mcp add mimir -e MIMIR_V8_URL=http://127.0.0.1:8456   -e MIMIR_V8_TOKEN_FILE=/home/sandro1123/.hermes/mimir/secrets/clients/claude.token   -- /home/sandro1123/.hermes/mimir/venvs/current/bin/mimir-mcp
```

## 2. Skill（教 Claude 何时/怎么用这些工具）

```bash
# 个人级（所有项目可用）
mkdir -p ~/.claude/skills
cp -r skills/mimir ~/.claude/skills/

# 或项目级（随仓走）
mkdir -p .claude/skills
cp -r skills/mimir .claude/skills/
```

装完在 Claude Code 里说「帮我记住……」或「你还记得……吗」，
skill 会自动触发对应工具。

## 验证

```
claude> 你有哪些 mimir 工具？
# 应列出 mimir_query / mimir_remember / ... 27 个
```
