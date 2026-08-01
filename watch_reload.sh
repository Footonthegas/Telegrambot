#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="/tmp/attendance-bot-watcher.pid"
LOG_FILE="$SCRIPT_DIR/watcher.log"

echo "=== Attendance Bot — File Watcher ==="
echo "Watching for file changes to auto-reload the bot..."
echo "Press Ctrl+C to stop."

echo $$ > "$PID_FILE"

stop_bot() {
    echo "[$(date)] Stopping bot..." >> "$LOG_FILE"
    sudo systemctl stop attendance-bot 2>/dev/null || true
    pkill -f "python3 telegram_bot.py" 2>/dev/null || true
    sleep 2
}

start_bot() {
    echo "[$(date)] Starting bot..." >> "$LOG_FILE"
    sudo systemctl start attendance-bot 2>/dev/null || true
}

restart_bot() {
    stop_bot
    sleep 1
    start_bot
    echo "[$(date)] Bot restarted after file change." >> "$LOG_FILE"
}

cleanup() {
    echo "[$(date)] Watcher stopped." >> "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 0
}

trap cleanup SIGINT SIGTERM

last_md5=""

while true; do
    current_md5=$(find "$SCRIPT_DIR" -name "*.py" -not -path "*/.venv/*" -not -path "*/__pycache__/*" -exec md5sum {} \; 2>/dev/null | sort | md5sum | awk '{print $1}')

    if [ -n "$last_md5" ] && [ "$current_md5" != "$last_md5" ]; then
        echo "[$(date)] Detected file changes. Reloading bot..." >> "$LOG_FILE"
        restart_bot
    fi

    last_md5="$current_md5"
    sleep 5
done