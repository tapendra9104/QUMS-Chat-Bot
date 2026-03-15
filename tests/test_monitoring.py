from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from qums_bot.config import Settings
from qums_bot.monitoring import init_monitoring
from qums_bot import task_queue


def make_settings(db_path: Path, *, sentry_dsn: str = "", task_queue_mode: str = "inline") -> Settings:
    return Settings(
        base_url="https://example.com",
        database_path=db_path,
        app_secret="secret",
        app_env="development",
        use_waitress=False,
        waitress_threads=8,
        dashboard_auto_refresh_seconds=30,
        run_scheduler=False,
        task_queue_mode=task_queue_mode,
        redis_url="redis://localhost:6379/0" if task_queue_mode == "rq" else "",
        task_queue_name="qums-bot",
        admin_username="",
        admin_password="",
        admin_telegram_username="",
        local_timezone="Asia/Kolkata",
        morning_digest_time="06:30",
        evening_report_time="19:00",
        attendance_poll_interval_minutes=10,
        substitution_poll_interval_minutes=5,
        monitor_poll_interval_minutes=5,
        sandbox_expiry_warning_minutes=10,
        lecture_grace_minutes=20,
        attendance_correction_lookback_days=14,
        attendance_shortage_buffer_lectures=1,
        delivery_retry_limit=3,
        delivery_retry_backoff_seconds=1,
        low_attendance_thresholds=(75, 70, 65),
        flask_host="127.0.0.1",
        flask_port=5000,
        public_base_url="https://example.com",
        webhook_rate_limit_count=60,
        webhook_rate_limit_window_seconds=60,
        admin_rate_limit_count=10,
        admin_rate_limit_window_seconds=60,
        sentry_dsn=sentry_dsn,
        sentry_traces_sample_rate=0.25,
        twilio_account_sid="",
        twilio_auth_token="",
        twilio_whatsapp_mode="sandbox",
        twilio_whatsapp_from="whatsapp:+14155238886",
        twilio_sandbox_join_code="demo-code",
        twilio_status_message_limit=50,
        twilio_status_callback_url="",
        twilio_content_sid_default="",
        twilio_content_sid_morning="",
        twilio_content_sid_attendance="",
    )


class MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = Path("tmp-test2")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_root / self.id().replace(".", "_")
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_monitoring_returns_false_without_dsn(self) -> None:
        settings = make_settings(self.tmp / "bot.sqlite3")

        self.assertFalse(init_monitoring(settings))

    def test_init_monitoring_registers_logging_and_flask_integrations(self) -> None:
        settings = make_settings(self.tmp / "bot.sqlite3", sentry_dsn="https://example@sentry.invalid/1")

        with patch("sentry_sdk.init") as init_mock, patch("sentry_sdk.set_tag") as set_tag_mock:
            enabled = init_monitoring(
                settings,
                component="web",
                include_flask_integration=True,
            )

        self.assertTrue(enabled)
        init_kwargs = init_mock.call_args.kwargs
        integration_names = {type(item).__name__ for item in init_kwargs["integrations"]}
        self.assertEqual(init_kwargs["dsn"], settings.sentry_dsn)
        self.assertEqual(init_kwargs["environment"], settings.app_env)
        self.assertEqual(init_kwargs["traces_sample_rate"], 0.25)
        self.assertFalse(init_kwargs["send_default_pii"])
        self.assertIn("LoggingIntegration", integration_names)
        self.assertIn("FlaskIntegration", integration_names)
        set_tag_mock.assert_any_call("component", "web")
        set_tag_mock.assert_any_call("task_queue_mode", "inline")

    def test_init_monitoring_reuses_active_client_for_same_dsn(self) -> None:
        settings = make_settings(self.tmp / "bot.sqlite3", sentry_dsn="https://example@sentry.invalid/1")
        active_client = Mock()
        active_client.is_active.return_value = True
        active_client.options = {"dsn": settings.sentry_dsn}

        with (
            patch("sentry_sdk.get_client", return_value=active_client),
            patch("sentry_sdk.init") as init_mock,
            patch("sentry_sdk.set_tag") as set_tag_mock,
        ):
            enabled = init_monitoring(settings, component="worker")

        self.assertTrue(enabled)
        init_mock.assert_not_called()
        set_tag_mock.assert_any_call("component", "worker")
        set_tag_mock.assert_any_call("task_queue_mode", "inline")

    def test_execute_dispatched_task_reports_failures_to_monitoring(self) -> None:
        settings = make_settings(self.tmp / "bot.sqlite3", sentry_dsn="https://example@sentry.invalid/1")
        runtime = Mock()
        runtime.service = Mock()
        runtime.service.run_due_checks.side_effect = RuntimeError("job failed")

        with (
            patch("qums_bot.task_queue.load_settings", return_value=settings),
            patch("qums_bot.task_queue.init_monitoring", return_value=True) as init_mock,
            patch("qums_bot.task_queue.build_runtime", return_value=runtime) as build_runtime_mock,
            patch("qums_bot.task_queue.capture_monitoring_exception") as capture_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "job failed"):
                task_queue.execute_dispatched_task("run_due_checks")

        init_mock.assert_called_once_with(settings, component="worker-job")
        build_runtime_mock.assert_called_once_with(settings)
        capture_mock.assert_called_once()
        self.assertTrue(capture_mock.call_args.kwargs["flush"])

    def test_run_worker_initializes_monitoring_before_starting_worker(self) -> None:
        settings = make_settings(
            self.tmp / "bot.sqlite3",
            sentry_dsn="https://example@sentry.invalid/1",
            task_queue_mode="rq",
        )

        with (
            patch("qums_bot.task_queue.load_settings", return_value=settings),
            patch("qums_bot.task_queue.init_monitoring", return_value=True) as init_mock,
            patch("qums_bot.task_queue.Queue", object()),
            patch("qums_bot.task_queue.Redis") as redis_mock,
            patch("qums_bot.task_queue.Worker") as worker_mock,
        ):
            redis_conn = object()
            redis_mock.from_url.return_value = redis_conn

            task_queue.run_worker()

        init_mock.assert_called_once_with(settings, component="worker")
        redis_mock.from_url.assert_called_once_with(settings.redis_url)
        worker_mock.assert_called_once_with([settings.task_queue_name], connection=redis_conn)
        worker_mock.return_value.work.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
