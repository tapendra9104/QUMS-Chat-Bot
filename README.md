# QUMS Multi-Channel Bot

Local Python bot for the Quantum University ERP.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/tapendra9104/QUMS-Chat-Bot)

What it does:
- stores ERP user id, password, WhatsApp number, and Telegram chat id in a local SQLite database
- starts a manual ERP login session and shows the captcha in a local dashboard
- submits the ERP login only after you type the captcha yourself
- fetches the daily timetable, substitutions, and attendance summary from the ERP
- uses each student's configured timezone for scheduled morning dispatches and daily date calculations
- sends the same notification to every configured channel for the student: WhatsApp and Telegram
- includes substitute lecture details in the morning schedule with faculty and timing information
- detects new same-day substitute assignments later and sends a live WhatsApp alert
- checks attendance after lectures and sends `present`, `absent`, or `not marked yet`
- sends one end-of-day attendance report after the final lecture check window closes
- sends low-attendance threshold alerts and shortage warnings when attendance gets risky
- detects ERP attendance corrections and notifies when a lecture is revised later
- treats holiday / no-class / cancelled-class timetable rows as non-lecture entries so they do not trigger attendance pending alerts
- treats an empty Sunday timetable as an `Off Day` fallback so it does not appear as a missing routine
- warns before the Twilio sandbox expires and reminds the user to send the current `join <code>` manually from WhatsApp
- alerts when the ERP session has expired and manual login is required again
- keeps a sent-message history, dead-letter queue, action center, audit log, CSV exports, and live auto-refresh dashboard state
- persists attendance marked timestamps and shows them in lecture alerts, daily reports, and the dashboard
- validates Twilio webhook signatures, rate-limits sensitive routes, and can send delivery events back into message history
- supports idempotent outbound alerts so automatic retries and restarts do not duplicate the same logical message
- retries transient delivery failures automatically per channel and escalates exhausted items into a dead-letter queue
- supports inbound WhatsApp commands like `help`, `today`, `next`, `attendance`, and `login status`
- supports optional `RQ` worker dispatch for multi-instance-safe periodic jobs

What it does not do:
- it does not solve or bypass the ERP captcha automatically
- it does not clone the ERP website
- it cannot auto-join a Twilio WhatsApp sandbox session on behalf of the user

## Render deployment

This repo now includes `render.yaml` for a Render web-service deployment.

Recommended Render shape for the current codebase:
- one always-on web service
- one persistent disk mounted at `/var/data`
- one instance only

Default single-instance notes:
- the default mode still uses APScheduler in the web process
- SQLite is fine for a small single-instance deployment
- queue mode is optional and disabled by default

Multi-instance notes:
- set `TASK_QUEUE_MODE=rq`
- set `REDIS_URL`
- run a separate worker with `python worker.py`
- use a shared application database before splitting web and worker processes; separate local SQLite files are not enough
- keep `PUBLIC_BASE_URL` set so Twilio callbacks validate correctly behind your public host
- the app now uses database-backed periodic slot claims and outbound idempotency keys to avoid duplicate scheduler work and duplicate alerts

