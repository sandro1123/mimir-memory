#!/usr/bin/env bash
# Mímir 测试门禁（流程见 docs/DEV-PROCESS.md 第 3 节）
#
#   scripts/test-gate.sh quick    提交前：ruff + release_scan + 改动相关测试（动到 hooks/adapters/api 时加跑集成测试）
#   scripts/test-gate.sh full     全量 tests/，作为独立 systemd 任务后台跑，不挂在会话下
#   scripts/test-gate.sh status   读最近一次 full 的结果
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
BASE="${TEST_GATE_BASE:-origin/master}"
PY="${TEST_GATE_PY:-python3}"
LOG_DIR="${TEST_GATE_LOG_DIR:-$HOME/.cache/mimir-test-gate}"
mkdir -p "$LOG_DIR"

case "$ROOT" in
  "$HOME/.hermes/mimir"*) echo "拒绝在生产目录 ~/.hermes/mimir 跑测试（DEV-PROCESS 第 6 节）" >&2; exit 2 ;;
esac

PYTEST=("$PY" -m pytest -q -p no:cacheprovider)

changed_tests() {
  git fetch -q origin 2>/dev/null || true
  local files
  files=$( { git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --name-only --cached; } | sort -u)
  {
    grep -E '^tests/.*test_.*\.py$' <<<"$files" || true
    grep -E '\.py$' <<<"$files" | grep -v '^tests/' | while read -r f; do
      m=$(basename "$f" .py)
      [ "$m" = "__init__" ] && continue
      grep -rl --include='test_*.py' -E "\b${m}\b" tests 2>/dev/null || true
    done
    # 动到 hooks / adapters / api 时，加跑集成测试
    if grep -q -E '^(adapters/|mimir_v8/(api|hooks)|hermes-plugin/)' <<<"$files"; then
      ls tests/test_hooks_*.py tests/*_e2e.py 2>/dev/null || true
    fi
  } | sort -u | while read -r t; do [ -f "$t" ] && echo "$t"; done
}

case "${1:-quick}" in
  quick)
    echo "== ruff mimir_v8/"; "$PY" -m ruff check mimir_v8/ --quiet 2>/dev/null || ruff check mimir_v8/ --quiet
    echo "== release_scan"; "$PY" scripts/release_scan.py
    mapfile -t T < <(changed_tests)
    echo "== tests（改动相关 ${#T[@]} 个文件）"
    if [ "${#T[@]}" -gt 0 ]; then
      nice -n 10 timeout 900 "${PYTEST[@]}" "${T[@]}"
    else
      echo "无改动相关的测试"
    fi
    echo "quick gate: PASS"
    ;;
  full)
    unit="mimir-test-gate-$(date +%Y%m%d_%H%M%S)"
    log="$LOG_DIR/$unit.log"
    systemd-run --user --collect --unit="$unit" -p WorkingDirectory="$ROOT" -p Nice=10 \
      /bin/bash -c "timeout 3000 ${PYTEST[*]} tests/ > '$log' 2>&1; echo exit=\$? >> '$log'"
    ln -sfn "$log" "$LOG_DIR/latest.log"
    echo "已在后台启动 $unit，日志 $log"
    echo "查看结果：scripts/test-gate.sh status（跑完前不要反复查）"
    ;;
  status)
    log="$LOG_DIR/latest.log"
    [ -f "$log" ] || { echo "没有 full 记录"; exit 1; }
    if grep -q '^exit=' "$log"; then
      grep -E '^FAILED|^ERROR|passed|failed|^exit=' "$log" | tail -30
    else
      echo "仍在运行：$(readlink -f "$log")"; tail -c 200 "$log"; echo
    fi
    ;;
  *)
    echo "用法：$0 {quick|full|status}" >&2; exit 2 ;;
esac
