from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .errors import AppConfigurationError


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DEFAULT_APP_SECRET = "change-this-secret"


@dataclass(frozen=True)
class Settings:
    base_url: str
    database_path: Path
    app_secret: str
    app_env: str
    use_waitress: bool
    waitress_threads: int
    dashboard_auto_refresh_seconds: int
    run_scheduler: bool
    task_queue_mode: str
    redis_url: str
    task_queue_name: str
    admin_username: str
    admin_password: str
    admin_telegram_username: str
    local_timezone: str
    morning_digest_time: str
    evening_report_time: str
    attendance_poll_interval_minutes: int
    substitution_poll_interval_minutes: int
    monitor_poll_interval_minutes: int
    sandbox_expiry_warning_minutes: int
    lecture_grace_minutes: int
    attendance_correction_lookback_days: int
    attendance_shortage_buffer_lectures: int
    delivery_retry_limit: int
    delivery_retry_backoff_seconds: int
    low_attendance_thresholds: tuple[int, ...]
    flask_host: str
    flask_port: int
    public_base_url: str
    webhook_rate_limit_count: int
    webhook_rate_limit_window_seconds: int
    admin_rate_limit_count: int
    admin_rate_limit_window_seconds: int
    sentry_dsn: str
    sentry_traces_sample_rate: float
    telegram_bot_token: str = ""
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_admin_chat_ids: tuple[str, ...] = ()
    telegram_poll_interval_seconds: int = 1
    lecture_schedule_poll_interval_seconds: int = 30
    attendance_poll_interval_seconds: int = 0
    substitution_poll_interval_seconds: int = 0
    monitor_poll_interval_seconds: int = 0
    telegram_bot_link: str = ""
    owner_telegram_contact: str = ""
    auto_captcha_enabled: bool = True
    auto_captcha_max_attempts: int = 5


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def env_int_list(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw_value = os.getenv(name, "")
    if not raw_value.strip():
        return default
    values: list[int] = []
    for part in raw_value.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        try:
            values.append(int(cleaned))
        except ValueError:
            continue
    normalized = tuple(sorted({value for value in values if value > 0}, reverse=True))
    return normalized or default


def env_timezone(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        ZoneInfo(value)
    except Exception:
        return default
    return value


def env_str_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw_value = os.getenv(name, "")
    if not raw_value.strip():
        return default
    values: list[str] = []
    for part in raw_value.split(","):
        cleaned = part.strip()
        if cleaned:
            values.append(cleaned)
    return tuple(dict.fromkeys(values)) or default


def load_settings() -> Settings:
    database_path = BASE_DIR / os.getenv("DATABASE_PATH", "data/bot.sqlite3")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    app_env = os.getenv("APP_ENV", "development").strip().lower() or "development"
    use_waitress = env_bool("USE_WAITRESS", app_env == "production")
    task_queue_mode = os.getenv("TASK_QUEUE_MODE", "inline").strip().lower() or "inline"
    if task_queue_mode not in {"inline", "rq"}:
        task_queue_mode = "inline"

    settings = Settings(
        base_url=os.getenv("ERP_BASE_URL", "https://qums.quantumuniversity.edu.in").rstrip("/"),
        database_path=database_path,
        app_secret=os.getenv("APP_SECRET", DEFAULT_APP_SECRET),
        app_env=app_env,
        use_waitress=use_waitress,
        waitress_threads=env_int("WAITRESS_THREADS", 8, minimum=1),
        dashboard_auto_refresh_seconds=env_int("DASHBOARD_AUTO_REFRESH_SECONDS", 30, minimum=0),
        run_scheduler=env_bool("RUN_SCHEDULER", True),
        task_queue_mode=task_queue_mode,
        redis_url=os.getenv("REDIS_URL", "").strip(),
        task_queue_name=os.getenv("TASK_QUEUE_NAME", "qums-bot").strip() or "qums-bot",
        admin_username=os.getenv("ADMIN_USERNAME", "").strip(),
        admin_password=os.getenv("ADMIN_PASSWORD", "").strip(),
        admin_telegram_username=os.getenv("ADMIN_TELEGRAM_USERNAME", "").strip(),
        local_timezone=env_timezone("LOCAL_TIMEZONE", "Asia/Kolkata"),
        morning_digest_time=os.getenv("MORNING_DIGEST_TIME", "06:30"),
        evening_report_time=os.getenv("EVENING_REPORT_TIME", "19:00"),
        attendance_poll_interval_minutes=env_int("ATTENDANCE_POLL_INTERVAL_MINUTES", 1, minimum=1),
        substitution_poll_interval_minutes=env_int("SUBSTITUTION_POLL_INTERVAL_MINUTES", 5, minimum=1),
        monitor_poll_interval_minutes=env_int("MONITOR_POLL_INTERVAL_MINUTES", 10, minimum=1),
        sandbox_expiry_warning_minutes=env_int("SANDBOX_EXPIRY_WARNING_MINUTES", 10, minimum=1),
        lecture_grace_minutes=env_int("LECTURE_GRACE_MINUTES", 20, minimum=0),
        attendance_correction_lookback_days=env_int("ATTENDANCE_CORRECTION_LOOKBACK_DAYS", 14, minimum=1),
        attendance_shortage_buffer_lectures=env_int("ATTENDANCE_SHORTAGE_BUFFER_LECTURES", 1, minimum=0),
        delivery_retry_limit=env_int("DELIVERY_RETRY_LIMIT", 3, minimum=0),
        delivery_retry_backoff_seconds=env_int("DELIVERY_RETRY_BACKOFF_SECONDS", 60, minimum=1),
        low_attendance_thresholds=env_int_list("LOW_ATTENDANCE_THRESHOLDS", (75, 70, 65)),
        flask_host=os.getenv("FLASK_HOST", "127.0.0.1"),
        flask_port=env_int("FLASK_PORT", env_int("PORT", 5000, minimum=1, maximum=65535), minimum=1, maximum=65535),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        webhook_rate_limit_count=env_int("WEBHOOK_RATE_LIMIT_COUNT", 60, minimum=1),
        webhook_rate_limit_window_seconds=env_int("WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", 60, minimum=1),
        admin_rate_limit_count=env_int("ADMIN_RATE_LIMIT_COUNT", 10, minimum=1),
        admin_rate_limit_window_seconds=env_int("ADMIN_RATE_LIMIT_WINDOW_SECONDS", 60, minimum=1),
        sentry_dsn=os.getenv("SENTRY_DSN", "").strip(),
        sentry_traces_sample_rate=env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0, minimum=0.0, maximum=1.0),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_api_base_url=os.getenv("TELEGRAM_API_BASE_URL", "https://api.telegram.org").rstrip("/"),
        telegram_admin_chat_ids=env_str_list("TELEGRAM_ADMIN_CHAT_IDS"),
        telegram_poll_interval_seconds=env_int("TELEGRAM_POLL_INTERVAL_SECONDS", 1, minimum=1, maximum=60),
        lecture_schedule_poll_interval_seconds=env_int("LECTURE_SCHEDULE_POLL_INTERVAL_SECONDS", 30, minimum=5, maximum=300),
        attendance_poll_interval_seconds=env_int("ATTENDANCE_POLL_INTERVAL_SECONDS", 0, minimum=0, maximum=300),
        substitution_poll_interval_seconds=env_int("SUBSTITUTION_POLL_INTERVAL_SECONDS", 0, minimum=0, maximum=300),
        monitor_poll_interval_seconds=env_int("MONITOR_POLL_INTERVAL_SECONDS", 0, minimum=0, maximum=300),
        telegram_bot_link=os.getenv("TELEGRAM_BOT_LINK", "https://t.me/QUMS_ALERT_BOT").strip(),
        owner_telegram_contact=os.getenv("OWNER_TELEGRAM_CONTACT", "").strip(),
        auto_captcha_enabled=env_bool("AUTO_CAPTCHA_ENABLED", True),
        auto_captcha_max_attempts=env_int("AUTO_CAPTCHA_MAX_ATTEMPTS", 5, minimum=1, maximum=15),
    )
    _validate_production_settings(settings)
    return settings


def _validate_production_settings(settings: Settings) -> None:
    if settings.app_env != "production":
        return
    if settings.app_secret == DEFAULT_APP_SECRET or not settings.app_secret.strip():
        raise AppConfigurationError("APP_SECRET must be set to a non-default value in production.")
    if not settings.admin_username or not settings.admin_password:
        raise AppConfigurationError("ADMIN_USERNAME and ADMIN_PASSWORD must be configured in production.")
