# Deployment Guide — attendance_bot

## Free Server Options (24/7)

### Option 1: Render.com (Recommended — No Credit Card)

Render.com offers a **free tier** with no credit card required:
- Free background worker (stays running)
- Supports Docker-based deployments
- Auto-deploys from GitHub
- No credit card needed

**Steps:**

1. Sign up at https://render.com (free, no credit card)
2. Connect your GitHub repo
3. Click **New +** → **Background Worker**
4. Configure:
   - **Name:** `attendance-bot`
   - **Region:** Oregon (US West) or closest to you
   - **Branch:** `main`
   - **Dockerfile:** Use the `Dockerfile` in your repo root
   - **Plan:** Free
5. Set environment variables in the Render dashboard:
   - `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather
   - `FERNET_KEY` — your encryption key
   - `SELENIUM_HEADLESS=1`
   - `FAST_MODE=1`
   - `ENABLE_HTTP_FAST_PATH=1`
   - `PAGE_LOAD_STRATEGY=eager`
   - `CHROMEDRIVER_PATH=/usr/bin/chromedriver`
6. Click **Create Background Worker**
7. Render builds the Docker image and starts the bot automatically
8. Push code changes to GitHub → Render auto-redeploys

**Note:** Render's free tier may have a brief cold-start delay (~30s) when the worker wakes from inactivity, but background workers stay running longer than web services.

### Option 2: Replit (No Credit Card)

Replit offers a **free tier** with no credit card required:
- Supports Python and can install Chrome
- Can run 24/7 bots
- Auto-deploys from GitHub

**Steps:**

1. Sign up at https://replit.com (free, no credit card)
2. Create a new Python repl
3. Upload your project files or import from GitHub
4. Add a `.replit` file with `run = "python telegram_bot.py"`
5. In the Secrets tab, add `TELEGRAM_BOT_TOKEN` and `FERNET_KEY`
6. Click Run

**Note:** Replit's free tier may be unreliable for long-running processes and may stop after periods of inactivity.

### Option 3: PythonAnywhere (No Credit Card)

PythonAnywhere offers a **free tier** with no credit card required:
- Supports scheduled tasks and long-running processes
- Limited to 1 CPU core, 512MB RAM on free tier
- May not support Selenium/Chrome well on the free tier

**Steps:**

1. Sign up at https://pythonanywhere.com (free, no credit card)
2. Upload your project files
3. Set up a scheduled task or always-on task
4. Configure environment variables

**Note:** PythonAnywhere's free tier has significant resource limitations and may not support Selenium/Chrome properly.

### Option 4: Google Cloud Free Tier (Credit Card Required for Verification)

Google Cloud offers an **always-free e2-micro** VM:
- 1 vCPU, 1GB RAM
- Always free (not a trial)
- Requires credit card for identity verification (not charged)
- Full Linux VM — install Chrome, Python, and run Selenium

**Steps:**

1. Sign up at https://cloud.google.com/free
2. Verify identity with credit card (not charged)
3. Create an e2-micro VM instance
4. SSH in and run the setup script

---

## Making Changes Take Effect on the Bot

### Git-Based Auto-Deploy (Render / Replit)

1. **Make changes locally** on your Windows machine in this folder.
2. **Commit and push** to GitHub/GitLab:
   ```bash
   git add .
   git commit -m "your change description"
   git push origin main
   ```
3. The platform auto-detects the push and **redeploys the bot automatically**.
4. The bot restarts within 1-2 minutes with the new code.

### Manual Update (if needed)

SSH into the server and run:
```bash
cd ~/attendance_bot
git pull origin main
bash setup_server.sh
sudo systemctl restart attendance-bot
```

### Auto-Reload (for development)

The `watch_reload.sh` script on the server watches for file changes and restarts the bot automatically. Enable it with:
```bash
sudo systemctl enable --now attendance-bot-watcher
```

---

## File Structure on Server

```
~/attendance_bot/
├── telegram_bot.py          # Main bot (unchanged)
├── scraper.py               # Selenium scraper (unchanged)
├── backend_server.py        # Flask backend (unchanged)
├── db.py                    # SQLite + Supabase (unchanged)
├── captcha_solver.py        # CAPTCHA solver (unchanged)
├── attendance_calc.py       # Attendance calculations (unchanged)
├── utils.py                 # Utilities (unchanged)
├── requirements.txt         # Dependencies (unchanged)
├── .env                     # Secrets (keep on server, never commit)
├── attendance_bot.db        # SQLite database (auto-created)
├── bot.log                  # Log file (auto-created)
├── setup_server.sh          # Server setup script (new)
├── watch_reload.sh          # File watcher for auto-reload (new)
├── speed_test.py            # Scraping speed test (new)
├── attendance-bot.service   # systemd service (new)
├── deploy.sh                # Git-based deploy script (new)
├── post-receive-hook.sh     # Auto-deploy hook (new)
├── Dockerfile               # Docker config for Render/Fly.io (new)
└── .gitignore               # Git ignore (new)
```

---

## Speed Testing

Run the speed test on the server to measure scraping performance:
```bash
cd ~/attendance_bot
python speed_test.py
```

This measures:
- Chrome startup time
- Login time
- Attendance fetch time
- Timetable fetch time
- Total end-to-end time

---

## Important Notes

- **Never commit `.env`** to Git — it contains your bot token and secrets.
- **The `.env` file must exist on the server** with your `TELEGRAM_BOT_TOKEN` and `FERNET_KEY`.
- **Chrome/Chromium must be installed** on the server for Selenium to work.
- **The `attendance_bot.db` file** is created automatically on first run.
- **If the bot crashes**, systemd will auto-restart it within seconds.