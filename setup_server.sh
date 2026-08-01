#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Attendance Bot — Server Setup ==="
echo ""

# Update system
echo "[1/8] Updating system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq git curl wget gnupg2 ca-certificates lsb-release unzip > /dev/null 2>&1

# Install Python and venv
echo "[2/8] Installing Python 3 and venv..."
sudo apt-get install -y -qq python3 python3-pip python3-venv > /dev/null 2>&1

# Install Chrome/Chromium
echo "[3/8] Installing Chromium browser..."
if ! command -v chromium-browser &> /dev/null && ! command -v google-chrome &> /dev/null; then
    wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | sudo apt-key add -
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq google-chrome-stable > /dev/null 2>&1
else
    echo "Chromium/Chrome already installed."
fi

# Create virtual environment
echo "[4/8] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

# Install Python dependencies
echo "[5/8] Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Generate FERNET_KEY if not set in .env
echo "[6/8] Checking .env configuration..."
if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found. Create one with TELEGRAM_BOT_TOKEN and FERNET_KEY."
    echo "Generate FERNET_KEY with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    exit 1
fi

# Initialize database
echo "[7/8] Initializing database..."
python3 -c "from db import init_db; init_db()"

# Warm up scraper runtime
echo "[8/8] Warming up scraper runtime..."
python3 -c "from scraper import warmup_scraper_runtime; warmup_scraper_runtime()"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Ensure your .env file has TELEGRAM_BOT_TOKEN and FERNET_KEY set"
echo "  2. Start the bot manually to test: python3 telegram_bot.py"
echo "  3. Install the systemd service for 24/7 operation:"
echo "     sudo cp attendance-bot.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now attendance-bot"
echo ""