#!/bin/bash
# Mímir capture-gap alert: run `worker capture-check` and, when any watched
# connector is stale, push the red card to Feishu. Runs under
# mimir-capture-check.service; env (PYTHONPATH, MIMIR_*) comes from the unit.
# Feishu credentials are read by feishu_send.py, same as daily_report_feishu.sh.
# Exit: 0 = all fresh, 1 = stale (card sent), other = check or delivery failed.
set -uo pipefail
MIMIR_HOME=${MIMIR_HOME:-$HOME/.hermes/mimir}
VENV=$MIMIR_HOME/venvs/current/bin/python3
OPS=$MIMIR_HOME/ops

OUT=$("$VENV" -m mimir_v8.worker capture-check --output feishu)
RC=$?
echo "$OUT"
if [ $RC -eq 0 ]; then
    echo "capture-check ok"
    exit 0
fi
if [ $RC -ne 1 ]; then
    echo "capture-check failed to run (rc=$RC)" >&2
    exit $RC
fi

echo "$OUT" | "$VENV" -c "
import json, sys
card = (json.load(sys.stdin).get('feishu_card') or {}).get('card')
if not card:
    sys.exit('stale but no feishu_card in capture-check output')
print(json.dumps(card, ensure_ascii=False))
" | "$VENV" "$OPS/feishu_send.py" -
SEND_RC=$?
if [ $SEND_RC -ne 0 ]; then
    echo "feishu delivery failed (rc=$SEND_RC)" >&2
    exit $SEND_RC
fi
echo "capture gap detected; alert delivered to feishu"
exit 1
