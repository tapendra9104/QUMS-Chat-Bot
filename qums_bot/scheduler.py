from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from .config import Settings
from .service import BotService


def build_scheduler(settings: Settings, service: BotService) -> BackgroundScheduler:
    timezone = ZoneInfo(settings.local_timezone)
    scheduler = BackgroundScheduler(timezone=timezone)

    hour_text, minute_text = settings.morning_digest_time.split(":", 1)
    scheduler.add_job(
        service.run_morning_sweep,
        CronTrigger(hour=int(hour_text), minute=int(minute_text), timezone=timezone),
        id="morning-digest",
        replace_existing=True,
    )
    scheduler.add_job(
        service.run_due_checks,
        "interval",
        minutes=settings.attendance_poll_interval_minutes,
        id="attendance-checks",
        replace_existing=True,
    )
    return scheduler
