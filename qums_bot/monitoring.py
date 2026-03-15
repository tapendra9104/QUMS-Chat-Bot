from __future__ import annotations

import logging

from .config import Settings


DEFAULT_FLUSH_TIMEOUT_SECONDS = 2.0


def init_monitoring(
    settings: Settings,
    *,
    component: str = "web",
    include_flask_integration: bool = False,
) -> bool:
    if not settings.sentry_dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logging.getLogger(__name__).warning(
            "Sentry DSN is configured but sentry-sdk is not installed."
        )
        return False

    if sentry_sdk.get_client().is_active():
        active_dsn = str(sentry_sdk.get_client().options.get("dsn") or "")
        if active_dsn == settings.sentry_dsn:
            sentry_sdk.set_tag("component", component)
            sentry_sdk.set_tag("task_queue_mode", settings.task_queue_mode)
            return True

    integrations = [
        LoggingIntegration(
            level=logging.INFO,
            event_level=logging.ERROR,
        )
    ]
    if include_flask_integration:
        try:
            from sentry_sdk.integrations.flask import FlaskIntegration
        except ImportError:
            logging.getLogger(__name__).warning(
                "Flask is installed but the Sentry Flask integration could not be loaded."
            )
        else:
            integrations.append(FlaskIntegration())
    if settings.task_queue_mode == "rq":
        try:
            from sentry_sdk.integrations.rq import RqIntegration
        except ImportError:
            logging.getLogger(__name__).warning(
                "TASK_QUEUE_MODE=rq is enabled but the Sentry RQ integration could not be loaded."
            )
        else:
            integrations.append(RqIntegration())

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=integrations,
        send_default_pii=False,
    )
    sentry_sdk.set_tag("component", component)
    sentry_sdk.set_tag("task_queue_mode", settings.task_queue_mode)
    return True


def capture_monitoring_exception(
    exc: BaseException,
    *,
    flush: bool = False,
    timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
) -> bool:
    try:
        import sentry_sdk
    except ImportError:
        return False

    if not sentry_sdk.get_client().is_active():
        return False

    sentry_sdk.capture_exception(exc)
    if flush:
        sentry_sdk.flush(timeout=timeout)
    return True
