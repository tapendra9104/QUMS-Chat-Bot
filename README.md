# QUMS WhatsApp Bot

Local Python bot for the Quantum University ERP.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/tapendra9104/QUMS-Chat-Bot)

What it does:
- stores ERP user id, password, and WhatsApp number in a local SQLite database
- starts a manual ERP login session and shows the captcha in a local dashboard
- submits the ERP login only after you type the captcha yourself
- fetches the daily timetable, substitutions, and attendance summary from the ERP
- sends a morning WhatsApp summary
- checks attendance after lectures and sends `present`, `absent`, or `not marked yet`

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

Why one instance only:
- the app uses APScheduler inside the web process for morning and attendance jobs
- the app uses SQLite by default, so scaling to multiple instances would create duplicate jobs and split state

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

## Requirements

- Python 3.14+
- a Twilio WhatsApp sender

Copy `.env.example` to `.env` and fill in:

```env
ERP_BASE_URL=https://qums.quantumuniversity.edu.in
DATABASE_PATH=data/bot.sqlite3
APP_SECRET=change-this-secret
APP_ENV=development
USE_WAITRESS=0
WAITRESS_THREADS=8
LOCAL_TIMEZONE=Asia/Kolkata
MORNING_DIGEST_TIME=06:30
ATTENDANCE_POLL_INTERVAL_MINUTES=10
LECTURE_GRACE_MINUTES=20
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_MODE=sandbox
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_SANDBOX_JOIN_CODE=
TWILIO_STATUS_MESSAGE_LIMIT=50
TWILIO_CONTENT_SID_DEFAULT=
TWILIO_CONTENT_SID_MORNING=
TWILIO_CONTENT_SID_ATTENDANCE=
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the bot:

```bash
python main.py
```

Open the dashboard:

```text
http://127.0.0.1:5000
```

## Dashboard flow

1. Add the student profile.
2. Click `Start ERP Login`.
3. Open the captcha page.
4. Type the captcha manually.
5. Click `Complete Login`.
6. Use `Preview Today` or `Send Morning Summary`.

## Twilio modes

### Sandbox mode

- set `TWILIO_WHATSAPP_MODE=sandbox`
- use `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`
- each recipient must send the current `join <code>` command from their own WhatsApp account
- sandbox access expires after roughly 72 hours and must be renewed by the user
- the dashboard can display the exact join command if you set `TWILIO_SANDBOX_JOIN_CODE`

### Production mode

- set `APP_ENV=production`
- set `USE_WAITRESS=1`
- set `TWILIO_WHATSAPP_MODE=production`
- use a WhatsApp-enabled Twilio sender, not the sandbox number
- configure approved content template SIDs for scheduled messages:
  - `TWILIO_CONTENT_SID_DEFAULT`
  - or `TWILIO_CONTENT_SID_MORNING` and `TWILIO_CONTENT_SID_ATTENDANCE`

The sender code uses template SIDs automatically for scheduled morning and attendance notifications when production mode is enabled.

## Deployment notes

- the app exposes `GET /healthz` for health checks
- `main.py` uses Waitress when `USE_WAITRESS=1`
- SQLite is acceptable for a small single-instance deployment; move to PostgreSQL before multi-instance deployment
- ERP login still requires a manual captcha refresh flow through the dashboard when the ERP session expires
- the deployed dashboard should always be protected with `ADMIN_USERNAME` and `ADMIN_PASSWORD`

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
  whatsapp.py
  templates/
    dashboard.html
    login.html
```
