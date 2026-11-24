#!/bin/bash
# Install the Mímir A-tier hook for Claude Code (1.9.0).
# Copies the versioned hook into ~/.claude/hooks/ (backing up any existing copy)
# and prints the settings.json hook block. It does NOT edit settings.json.
#
# Identity: set MIMIR_AGENT_ID (thor / odin / claudecode …) in the agent's
# environment, or rely on auto-detect (aiduCAT bridge => thor, else claudecode).
# Token:    ~/.hermes/mimir/secrets/clients/<agent>.token (mode 600).
set -euo pipefail
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

Add (or confirm) in ~/.claude/settings.json:
"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "$DST", "timeout": 15}]}],
  "Stop":         [{"hooks": [{"type": "command", "command": "$DST", "timeout": 30, "async": true}]}],
  "SessionEnd":   [{"hooks": [{"type": "command", "command": "$DST", "timeout": 30, "async": true}]}]
}
JSON
