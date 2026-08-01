#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/ubuntu/attendance_bot"
LOG_FILE="$REPO_DIR/deploy.log"

echo "[$(date)] Post-receive hook triggered" >> "$LOG_FILE"

cd "$REPO_DIR"

echo "[$(date)] Pulling latest code..." >> "$LOG_FILE"
git pull origin main >> "$LOG_FILE" 2>&1

echo "[$(date)] Installing dependencies..." >> "$LOG_FILE"
source .venv/bin/activate
pip install -r requirements.txt -q >> "$LOG_FILE" 2>&1

echo "[$(date)] Restarting attendance-bot service..." >> "$LOG_FILE"
sudo systemctl restart attendance-bot >> "$LOG_FILE" 2>&1

echo "[$(date)] Deploy complete" >> "$LOG_FILE"