#!/usr/bin/env bash
# Install Mímir MemoryProvider plugin into Hermes (flat directory layout).
#
# The Hermes plugin loader scans $HERMES_HOME/plugins/<name>/ for a directory
# whose __init__.py registers a MemoryProvider, then the plugin is activated
# via the memory.provider config key. A single-file copy does NOT work.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)/mimir_memory_provider"
HERMES_PLUGIN_DIR="${HERMES_PLUGIN_DIR:-$HOME/.hermes/plugins}"

if [ ! -f "$PLUGIN_DIR/__init__.py" ]; then
  echo "❌ plugin source not found: $PLUGIN_DIR" >&2
  exit 1
fi

mkdir -p "$HERMES_PLUGIN_DIR/mimir_memory_provider"
cp "$PLUGIN_DIR"/*.py "$PLUGIN_DIR"/plugin.yaml "$HERMES_PLUGIN_DIR/mimir_memory_provider/"
chmod 644 "$HERMES_PLUGIN_DIR"/mimir_memory_provider/*

echo "✅ Mímir MemoryProvider installed to $HERMES_PLUGIN_DIR/mimir_memory_provider/"
echo "ℹ️  Configure Hermes: memory.provider=mimir_memory_provider"
echo "ℹ️  (add 'mimir_memory_provider' to plugins.enabled, then restart the gateway)"
