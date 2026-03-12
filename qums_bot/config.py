from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    base_url: str
    database_path: Path
    app_secret: str
    app_env: str
    use_waitress: bool
    waitress_threads: int
    local_timezone: str
    morning_digest_time: str
    attendance_poll_interval_minutes: int
    lecture_grace_minutes: int
    flask_host: str
    flask_port: int
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_mode: str
    twilio_whatsapp_from: str
    twilio_sandbox_join_code: str
    twilio_status_message_limit: int
    twilio_content_sid_default: str
    twilio_content_sid_morning: str
    twilio_content_sid_attendance: str


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    database_path = BASE_DIR / os.getenv("DATABASE_PATH", "data/bot.sqlite3")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    app_env = os.getenv("APP_ENV", "development").strip().lower() or "development"
    use_waitress = env_bool("USE_WAITRESS", app_env == "production")
    twilio_mode = os.getenv("TWILIO_WHATSAPP_MODE", "sandbox").strip().lower() or "sandbox"
    if twilio_mode not in {"sandbox", "production"}:
        twilio_mode = "sandbox"

    return Settings(
        base_url=os.getenv("ERP_BASE_URL", "https://qums.quantumuniversity.edu.in").rstrip("/"),
        database_path=database_path,
        app_secret=os.getenv("APP_SECRET", "change-this-secret"),
        app_env=app_env,
        use_waitress=use_waitress,
        waitress_threads=int(os.getenv("WAITRESS_THREADS", "8")),
        local_timezone=os.getenv("LOCAL_TIMEZONE", "Asia/Kolkata"),
        morning_digest_time=os.getenv("MORNING_DIGEST_TIME", "06:30"),
        attendance_poll_interval_minutes=int(os.getenv("ATTENDANCE_POLL_INTERVAL_MINUTES", "10")),
        lecture_grace_minutes=int(os.getenv("LECTURE_GRACE_MINUTES", "20")),
        flask_host=os.getenv("FLASK_HOST", "127.0.0.1"),
        flask_port=int(os.getenv("FLASK_PORT", "5000")),
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
        twilio_whatsapp_mode=twilio_mode,
        twilio_whatsapp_from=os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
        twilio_sandbox_join_code=os.getenv("TWILIO_SANDBOX_JOIN_CODE", "").strip(),
        twilio_status_message_limit=int(os.getenv("TWILIO_STATUS_MESSAGE_LIMIT", "50")),
        twilio_content_sid_default=os.getenv("TWILIO_CONTENT_SID_DEFAULT", "").strip(),
        twilio_content_sid_morning=os.getenv("TWILIO_CONTENT_SID_MORNING", "").strip(),
        twilio_content_sid_attendance=os.getenv("TWILIO_CONTENT_SID_ATTENDANCE", "").strip(),
    )
