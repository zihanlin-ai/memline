#!/usr/bin/env bash
# Start/stop/status for the local qdrant server backing memline.
# WSL has no systemd, so run `qdrantctl.sh start` after a WSL restart
# (before any memline command).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/bin/qdrant"
CFG="$DIR/config.yaml"
PIDFILE="$DIR/qdrant.pid"
LOG="$DIR/qdrant.log"
URL="http://127.0.0.1:6333"

running_pid() {
    pgrep -x qdrant | head -1 || true
}

case "${1:-status}" in
start)
    pid="$(running_pid)"
    if [ -n "$pid" ]; then
        echo "already running (pid $pid)"
        exit 0
    fi
    nohup "$BIN" --config-path "$CFG" >> "$LOG" 2>&1 &
    echo "pid=$!" > "$PIDFILE"
    for _ in $(seq 1 30); do
        if curl -sf "$URL/readyz" > /dev/null 2>&1; then
            echo "started (pid $!)"
            exit 0
        fi
        sleep 1
    done
    echo "failed to become ready within 30s; see $LOG" >&2
    exit 1
    ;;
stop)
    pid="$(running_pid)"
    if [ -z "$pid" ]; then
        echo "not running"
        exit 0
    fi
    kill "$pid"
    for _ in $(seq 1 15); do
        kill -0 "$pid" 2>/dev/null || { echo "stopped"; exit 0; }
        sleep 1
    done
    echo "still alive after 15s; not forcing. Inspect pid $pid manually." >&2
    exit 1
    ;;
status)
    pid="$(running_pid)"
    if [ -n "$pid" ] && curl -sf "$URL/readyz" > /dev/null 2>&1; then
        echo "running (pid $pid), ready"
    elif [ -n "$pid" ]; then
        echo "process up (pid $pid) but not ready"
        exit 1
    else
        echo "not running"
        exit 1
    fi
    ;;
*)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
