from __future__ import annotations

import logging

from .config import Settings


def init_monitoring(settings: Settings) -> bool:
    if not settings.sentry_dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        logging.getLogger(__name__).warning(
            "Sentry DSN is configured but sentry-sdk is not installed."
        )
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[FlaskIntegration()],
        send_default_pii=False,
    )
    return True
