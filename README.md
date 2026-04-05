# QUMS Academic Bot

Automated attendance tracking and notification system for Quantum University Roorkee, built with Python and Flask.

## What it does

- Stores ERP credentials and Telegram chat IDs in a local SQLite database
- Starts a manual ERP login session and shows the captcha in a local dashboard
- Submits the ERP login only after you type the captcha yourself
- Fetches the daily timetable, substitutions, and attendance summary from the ERP
- Uses each student's configured timezone for scheduled morning dispatches and daily date calculations
- Sends all notifications through **Telegram** (bot messages)
- Includes substitute lecture details in the morning schedule with faculty and timing information
- Detects new same-day substitute assignments later and sends a live Telegram alert
- Checks attendance after lectures and sends `Present`, `Absent`, or `Not marked yet`
- Sends one end-of-day attendance report after the final lecture check window closes (or at 7:00 PM fallback)
- Sends low-attendance threshold alerts and shortage warnings when attendance gets risky
- Detects ERP attendance corrections and notifies when a lecture is revised later
- Treats holiday / no-class / cancelled-class timetable rows as non-lecture entries
- Treats an empty Sunday timetable as an `Off Day` fallback
- Alerts when the ERP session has expired and manual login is required again
- Keeps a sent-message history, dead-letter queue, action center, audit log, CSV exports, and live auto-refresh dashboard
- Supports idempotent outbound alerts so automatic retries and restarts do not duplicate messages
- Retries transient delivery failures automatically and escalates exhausted items into a dead-letter queue
- Supports Telegram bot commands for admins and students
- Supports optional `RQ` worker dispatch for multi-instance-safe periodic jobs

### What it does not do

- It does not solve or bypass the ERP captcha automatically
- It does not clone the ERP website

## Requirements

- Python 3.14+
- A Telegram bot token for live notifications and bot commands

## Quick start

1. Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the bot:

```bash
python main.py
```

4. Open the dashboard:

```
http://127.0.0.1:5000
```

## Environment variables

The key variables to configure in `.env`:

| Variable | Description | Default |
|---|---|---|
| `ERP_BASE_URL` | Quantum University ERP URL | `https://qums.quantumuniversity.edu.in` |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token | (required) |
| `TELEGRAM_ADMIN_CHAT_IDS` | Comma-separated admin Telegram chat IDs | (required) |
| `ADMIN_USERNAME` | Dashboard login username | (empty) |
| `ADMIN_PASSWORD` | Dashboard login password | (empty) |
| `MORNING_DIGEST_TIME` | Time to send morning schedule | `06:30` |
| `EVENING_REPORT_TIME` | Fallback time for end-of-day report | `19:00` |
| `ATTENDANCE_POLL_INTERVAL_MINUTES` | How often to check attendance | `1` |
| `LECTURE_GRACE_MINUTES` | Grace period after lecture ends before checking | `20` |

See `.env.example` for the full list of available settings.

## Dashboard flow

1. Add the student profile, including any Telegram chat ID you want to use.
2. Click `Start ERP Login`.
3. Open the captcha page.
4. Type the captcha manually.
5. Click `Complete Login`.
6. Use `Preview Today` or `Send Morning Summary`.
7. Review recent sent alerts in the dashboard message history panel.

## Telegram commands

The bot supports Telegram-driven admin and student actions:

| Command | Description |
|---|---|
| `/menu` | Open the main menu |
| `/dashboard` | View dashboard status |
| `/students` | List student profiles |
| `/attendance` | View attendance summary |
| `/morning` | Send morning schedule |
| `/dayreport` | Send end-of-day report |
| `/shortage` | Send shortage report |
| `/startlogin` | Start ERP login session |

## How attendance updates work

The ERP exposes subject-level attendance totals, not a per-lecture API. The bot infers lecture results:

- If `TotalLecture` increases and `TotalPresent` increases → **Present**
- If `TotalLecture` increases and `TotalPresent` does not → **Absent**
- If totals do not change after the lecture ends → **Attendance not marked yet**
- After the final lecture of the day → summary report with total, present, absent, and unmarked counts
- If a previous day's lecture is marked later → correction notification with both original date and marking time
- Holiday / No Class / Off Day / cancelled rows are excluded from attendance checking

## ERP endpoints used

- `POST /Account/GetStudentDetail`
- `POST /Web_StudentAcademic/FillStudentTimeTable`
- `POST /Account/GetAllSubstitute`
- `POST /Web_StudentAcademic/GetSubjectDetailStudentAcademicFromLive`
- `POST /Account/showrefreshcaptchaImage`

## VPS deployment

For a small Ubuntu VPS, the repo includes deployment helpers:

- `scripts/deploy_vps.ps1` — uploads from Windows / PowerShell
- `scripts/deploy_vps.py` — uploads over SSH/SFTP with password auth
- `scripts/bootstrap_vps.sh` — installs packages, builds virtualenv, writes systemd + Nginx config

```powershell
.\scripts\deploy_vps.ps1 -Host 203.0.113.10 -User root -UploadEnv
```

See the script files for full usage details and flags.

## Deployment notes

- The app exposes `GET /healthz` for health checks
- `main.py` uses Waitress when `USE_WAITRESS=1`
- SQLite is fine for a small deployment; use `TASK_QUEUE_MODE=rq` with Redis before scaling
- ERP login requires a manual captcha flow through the dashboard when the session expires
- Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` to protect the dashboard
- Set `PUBLIC_BASE_URL` for correct Telegram-linked dashboard URLs
- Set `SENTRY_DSN` for error reporting in production

## Project structure

```
main.py
requirements.txt
qums_bot/
  app.py          — Flask routes and dashboard
  config.py       — Environment variable loading
  db.py           — SQLite database layer
  erp_client.py   — ERP HTTP client
  models.py       — Data models
  parsers.py      — ERP response parsers
  scheduler.py    — Background task scheduler
  security.py     — Auth utilities
  service.py      — Core business logic
  telegram.py     — Telegram Bot API client
  static/
    dashboard.css  — Dashboard styles (Slate + Emerald theme)
    auth.css       — Login page styles
  templates/
    dashboard.html — Admin dashboard
    login.html     — Login page
scripts/
  deploy_vps.ps1  — Windows VPS deployment
  deploy_vps.py   — Python VPS deployment
  bootstrap_vps.sh — Server bootstrap
tests/
  ...             — Test suite
```

## License

This project is for personal educational use with the Quantum University ERP system.
