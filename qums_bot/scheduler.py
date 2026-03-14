from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo

from .config import Settings
from .service import BotService
from .task_queue import TaskDispatcher

MIN_TELEGRAM_JOB_INTERVAL_SECONDS = 5


def build_scheduler(
    settings: Settings,
    service: BotService,
    dispatcher: TaskDispatcher | None = None,
) -> BackgroundScheduler:
    timezone = ZoneInfo(settings.local_timezone)
    telegram_job_interval_seconds = max(
        settings.telegram_poll_interval_seconds,
        MIN_TELEGRAM_JOB_INTERVAL_SECONDS,
    )
    telegram_refresh_interval_seconds = max(
        settings.dashboard_auto_refresh_seconds,
        MIN_TELEGRAM_JOB_INTERVAL_SECONDS,
    )
    scheduler = BackgroundScheduler(
        timezone=timezone,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60,
        },
    )

    scheduler.add_job(
        _dispatch(
            service.run_scheduled_dispatch,
            dispatcher=dispatcher,
            job_name="scheduled-dispatch",
            callback_name="run_scheduled_dispatch",
            interval_minutes=1,
        ),
        "interval",
        minutes=1,
        id="scheduled-dispatch",
        replace_existing=True,
    )
    scheduler.add_job(
        _dispatch(
            service.run_due_checks,
            dispatcher=dispatcher,
            job_name="attendance-checks",
            callback_name="run_due_checks",
            interval_minutes=settings.attendance_poll_interval_minutes,
        ),
        "interval",
        minutes=settings.attendance_poll_interval_minutes,
        id="attendance-checks",
        replace_existing=True,
    )
    scheduler.add_job(
        _dispatch(
            service.run_substitution_sweep,
            dispatcher=dispatcher,
            job_name="substitution-checks",
            callback_name="run_substitution_sweep",
            interval_minutes=settings.substitution_poll_interval_minutes,
        ),
        "interval",
        minutes=settings.substitution_poll_interval_minutes,
        id="substitution-checks",
        replace_existing=True,
    )
    scheduler.add_job(
        _dispatch(
            service.run_monitor_sweep,
            dispatcher=dispatcher,
            job_name="monitor-checks",
            callback_name="run_monitor_sweep",
            interval_minutes=settings.monitor_poll_interval_minutes,
        ),
        "interval",
        minutes=settings.monitor_poll_interval_minutes,
        id="monitor-checks",
        replace_existing=True,
    )
    scheduler.add_job(
        _dispatch(
            service.run_retry_sweep,
            dispatcher=dispatcher,
            job_name="delivery-retry-checks",
            callback_name="run_retry_sweep",
            interval_minutes=1,
        ),
        "interval",
        minutes=1,
        id="delivery-retry-checks",
        replace_existing=True,
    )
    scheduler.add_job(
        _dispatch(
            service.run_telegram_inbound_sweep,
            dispatcher=dispatcher,
            job_name="telegram-inbound-checks",
            callback_name="run_telegram_inbound_sweep",
            interval_seconds=telegram_job_interval_seconds,
        ),
        "interval",
        seconds=telegram_job_interval_seconds,
        id="telegram-inbound-checks",
        replace_existing=True,
    )
    scheduler.add_job(
        _dispatch(
            service.run_telegram_admin_refresh_sweep,
            dispatcher=dispatcher,
            job_name="telegram-admin-refresh-checks",
            callback_name="run_telegram_admin_refresh_sweep",
            interval_seconds=telegram_refresh_interval_seconds,
        ),
        "interval",
        seconds=telegram_refresh_interval_seconds,
        id="telegram-admin-refresh-checks",
        replace_existing=True,
    )
    return scheduler


def _dispatch(
    callback,
    *,
    dispatcher: TaskDispatcher | None,
    job_name: str,
    callback_name: str,
    interval_minutes: int | None = None,
    interval_seconds: int | None = None,
):
    if dispatcher is None:
        return callback

    def runner() -> None:
        dispatcher.dispatch_periodic(
            job_name=job_name,
            callback_name=callback_name,
            interval_minutes=interval_minutes,
            interval_seconds=interval_seconds,
        )

    return runner
