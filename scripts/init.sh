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


def _mint(name):
    # 32 字节随机 → URL-safe base64；明文写给 client，registry 只存 sha256
    token = secrets.token_urlsafe(32)
    with open(os.path.join(clients_dir, f"{name}.token"), "w") as f:
        f.write(token + "\n")
    os.chmod(os.path.join(clients_dir, f"{name}.token"), 0o600)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


for agent in agents:
    principals.append({"id": agent, "token_sha256": _mint(agent), "scopes": ["read", "write"], "admin": False})
# 独立 admin 主体 / dedicated admin principal —— dashboard、Hermes 插件回退、运维脚本
# 都读 clients/admin.token（audit 2026-09-07: 此前 init 从不生成它，三处消费者全部落空）。
principals.append({
    "id": "admin", "token_sha256": _mint("admin"),
    "scopes": ["read", "write", "delete", "ingest", "review", "manage", "admin"], "admin": True,
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
# Mímir config —— 只有下面两段会被代码读取 / only these sections are read by the code
# (audit 2026-09-07: 旧模板的 agents/subscriptions/water_level 从未被任何代码消费)
#   federation.agents / federation.domains  → 动态主体与领域注册表 (mimir_v8/config.py)
#   collector.rss_feeds / collector.sources → 统一采集源注册表 (mimir_v8/worker.py)
version: 14.1.0
federation:
  agents: [heimdallr, quantmaster, jarvis, mentor]
  domains: [infrastructure, quant, tech_support, personal, system, knowledge]
collector:
  rss_feeds: []
  sources: []
  # 示例 / examples:
  # - {name: vault, type: vault, vault_root: /home/me/obsidian, exclude_dirs: [private], category: knowledge_doc}
  # - {name: blog, type: web, url: https://example.com/post}
YAML
  echo "    已写入 / wrote $CONFIG_FILE"
fi

# ── 3.5 嵌入模型预取 / embedding model prefetch ──────────────
# runtime.py 以 local_files_only=True + HF_HUB_OFFLINE=1 加载 bge-m3（生
# 产纪律：运行时永不联网下载）。首装必须在此显式预取——否则首次嵌入
# 调用即抛「模型不在本地缓存」。预取完成后再交回离线纪律。
if [ "${SKIP_MODEL_PREFETCH:-0}" != "1" ]; then
  MODEL_NAME="${MIMIR_V8_MODEL:-BAAI/bge-m3}"
  echo "==> 预取嵌入模型 / prefetching embedding model: $MODEL_NAME"
  echo "    （约 2.3G；SKIP_MODEL_PREFETCH=1 可跳过，模型已缓存时推荐跳过）"
  python3 - "$MODEL_NAME" <<'PYEOF_INNER'
import sys
try:
    from sentence_transformers import SentenceTransformer
    model = sys.argv[1]
    SentenceTransformer(model, device="cpu")  # 联网下载并写入 HF 缓存
    print(f"    model cached: {model}")
except ImportError:
    print("    !! sentence-transformers 未安装，跳过预取（先 pip install -e .[embeddings]）")
    sys.exit(0)
PYEOF_INNER
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
