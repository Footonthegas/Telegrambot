#!/usr/bin/env bash
set -euo pipefail

REMOTE_USER="ubuntu"
REMOTE_HOST="<your-server-ip>"
REMOTE_DIR="/home/ubuntu/attendance_bot"
BRANCH="main"

echo "=== Deploy attendance_bot to server ==="
echo ""

echo "[1/4] Pushing changes to remote..."
git push origin "$BRANCH"

echo "[2/4] Pulling latest code on server..."
ssh "${REMOTE_USER}@${REMOTE_HOST}" "cd ${REMOTE_DIR} && git pull origin ${BRANCH}"

echo "[3/4] Installing dependencies on server..."
ssh "${REMOTE_USER}@${REMOTE_HOST}" "cd ${REMOTE_DIR} && source .venv/bin/activate && pip install -r requirements.txt -q"

echo "[4/4] Restarting bot..."
ssh "${REMOTE_USER}@${REMOTE_HOST}" "sudo systemctl restart attendance-bot"

echo ""
echo "=== Deploy complete ==="
echo "The bot should be back online within 10 seconds."