Before deploying on Render, set these environment variables in the Render dashboard:
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`

For testing on sandbox:
- keep `TWILIO_WHATSAPP_MODE=sandbox`
- keep `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`
- set `TWILIO_SANDBOX_JOIN_CODE` if you want the dashboard to show the exact join command

For real production delivery:
- change `TWILIO_WHATSAPP_MODE=production`
- change `TWILIO_WHATSAPP_FROM` to your approved WhatsApp-enabled Twilio sender
- set `TWILIO_CONTENT_SID_DEFAULT` or the specific morning and attendance template SIDs
- do not rely on the sandbox for daily automated notifications

## ERP endpoints used

- `POST /Account/GetStudentDetail`
- `POST /Web_StudentAcademic/FillStudentTimeTable`
- `POST /Account/GetAllSubstitute`
- `POST /Web_StudentAcademic/GetSubjectDetailStudentAcademicFromLive`
- `POST /Account/showrefreshcaptchaImage`

## How attendance updates work

The ERP pages available in this workspace expose subject-level attendance totals, not a clean per-lecture API.

Because of that, the bot infers lecture results like this:
- if `TotalLecture` increases and `TotalPresent` increases, the bot reports `Present`
- if `TotalLecture` increases and `TotalPresent` does not, the bot reports `Absent`
- if totals do not change after the lecture ends, the bot reports `Attendance not marked yet`
- after the final lecture of the day, the bot sends a summary with total lectures, marked lectures, present, absent, and still-unmarked lectures
- if a previous day's lecture is marked later, the bot still sends the lecture-wise update and includes both the original lecture date and the later marking time
- if the timetable row says `Holiday`, `No Class`, `Off Day`, or a cancelled-class variant, the bot keeps it out of attendance checking

## Requirements

- Python 3.14+
- at least one delivery channel:
  - a Twilio WhatsApp sender
  - or a Telegram bot token

Copy `.env.example` to `.env` and fill in:

```env
ERP_BASE_URL=https://qums.quantumuniversity.edu.in
DATABASE_PATH=data/bot.sqlite3
APP_SECRET=change-this-secret
APP_ENV=development
USE_WAITRESS=0
WAITRESS_THREADS=8
DASHBOARD_AUTO_REFRESH_SECONDS=30
RUN_SCHEDULER=1
TASK_QUEUE_MODE=inline
REDIS_URL=
TASK_QUEUE_NAME=qums-bot
ADMIN_USERNAME=
ADMIN_PASSWORD=
ADMIN_TELEGRAM_USERNAME=
LOCAL_TIMEZONE=Asia/Kolkata
MORNING_DIGEST_TIME=06:30
EVENING_REPORT_TIME=19:00
ATTENDANCE_POLL_INTERVAL_MINUTES=1
SUBSTITUTION_POLL_INTERVAL_MINUTES=1
MONITOR_POLL_INTERVAL_MINUTES=1
SANDBOX_EXPIRY_WARNING_MINUTES=10
LECTURE_GRACE_MINUTES=20
ATTENDANCE_CORRECTION_LOOKBACK_DAYS=14
ATTENDANCE_SHORTAGE_BUFFER_LECTURES=1
DELIVERY_RETRY_LIMIT=3
DELIVERY_RETRY_BACKOFF_SECONDS=60
LOW_ATTENDANCE_THRESHOLDS=75,70,65
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
PUBLIC_BASE_URL=
WEBHOOK_RATE_LIMIT_COUNT=60
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS=60
ADMIN_RATE_LIMIT_COUNT=10
ADMIN_RATE_LIMIT_WINDOW_SECONDS=60
SENTRY_DSN=
SENTRY_TRACES_SAMPLE_RATE=0.0
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_MODE=sandbox
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_SANDBOX_JOIN_CODE=
TWILIO_STATUS_MESSAGE_LIMIT=50
TWILIO_STATUS_CALLBACK_URL=
TWILIO_CONTENT_SID_DEFAULT=
TWILIO_CONTENT_SID_MORNING=
TWILIO_CONTENT_SID_ATTENDANCE=
TELEGRAM_BOT_TOKEN=
TELEGRAM_API_BASE_URL=https://api.telegram.org
```

Admin login recovery:
- `ADMIN_USERNAME` and `ADMIN_PASSWORD` remain the bootstrap credentials
- `ADMIN_TELEGRAM_USERNAME` can be set for Telegram password recovery
- after sign-in, you can update the admin login username, password, and recovery Telegram username from the dashboard
- if you forget the password, the login page can send a one-time reset code to the configured Telegram admin chat after you verify the recovery Telegram username

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the bot:

```bash
python main.py
```

Run the optional RQ worker:

```bash
python worker.py
```

Open the dashboard:

```text
http://127.0.0.1:5000
```

## Dashboard flow

1. Add the student profile, including any Telegram chat id you want to use.
2. Click `Start ERP Login`.
3. Open the captcha page.
4. Type the captcha manually.
5. Click `Complete Login`.
6. Use `Preview Today` or `Send Morning Summary`.
7. Review recent sent alerts in the dashboard message history panel.

## WhatsApp inbound commands

Configure Twilio's inbound webhook to point at:

```text
POST /webhooks/twilio/inbound
```

Available commands:
- `help`
- `today`
- `next`
- `attendance`
- `login status`

## Twilio modes

### Sandbox mode

- set `TWILIO_WHATSAPP_MODE=sandbox`
- use `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`
- each recipient must send the current `join <code>` command from their own WhatsApp account
- sandbox access expires after roughly 72 hours and must be renewed by the user
- the dashboard can display the exact join command if you set `TWILIO_SANDBOX_JOIN_CODE`
- the bot can warn shortly before expiry, but the actual `join <code>` message still must be sent manually from the recipient's WhatsApp

### Production mode

- set `APP_ENV=production`
- set `USE_WAITRESS=1`
- set `TWILIO_WHATSAPP_MODE=production`
- use a WhatsApp-enabled Twilio sender, not the sandbox number
- configure approved content template SIDs for scheduled messages:
  - `TWILIO_CONTENT_SID_DEFAULT`
  - or `TWILIO_CONTENT_SID_MORNING` and `TWILIO_CONTENT_SID_ATTENDANCE`

The sender code uses template SIDs automatically for scheduled morning messages, lecture attendance notifications, and the end-of-day attendance report when production mode is enabled.

## Deployment notes

- the app exposes `GET /healthz` for health checks
- `main.py` uses Waitress when `USE_WAITRESS=1`
- SQLite is acceptable for a small deployment; use `TASK_QUEUE_MODE=rq` with Redis and a worker before scaling web instances
- ERP login still requires a manual captcha refresh flow through the dashboard when the ERP session expires
- the deployed dashboard should always be protected with `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- if lecture end times are missing from the routine, the end-of-day report falls back to `EVENING_REPORT_TIME`
- set `PUBLIC_BASE_URL` or `TWILIO_STATUS_CALLBACK_URL` if you want validated Twilio delivery callbacks
- set `SENTRY_DSN` if you want unhandled errors reported to Sentry

## Important Twilio note

Scheduled business-initiated WhatsApp messages can require an approved template outside the 24-hour customer service window. Sandbox cannot be auto-joined by the server, because the user must send the `join` command from their own WhatsApp account.

## Project structure

```text
main.py
requirements.txt
qums_bot/
  app.py
  config.py
  db.py
  erp_client.py
  models.py
  parsers.py
  scheduler.py
  security.py
  service.py
  telegram.py
  whatsapp.py
  templates/
    dashboard.html
    login.html
```
