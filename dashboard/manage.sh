#!/usr/bin/env bash
# Mímir Dashboard 管理脚本
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/dashboard.pid"
LOGFILE="$DIR/dashboard.log"

case "${1:-status}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "⚠️  Dashboard 已在运行 (PID: $(cat "$PIDFILE"))"
      exit 0
    fi
    cd "$DIR"
    nohup python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8800 >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "✅ Dashboard 已启动 (PID: $!)"
    ;;
  stop)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")"
      rm -f "$PIDFILE"
      echo "✅ Dashboard 已停止"
    else
      echo "⚠️  Dashboard 未在运行"
    fi
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "✅ Dashboard 运行中 (PID: $(cat "$PIDFILE"))"
      echo "   端口: 8800"
      echo "   访问: http://localhost:8800"
    else
      echo "⚠️  Dashboard 未运行"
    fi
    ;;
  logs)
    tail -f "$LOGFILE"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac