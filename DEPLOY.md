# Deployment Guide — attendance_bot

## Render.com Web Service (Free, No Credit Card)

Render.com's **web service** is free but spins down after 15 minutes of inactivity. We use **UptimeRobot** (also free) to ping it every 5 minutes and keep it awake.

### Step 1: Push Code to GitHub

Your repo is already at https://github.com/footonthegas/attendance_bot

### Step 2: Set Up Render.com

1. Go to https://render.com and sign up (free, no credit card)
2. Click **New +** → **Web Service**
3. Connect your GitHub repo `footonthegas/attendance_bot`
4. Configure:
   - **Name:** `attendance-bot`
   - **Region:** Oregon (US West)
   - **Branch:** `main`
   - **Plan:** Free
5. Render will auto-detect the `render.yaml` and use Docker
6. Click **Create Web Service**

### Step 3: Set Environment Variables

In the Render dashboard, go to your service → **Environment** → **Add Environment Variable**:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `FERNET_KEY` | Your Fernet encryption key |
| `SELENIUM_HEADLESS` | `1` |
| `FAST_MODE` | `1` |
| `ENABLE_HTTP_FAST_PATH` | `1` |
| `PAGE_LOAD_STRATEGY` | `eager` |
| `CHROMEDRIVER_PATH` | `/usr/bin/chromedriver` |

### Step 4: Deploy

Render will build the Docker image and start the bot. Check the **Logs** tab to confirm it's running.

### Step 5: Set Up UptimeRobot (Keep Awake)

Render spins down after 15 minutes of inactivity. UptimeRobot pings it every 5 minutes to keep it awake.

1. Go to https://uptimerobot.com and sign up (free, no credit card)
2. Click **+ Add New Monitor**
3. Configure:
   - **Monitor Type:** HTTP(s)
   - **Friendly Name:** `attendance-bot-keepalive`
   - **URL:** `https://attendance-bot.onrender.com/health`
   - **Monitor Interval:** 5 minutes
4. Click **Create Monitor**

The bot will now stay awake 24/7.

---

## Making Changes Take Effect

When you edit files locally and push to GitHub:

```powershell
cd "C:\Users\Theam\OneDrive\Desktop\attendance_bot"
git add .
git commit -m "your change description"
git push origin main
```

Render auto-detects the push and redeploys within 1-2 minutes.

---

## Speed Testing

Run the speed test to measure scraping performance:

```powershell
cd "C:\Users\Theam\OneDrive\Desktop\attendance_bot"
python speed_test.py
```

This measures:
- Chrome startup time
- Login time
- Attendance fetch time
- Timetable fetch time
- Total end-to-end time

Results are saved to `speed_test_results.log`.

---

## Important Notes

- **Never commit `.env`** to Git — it contains your bot token and secrets.
- **The `.env` file must exist on the server** with your `TELEGRAM_BOT_TOKEN` and `FERNET_KEY`.
- **Chrome/Chromium must be installed** on the server for Selenium to work.
- **The `attendance_bot.db` file** is created automatically on first run.
- **If the bot crashes**, Render will auto-restart it.
- **Render's free tier** spins down after 15 minutes — UptimeRobot prevents this.