# QUMS Academic Bot

Automated attendance tracking and notification system for Quantum University Roorkee, built with Python and Flask.

## What it does

- Stores ERP credentials and Telegram chat IDs in a local SQLite database
- Starts a manual or **automatic** ERP login session — auto-captcha solver (ddddocr + image preprocessing) handles login unattended
- Falls back to manual captcha entry if auto-login fails after configurable attempts
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
- Can automatically solve ERP captchas using OCR (`ddddocr`) for unattended session recovery

### What it does not do

- It does not clone the ERP website

## Requirements

- Python 3.11+
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
| `LECTURE_SCHEDULE_POLL_INTERVAL_SECONDS` | Lecture schedule live polling interval | `30` |
| `AUTO_CAPTCHA_ENABLED` | Enable automatic captcha solving via OCR | `true` |
| `AUTO_CAPTCHA_MAX_ATTEMPTS` | Max OCR attempts per auto-login | `3` |
| `RUN_SCHEDULER` | Start the background scheduler | `true` |

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
| `/adminapps` | List admin access applications |
| `/applications` | List student sign-up applications |

## Admin roles

The system supports primary and secondary admin roles:

- **Primary admin**: Configured via `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env`. Has full control over everything: admin security settings, approve/reject admin applications, enable/disable/remove secondary admins, and toggle all features.
- **Secondary admin**: Created when the primary admin approves an admin application. Can manage students (add, edit, delete), accept user sign-up applications, view all student data (IDs, passwords), send reports, and access the full dashboard. Cannot modify admin security settings, manage other admins, or approve admin applications.

### Admin application flow

1. A public user visits `/admin/apply` and submits their name, desired username, and password.
2. The primary admin sees the pending application on the dashboard under "🛡️ Admin Applications".
3. The primary admin approves or rejects the application.
4. If approved, the applicant can immediately sign in at `/admin/login` with their chosen credentials.
5. The primary admin can disable or remove secondary admin accounts at any time.

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
- Auto-captcha (`AUTO_CAPTCHA_ENABLED=1`) recovers expired sessions automatically
- Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` to protect the dashboard
- Set `PUBLIC_BASE_URL` for correct Telegram-linked dashboard URLs
- Set `SENTRY_DSN` for error reporting in production

## Production deployment checklist

1. Set `APP_ENV=production`
2. Set `APP_SECRET` to a strong, unique value (used for password encryption)
3. Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` for dashboard auth (required in production)
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_CHAT_IDS`
5. Set `PUBLIC_BASE_URL` to your server's public URL
6. Set `USE_WAITRESS=1` for production WSGI server
7. Set `RUN_SCHEDULER=1` to enable background jobs
8. Optionally set `SENTRY_DSN` for error monitoring
9. Optionally set `AUTO_CAPTCHA_ENABLED=1` for unattended session recovery
10. Run `pip install -r requirements.txt`
11. Run `python main.py`

## Database backups

The scheduler runs a daily SQLite backup at midnight (local timezone). Backups are saved to `data/backups/` and the last 7 are kept automatically.

To trigger a manual backup:

```python
from qums_bot.runtime import build_runtime
runtime = build_runtime()
runtime.db.backup_to("data/backups")
```

## API routes

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | Public/Admin | Dashboard (public view or admin view) |
| `GET` | `/healthz` | None | Health check endpoint |
| `GET` | `/login` | None | Student site login page |
| `POST` | `/login` | None | Student site login submit |
| `GET` | `/admin/login` | None | Admin login page |
| `POST` | `/admin/login` | None | Admin login submit |
| `POST` | `/admin/logout` | Admin | Admin logout |
| `POST` | `/logout` | Student | Student logout |
| `GET` | `/dashboard/live-data` | Public/Admin | Live dashboard JSON data |
| `POST` | `/students` | Admin | Create/update student |
| `POST` | `/students/<id>/controls` | Admin | Update student notification controls |
| `POST` | `/students/<id>/delete` | Admin | Delete student |
| `POST` | `/students/<id>/login/start` | Admin | Start ERP login flow |
| `GET` | `/students/<id>/login` | Admin | Captcha entry page |
| `POST` | `/students/<id>/login/refresh` | Admin | Refresh captcha image |
| `POST` | `/students/<id>/login/complete` | Admin | Submit captcha |
| `POST` | `/students/<id>/preview` | Admin | Preview today's schedule |
| `POST` | `/students/<id>/send-morning` | Admin | Send morning summary |
| `POST` | `/students/<id>/send-attendance-summary` | Admin | Send attendance summary |
| `POST` | `/students/<id>/send-substitution-report` | Admin | Send substitution report |
| `POST` | `/students/<id>/send-evening` | Admin | Send day report |
| `POST` | `/students/<id>/send-shortage-report` | Admin | Send shortage report |
| `POST` | `/students/<id>/send-test` | Admin | Send test notification |
| `POST` | `/applications` | None | Submit self-signup application |
| `POST` | `/applications/<id>/accept` | Admin | Accept application |
| `POST` | `/applications/<id>/reject` | Admin | Reject application |
| `POST` | `/applications/<id>/clear` | Admin | Clear application |
| `POST` | `/dead-letter/<key>/retry` | Admin | Retry dead letter message |
| `POST` | `/checks/run` | Admin | Trigger manual attendance check |
| `POST` | `/exports/message-history.csv` | Admin | Export message history |
| `POST` | `/exports/audit-log.csv` | Admin | Export audit log |
| `GET` | `/admin/apply` | Public | Admin access application form |
| `POST` | `/admin/apply` | Public | Submit admin application |
| `POST` | `/admin/applications/<id>/accept` | Primary Admin | Approve admin application |
| `POST` | `/admin/applications/<id>/reject` | Primary Admin | Reject admin application |
| `POST` | `/admin/applications/<id>/clear` | Primary Admin | Clear admin application record |
| `POST` | `/admin/accounts/<id>/toggle` | Primary Admin | Enable/disable secondary admin |
| `POST` | `/admin/accounts/<id>/remove` | Primary Admin | Remove secondary admin account |

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
    admin_apply.html — Admin application form
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
