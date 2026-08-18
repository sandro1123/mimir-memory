#!/usr/bin/env bash
# Mímir 一键开箱即用 · One-shot bootstrap & run
#
# 一条命令完成：装依赖 → 初始化 → 启动服务
# One command: install deps → init → start server
#
# 用法 / Usage:
#   ./bootstrap.sh            # 安装并前台启动 / install & run in foreground
#   ./bootstrap.sh --bg       # 安装并后台启动 / install & run in background
#   ./bootstrap.sh --help     # 帮助 / help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 默认路径 / default paths
MIMIR_HOME="${MIMIR_HOME:-$HOME/.hermes/mimir}"
DATA_DIR="${MIMIR_DATA_DIR:-$MIMIR_HOME/data}"
SECRETS_DIR="${MIMIR_SECRETS_DIR:-$MIMIR_HOME/secrets}"
PORT="${MIMIR_PORT:-8456}"

MODE="foreground"
if [[ "${1:-}" == "--bg" ]]; then
  MODE="background"
elif [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'HELP'
Mímir 一键开箱即用 / one-shot bootstrap & run

用法 / usage:
  ./bootstrap.sh          安装依赖 + 初始化 + 前台启动
  ./bootstrap.sh --bg     安装依赖 + 初始化 + 后台启动

环境变量 / env:
  MIMIR_HOME        主目录 (default ~/.hermes/mimir)
  MIMIR_PORT        服务端口 (default 8456)
  MIMIR_SKIP_DEPS=1 跳过依赖安装（已装过时）

流程 / steps:
  1. 检查 Python 3.11+ / check python
  2. pip install -e ".[embeddings]"  (首次需下载 bge-m3 模型)
  3. scripts/init.sh  生成目录 + agent token + config
  4. 启动 server
HELP
  exit 0
fi

echo "═══════════════════════════════════════════"
echo "  Mímir 一键开箱即用 · One-shot bootstrap"
echo "═══════════════════════════════════════════"
echo ""

# ── 1. 检查 Python / check python ────────────────────────
echo "==> [1/4] 检查 Python ..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未找到 python3，请先安装 Python 3.11+" >&2
  exit 1
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "    Python $PY_VER ✓"
if [[ ! "$PY_VER" =~ ^3\.(1[1-9]|[2-9][0-9])$ ]]; then
  echo "⚠️  需要 Python 3.11+，当前 $PY_VER" >&2
  exit 1
fi

# ── 2. 安装依赖 / install deps ───────────────────────────
if [[ "${MIMIR_SKIP_DEPS:-0}" == "1" ]]; then
  echo "==> [2/4] 跳过依赖安装 (MIMIR_SKIP_DEPS=1)"
else
  echo "==> [2/4] 安装依赖（首次需下载 bge-m3 嵌入模型，可能较慢）..."
  pip install -e "$SCRIPT_DIR[embeddings]" --quiet
  echo "    依赖安装完成 ✓"
fi

# ── 3. 初始化 / init ─────────────────────────────────────
echo "==> [3/4] 初始化目录 + agent token + config ..."
export MIMIR_HOME MIMIR_DATA_DIR MIMIR_SECRETS_DIR
bash "$SCRIPT_DIR/scripts/init.sh"

# ── 4. 启动 / start ──────────────────────────────────────
echo ""
echo "==> [4/4] 启动服务 ..."
TOKEN_FILE="$SECRETS_DIR/api_tokens.json"

if [[ "$MODE" == "background" ]]; then
  nohup python3 -m mimir_v8.server \
    --bind 127.0.0.1 --port "$PORT" \
    --data-dir "$DATA_DIR" \
    --token-file "$TOKEN_FILE" \
    > "$MIMIR_HOME/server.log" 2>&1 &
  echo "    已后台启动 (PID $!)，日志: $MIMIR_HOME/server.log"
  echo ""
  echo "    ✅ 服务运行中，测试: curl http://127.0.0.1:$PORT/health"
else
  echo "    前台启动，Ctrl+C 停止 ..."
  echo ""
  python3 -m mimir_v8.server \
    --bind 127.0.0.1 --port "$PORT" \
    --data-dir "$DATA_DIR" \
    --token-file "$TOKEN_FILE"
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  下一步 / next steps:"
echo "    - 单智能体上手: examples/QUICKSTART.md"
echo "    - 多智能体联邦: docs/FEDERATION.md"
echo "    - 看板: dashboard/ (cd dashboard && ./manage.sh start)"
echo "═══════════════════════════════════════════"
