#!/usr/bin/env bash
# Mímir one-shot bootstrap — 一键初始化脚本
#
# 用途 / Purpose:
#   为新部署生成目录结构、智能体 token、以及最小可运行配置。
#   Bootstrap a fresh deployment: directory layout, agent tokens, minimal config.
#
# 用法 / Usage:
#   ./scripts/init.sh            # 交互式 / interactive
#   MIMIR_HOME=/data/mimir ./scripts/init.sh   # 指定主目录 / custom home

set -euo pipefail

# ── 默认路径 / default paths ──────────────────────────────
MIMIR_HOME="${MIMIR_HOME:-$HOME/.hermes/mimir}"
DATA_DIR="${MIMIR_DATA_DIR:-$MIMIR_HOME/data}"
SECRETS_DIR="${MIMIR_SECRETS_DIR:-$MIMIR_HOME/secrets}"
CLIENTS_DIR="$SECRETS_DIR/clients"
CONFIG_FILE="${MIMIR_CONFIG_FILE:-$MIMIR_HOME/mimir_config.yaml}"

# 默认智能体 / default agents (edit here to add/remove)
AGENTS=("heimdallr" "quantmaster" "jarvis" "mentor")

# ── 帮助 / help ──────────────────────────────────────────
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Mímir init — 一键初始化 / one-shot bootstrap"
  echo "  生成目录 + agent token + 最小 config"
  echo "环境变量 / env vars:"
  echo "  MIMIR_HOME        主目录 (default: ~/.hermes/mimir)"
  echo "  MIMIR_DATA_DIR    数据目录 (default: \$MIMIR_HOME/data)"
  echo "  MIMIR_SECRETS_DIR 密钥目录 (default: \$MIMIR_HOME/secrets)"
  exit 0
fi

echo "==> Mímir init"
echo "    MIMIR_HOME    = $MIMIR_HOME"
echo "    DATA_DIR      = $DATA_DIR"
echo "    SECRETS_DIR   = $SECRETS_DIR"

# ── 1. 创建目录 / create directories ─────────────────────
mkdir -p "$DATA_DIR" "$CLIENTS_DIR"
chmod 700 "$SECRETS_DIR" 2>/dev/null || true

# ── 2. 生成 token / generate tokens ──────────────────────
TOKEN_JSON="$SECRETS_DIR/api_tokens.json"

if [[ -f "$TOKEN_JSON" ]]; then
  echo "==> 跳过 token 生成（已存在 $TOKEN_JSON）"
else
  echo "==> 生成 agent token ..."
  # 用 python 生成随机 token 与 sha256（避免依赖 openssl 输出差异）
  python3 - "${AGENTS[*]}" "$TOKEN_JSON" "$CLIENTS_DIR" <<'PY'
import json, os, secrets, hashlib, sys

agents = sys.argv[1].split()
token_json = sys.argv[2]
clients_dir = sys.argv[3]

principals = []
for i, agent in enumerate(agents):
    # 32 字节随机 → URL-safe base64
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    # 写明文 token 给 client 使用 / write plaintext for clients
    with open(os.path.join(clients_dir, f"{agent}.token"), "w") as f:
        f.write(token + "\n")
    os.chmod(os.path.join(clients_dir, f"{agent}.token"), 0o600)
    principals.append({
        "id": agent,
        "token_sha256": digest,
        "scopes": ["read", "write"] + (["review", "delete", "ingest", "manage"] if i == 0 else []),
        "admin": (i == 0),  # 第一个 agent 作为 admin / first agent is admin
    })

registry = {"version": 1, "principals": principals}
with open(token_json, "w") as f:
    json.dump(registry, f, indent=2)
os.chmod(token_json, 0o600)
print(f"    已写入 / wrote {token_json}")
print(f"    明文 token 在 / plaintext tokens in {clients_dir}/")
PY
fi

# ── 3. 生成最小 config / minimal config ──────────────────
if [[ -f "$CONFIG_FILE" ]]; then
  echo "==> 跳过 config 生成（已存在 $CONFIG_FILE）"
else
  echo "==> 生成最小 config ..."
  cat > "$CONFIG_FILE" <<'YAML'
version: 7.0.0
schema_version: '7'
agents:
- id: heimdallr
  name: Heimdallr-EX
  role: 基础设施/综合助手
  subscriptions:
    domains: [infrastructure, system, personal, quant, knowledge]
    types: [all]
- id: quantmaster
  name: QuantMaster
  role: 量化投顾
  subscriptions:
    domains: [quant, system, knowledge]
    types: [iron_rule, pattern, project_config]
- id: jarvis
  name: J.A.R.V.I.S.
  role: 技术顾问
  subscriptions:
    domains: [system, tech_support, infrastructure, knowledge]
    types: [project_config, pattern, iron_rule]
- id: mentor
  name: Mentor
  role: 培训师/记忆守护者
  maintainer: true
  subscriptions:
    domains: [system, knowledge, infrastructure, personal, quant]
    types: [all]
domains: [infrastructure, quant, tech_support, personal, system, knowledge]
fact_types: [iron_rule, user_pref, project_config, event, pattern, ephemeral, learning, reference]
visibility: [all, owner_only, shared]
water_level:
  per_agent_warn: 200
  per_agent_force: 300
  total_facts_warn: 500
  total_facts_force: 800
YAML
  echo "    已写入 / wrote $CONFIG_FILE"
fi

# ── 4. 收尾 / wrap up ────────────────────────────────────
echo ""
echo "==> 完成 / Done."
echo "    启动服务 / start server:"
echo "      python -m mimir_v8.server --bind 127.0.0.1 --port 8456 \\"
echo "        --data-dir $DATA_DIR \\"
echo "        --token-file $TOKEN_JSON"
echo ""
echo "    每个 agent 用明文 token 连接 / each agent connects with its plaintext token:"
for a in "${AGENTS[@]}"; do
  echo "      $a -> $CLIENTS_DIR/$a.token"
done
echo ""
echo "    多智能体联邦接入指南 / multi-agent federation guide: docs/FEDERATION.md"
