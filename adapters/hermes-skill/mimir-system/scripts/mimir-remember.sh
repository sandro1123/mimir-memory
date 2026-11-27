#!/bin/bash
# Mímir 写入客户端（供非 Hermes 外部 Agent 使用，如云端 Claude Code）
# 用法: mimir-remember <domain> <fact_type> "内容"
#   domain:    infrastructure | quant | tech_support | personal | system | knowledge
#   fact_type: event | user_pref | project_config | pattern | iron_rule | reference
#
# 部署位置: /usr/local/bin/mimir-remember (chmod 755)
# 依赖文件: ~/.hermes/mimir/secrets/clients/<principal>.token (chmod 600)；claudecode 已于 2026-11-24 退役
# 环境变量: MIMIR_HOST / MIMIR_PORT / MIMIR_TOKEN / MIMIR_PRINCIPAL 均可覆盖

set -euo pipefail

if [ $# -lt 3 ]; then
  echo "用法: mimir-remember <domain> <fact_type> \"内容\""
  echo "  domain:    infrastructure | quant | tech_support | personal | system | knowledge"
  echo "  fact_type: event | user_pref | project_config | pattern | iron_rule | reference"
  exit 1
fi

DOMAIN="$1"
FACT_TYPE="$2"
shift 2
CONTENT="$*"

MIMIR_HOST="${MIMIR_HOST:-127.0.0.1}"  # remote hosts must set MIMIR_HOST
MIMIR_PORT="${MIMIR_PORT:-8456}"
MIMIR_PRINCIPAL="${MIMIR_PRINCIPAL:-thor}"
TOKEN="${MIMIR_TOKEN:-$(cat "$HOME/.hermes/mimir/secrets/clients/$MIMIR_PRINCIPAL.token" 2>/dev/null || true)}"

if [ -z "$TOKEN" ]; then
  echo "错误: 找不到 Mímir token（~/.hermes/mimir/secrets/clients/$MIMIR_PRINCIPAL.token）" >&2
  exit 1
fi

python3 - "$MIMIR_HOST" "$MIMIR_PORT" "$TOKEN" "$MIMIR_PRINCIPAL" "$DOMAIN" "$FACT_TYPE" "$CONTENT" <<'PY'
import json, sys, urllib.request, urllib.error

host, port, token, principal, domain, fact_type, content = sys.argv[1:8]
payload = {
    "content": content,
    "owner_principal": principal,
    "domain": domain,
    "fact_type": fact_type,
    "visibility": "shared",
}
req = urllib.request.Request(
    f"http://{host}:{port}/v8/facts",
    data=json.dumps(payload).encode(),
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
        # 注意：projection_status=pending 是正常态，向量投影滞后于 canonical
        print(f"OK  fact_id={d.get('fact_id')}  version={d.get('version')}")
        print(f"    projection={d.get('projection_status')} (pending 时用 GET /v8/facts/<id> 验证)")
except urllib.error.HTTPError as e:
    body = e.read().decode()[:300]
    print(f"FAIL HTTP {e.code}: {body}", file=sys.stderr)
    if e.code == 422 and "invalid owner_principal" in body:
        print("    principal 未注册：在 mimir_config.yaml 的 federation.agents 追加后重启 mimir.service", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"FAIL 连接失败: {e}", file=sys.stderr)
    sys.exit(1)
PY
