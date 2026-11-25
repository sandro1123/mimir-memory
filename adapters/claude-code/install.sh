#!/bin/bash
# Install the Mímir A-tier hook for Claude Code (1.9.1).
# Copies the versioned hook into ~/.claude/hooks/ (backing up any existing copy)
# and prints the settings.json hook block. It does NOT edit settings.json.
#
# Identity: MIMIR_AGENT_ID is REQUIRED on every host (1.9.1, docs/ops/ROLES.md).
#   n100 (Thor)   -> MIMIR_AGENT_ID=thor
#   sanjose (Odin) -> MIMIR_AGENT_ID=odin
# The hook never guesses an identity and never defaults to thor. If the variable
# is unset, the hook logs an error and does nothing: no network calls, exit 0.
# Token:    ~/.hermes/mimir/secrets/clients/<agent>.token (mode 600).
set -euo pipefail
: "${MIMIR_AGENT_ID:?set MIMIR_AGENT_ID=thor (n100) or odin (sanjose) before installing}"
SRC="$(cd "$(dirname "$0")" && pwd)/mimir_auto_capture.py"
DST_DIR="$HOME/.claude/hooks"
DST="$DST_DIR/mimir_auto_capture.py"
mkdir -p "$DST_DIR"
if [ -f "$DST" ] && ! cmp -s "$SRC" "$DST"; then
  cp -p "$DST" "$DST.bak-$(date +%Y%m%d_%H%M%S)"
  echo "backed up existing hook"
fi
install -m 755 "$SRC" "$DST"
python3 -m py_compile "$DST"
echo "installed: $DST"
cat <<JSON

Add (or confirm) in ~/.claude/settings.json. The MIMIR_AGENT_ID prefix is required:
"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "MIMIR_AGENT_ID=$MIMIR_AGENT_ID $DST", "timeout": 15}]}],
  "Stop":         [{"hooks": [{"type": "command", "command": "MIMIR_AGENT_ID=$MIMIR_AGENT_ID $DST", "timeout": 30, "async": true}]}],
  "SessionEnd":   [{"hooks": [{"type": "command", "command": "MIMIR_AGENT_ID=$MIMIR_AGENT_ID $DST", "timeout": 30, "async": true}]}]
}
JSON
