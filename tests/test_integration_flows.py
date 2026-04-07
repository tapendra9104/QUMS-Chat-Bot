from __future__ import annotations

import os
import re
import requests
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from qums_bot.app import create_app
from qums_bot.config import Settings, load_settings
from qums_bot.db import Database
from qums_bot.errors import AppConfigurationError, StudentValidationError
from qums_bot.erp_client import AuthenticationRequired, ERPClient
from qums_bot.models import PendingLogin
from qums_bot.parsers import extract_login_state, parse_attendance_summary, parse_timetable_slots
from qums_bot.telegram import TelegramSender
from qums_bot.telegram import TelegramError


def env_context(values: dict[str, str]):
    class EnvContext:
        def __enter__(self_inner):
            self_inner.original = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                os.environ[key] = value

        def __exit__(self_inner, exc_type, exc, tb):
            for key, original in self_inner.original.items():
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original

    return EnvContext()


class FakeTelegramResponse:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


class FakeTelegramSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, *, json=None, data=None, files=None, timeout=15):
        self.calls.append(
            {
                "url": url,
                "json": json,
                "data": data,
                "files": files,
                "timeout": timeout,
            }
        )
        return FakeTelegramResponse({"ok": True, "result": {"message_id": 99}})


class FakeERPResponse:
    def __init__(self, *, status_code: int, text: str = "", json_payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._json_payload = json_payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._json_payload


class FakeERPSession:
    def __init__(self, response: FakeERPResponse) -> None:
        self.response = response

    def request(self, method, url, **kwargs):
        return self.response


class SequenceERPSession:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class IntegrationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = Path("tmp-test2")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_root / self.id().replace(".", "_")
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _extract_csrf_token(self, html: str) -> str:
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        self.assertIsNotNone(match)
        return str(match.group(1))

    def test_extract_login_state_from_real_fixture(self) -> None:
        html = Path("qums_login_live.html").read_text(encoding="utf-8")

        state = extract_login_state(html)

        self.assertTrue(state["request_verification_token"])
        self.assertEqual(state["hdn_msg"], "QGC")
        self.assertEqual(state["check_online"], "0")
        self.assertEqual(state["client_ip"], "~~~~~")
        self.assertTrue(state["captcha_data_url"].startswith("data:image/png;base64,"))

    def test_parse_realistic_timetable_and_attendance_payloads(self) -> None:
        timetable_payload = {
            "state": [
                {
                    "Period": "09:00 - 10:00",
                    "Subject": "Mathematics",
                    "Employee": "Prof A",
                    "Time": "09:00 - 10:00",
                },
                {
                    "Period": "10:00 - 11:00",
                    "Subject": "Physics Lab",
                    "Employee": "Prof B",
                    "Time": "10:00 - 11:00",
                },
            ]
        }
        attendance_payload = {
            "state": [
                {
                    "Subject": "Mathematics",
                    "SubjectCode": "MATH101",
                    "EMPNAME": "Prof A",
                    "TotalLecture": "12",
                    "TotalPresent": "11",
                    "Percentage": "91.67%",
                },
                {
                    "Subject": "Physics Lab",
                    "SubjectCode": "PHYL102",
                    "EMPNAME": "Prof B",
                    "TotalLecture": "8",
                    "TotalPresent": "7",
                    "Percentage": "87.50%",
                },
            ]
        }

        slots = parse_timetable_slots(timetable_payload, target_date=date(2026, 3, 13))
        attendance = parse_attendance_summary(attendance_payload)

        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0].subject_name, "Mathematics")
        self.assertEqual(slots[1].teacher_name, "Prof B")
        self.assertEqual(len(attendance), 2)
        self.assertEqual(attendance[0].subject_key, "math101")
        self.assertEqual(attendance[1].total_present, 7)

    def test_parse_holiday_timetable_rows_as_no_class(self) -> None:
        timetable_payload = {
            "state": [
                {
                    "Period": "09:00 - 10:00",
                    "Subject": "Holiday",
                    "Employee": "",
                    "Time": "09:00 - 10:00",
                }
            ]
        }

        slots = parse_timetable_slots(timetable_payload, target_date=date(2026, 3, 13))

        self.assertEqual(len(slots), 1)
        self.assertTrue(slots[0].is_break)
        self.assertEqual(slots[0].subject_name, "Holiday")
        self.assertIn("Holiday", slots[0].note)

    def test_telegram_sender_omits_null_reply_markup_for_plain_text(self) -> None:
        settings = Settings(
            base_url="https://example.com",
            database_path=self.tmp / "bot.sqlite3",
            app_secret="secret",
            app_env="development",
            use_waitress=False,
            waitress_threads=8,
            dashboard_auto_refresh_seconds=30,
            run_scheduler=False,
            task_queue_mode="inline",
            redis_url="",
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
            public_base_url="https://bot.example.com",
            webhook_rate_limit_count=60,
            webhook_rate_limit_window_seconds=60,
            admin_rate_limit_count=10,
            admin_rate_limit_window_seconds=60,
            sentry_dsn="",
            sentry_traces_sample_rate=0.0,
            telegram_bot_token="token",
            telegram_api_base_url="https://api.telegram.org",
            telegram_admin_chat_ids=("5570554765",),
            telegram_poll_interval_seconds=5,
            lecture_schedule_poll_interval_seconds=30,
        )
        sender = TelegramSender(settings)
        sender._session = FakeTelegramSession()

        sender.send_text("5570554765", "Plain Telegram test")

        self.assertEqual(len(sender._session.calls), 1)
        payload = sender._session.calls[0]["json"]
        self.assertNotIn("reply_markup", payload)

    def test_telegram_sender_rejects_username_delivery_targets(self) -> None:
        settings = Settings(
            base_url="https://example.com",
            database_path=self.tmp / "bot.sqlite3",
            app_secret="secret",
            app_env="development",
            use_waitress=False,
            waitress_threads=8,
            dashboard_auto_refresh_seconds=30,
            run_scheduler=False,
            task_queue_mode="inline",
            redis_url="",
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
            public_base_url="https://bot.example.com",
            webhook_rate_limit_count=60,
            webhook_rate_limit_window_seconds=60,
            admin_rate_limit_count=10,
            admin_rate_limit_window_seconds=60,
            sentry_dsn="",
            sentry_traces_sample_rate=0.0,
            telegram_bot_token="token",
            telegram_api_base_url="https://api.telegram.org",
            telegram_admin_chat_ids=("5570554765",),
            telegram_poll_interval_seconds=5,
            lecture_schedule_poll_interval_seconds=30,
        )
        sender = TelegramSender(settings)

        with self.assertRaises(TelegramError):
            sender.send_text("@QUMS_ALERT_BOT", "Plain Telegram test")

    def test_erp_client_maps_403_json_endpoint_to_authentication_required(self) -> None:
        settings = Settings(
            base_url="https://example.com",
            database_path=self.tmp / "bot.sqlite3",
            app_secret="secret",
            app_env="development",
            use_waitress=False,
            waitress_threads=8,
            dashboard_auto_refresh_seconds=30,
            run_scheduler=False,
            task_queue_mode="inline",
            redis_url="",
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
            public_base_url="https://bot.example.com",
            webhook_rate_limit_count=60,
            webhook_rate_limit_window_seconds=60,
            admin_rate_limit_count=10,
            admin_rate_limit_window_seconds=60,
            sentry_dsn="",
            sentry_traces_sample_rate=0.0,
        )
        client = ERPClient(settings)
        session = FakeERPSession(FakeERPResponse(status_code=403, text="Forbidden"))

        with self.assertRaises(AuthenticationRequired):
            client._post_json(session, "/Account/GetStudentDetail", {})

    def test_healthz_reports_runtime_scheduler_state(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "1",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            payload = client.get("/healthz").get_json()

            self.assertTrue(payload["run_scheduler_configured"])
            self.assertFalse(payload["scheduler_active"])

    def test_healthz_uses_scheduler_running_state_not_object_truthiness(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "1",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            class FakeScheduler:
                def __init__(self, running: bool) -> None:
                    self.running = running

            app.config["scheduler"] = FakeScheduler(running=False)
            self.assertFalse(client.get("/healthz").get_json()["scheduler_active"])

            app.config["scheduler"] = FakeScheduler(running=True)
            self.assertTrue(client.get("/healthz").get_json()["scheduler_active"])

    def test_erp_client_retries_transient_timeout_and_normalizes_timeout_tuple(self) -> None:
        settings = Settings(
            base_url="https://example.com",
            database_path=self.tmp / "bot.sqlite3",
            app_secret="secret",
            app_env="development",
            use_waitress=False,
            waitress_threads=8,
            dashboard_auto_refresh_seconds=30,
            run_scheduler=False,
            task_queue_mode="inline",
            redis_url="",
            task_queue_name="qums-bot",
            admin_username="",
            admin_password="",
            admin_telegram_username="",
            local_timezone="Asia/Kolkata",
            morning_digest_time="06:30",
            evening_report_time="19:00",
            attendance_poll_interval_minutes=1,
            substitution_poll_interval_minutes=1,
            monitor_poll_interval_minutes=1,
            sandbox_expiry_warning_minutes=10,
            lecture_grace_minutes=20,
            attendance_correction_lookback_days=14,
            attendance_shortage_buffer_lectures=1,
            delivery_retry_limit=3,
            delivery_retry_backoff_seconds=1,
            low_attendance_thresholds=(75, 70, 65),
            flask_host="127.0.0.1",
            flask_port=5000,
            public_base_url="https://bot.example.com",
            webhook_rate_limit_count=60,
            webhook_rate_limit_window_seconds=60,
            admin_rate_limit_count=10,
            admin_rate_limit_window_seconds=60,
            sentry_dsn="",
            sentry_traces_sample_rate=0.0,
        )
        client = ERPClient(settings)
        session = SequenceERPSession(
            [
                requests.ConnectTimeout("first timeout"),
                FakeERPResponse(status_code=200, text='{"ok":true}', json_payload={"ok": True}),
            ]
        )

        with patch("qums_bot.erp_client.time.sleep", return_value=None) as sleep_mock:
            response = client._request(
                session,
                "post",
                "https://example.com/test",
                context="ERP test endpoint",
                timeout=30,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["kwargs"]["timeout"], (10.0, 30.0))
        sleep_mock.assert_called_once_with(1.0)

    def test_admin_login_rejects_external_next_path(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_ADMIN_CHAT_IDS": "",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))

            response = client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "https://example.com/steal-session",
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")

    def test_root_shows_public_dashboard_with_login_and_application_when_admin_auth_enabled(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_ADMIN_CHAT_IDS": "",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            response = client.get("/")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Create Application", html)
            self.assertIn(">Login<", html)
            self.assertIn("https://t.me/QUMS_ALERT_BOT", html)
            self.assertIn("@userinfo3bot", html)
            self.assertNotIn("Admin Security", html)
            self.assertNotIn(">Save Student<", html)

    def test_public_root_shows_prototype_only_and_owner_contacts(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "OWNER_TELEGRAM_CONTACT": "@gunda872",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Hidden Student",
                user_name="hidden_erp",
                password="erp-pass-123",
                site_login_username="hidden-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            response = client.get("/")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("prototype view only", html)
            self.assertIn("@gunda872", html)
            self.assertNotIn("+919634549096", html)
            self.assertIn("Create Application", html)
            self.assertNotIn("Hidden Student", html)

    def test_public_live_data_stays_in_prototype_mode_without_real_students(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "OWNER_TELEGRAM_CONTACT": "@gunda872",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Hidden Student",
                user_name="hidden_erp",
                password="erp-pass-123",
                site_login_username="hidden-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            response = client.get("/dashboard/live-data")
            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertIn("Prototype Preview", payload["student_cards_html"])
            self.assertIn("@gunda872", payload["student_cards_html"])
            self.assertNotIn("Hidden Student", payload["student_cards_html"])

    def test_login_page_renders_student_login_and_admin_link(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_ADMIN_CHAT_IDS": "",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            response = client.get("/login")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("QUMS Bot", html)
            self.assertIn("Student Sign In", html)
            self.assertIn("/admin/login", html)
            self.assertIn("Forgot Password?", html)
            self.assertNotIn("Forgot your admin password?", html)

    def test_admin_login_page_uses_shared_qums_bot_branding(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            response = client.get("/admin/login")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("QUMS Bot", html)
            self.assertIn("Admin Sign In", html)
            self.assertIn("Forgot your password?", html)
            self.assertIn("Reset via Telegram", html)

    def test_public_application_submission_saves_request_and_sends_telegram_notification(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            fake_session = FakeTelegramSession()
            service.telegram._session = fake_session

            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            response = client.post(
                "/applications",
                data={
                    "csrf_token": csrf_token,
                    "applicant_name": "Tapendra Chaudhary",
                    "student_label": "Tapendra",
                    "user_name": "23030682",
                    "password": "erp-password",
                    "site_login_username": "tapendra-site",
                    "site_login_password": "site-pass-123",
                    "reg_id": "8027",
                    "telegram_chat_id": "5570554766",
                    "timezone": "Asia/Kolkata",
                    "note": "Please add my profile for attendance alerts.",
                },
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)
            requests = service.list_application_requests()

            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].student_label, "Tapendra")
            self.assertEqual(requests[0].user_name, "23030682")
            self.assertEqual(requests[0].site_login_username, "tapendra-site")
            self.assertIn("Your website account is active", html)
            self.assertEqual(len(fake_session.calls), 1)
            telegram_payload = fake_session.calls[0]["json"]
            self.assertEqual(telegram_payload["chat_id"], "5570554765")
            self.assertIn("New student application request", telegram_payload["text"])
            self.assertIn("Tapendra Chaudhary", telegram_payload["text"])
            self.assertIn("Website login username: tapendra-site", telegram_payload["text"])
            self.assertIn("ERP password: Submitted securely in the website application.", telegram_payload["text"])
            self.assertNotIn("erp-password", telegram_payload["text"])

    def test_pending_application_user_can_sign_in_but_dashboard_stays_locked(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.submit_application_request(
                applicant_name="Pending Student",
                student_label="Pending",
                user_name="23030001",
                password="erp-password",
                site_login_username="pending-user",
                site_login_password="pending-pass-123",
                telegram_chat_id="5570554766",
                timezone="Asia/Kolkata",
                reg_id="9001",
                note="Please approve soon.",
                created_from_ip="website",
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            response = client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "pending-user",
                    "password": "pending-pass-123",
                    "next_path": "/",
                },
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Application Access Dashboard", html)
            self.assertIn("student features remain unavailable until an administrator approves the request", html)
            self.assertNotIn("Student Control Dashboard", html)
            self.assertNotIn("Edit Profile", html)
            self.assertNotIn("Open Login", html)

    def test_pending_application_session_upgrades_to_student_dashboard_after_admin_acceptance(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            service = app.config["service"]
            service.submit_application_request(
                applicant_name="Approved Student",
                student_label="Approved",
                user_name="23030002",
                password="erp-password",
                site_login_username="approved-user",
                site_login_password="approved-pass-123",
                telegram_chat_id="5570554766",
                timezone="Asia/Kolkata",
                reg_id="9002",
                note="Please approve soon.",
                created_from_ip="website",
            )

            pending_client = app.test_client()
            login_html = pending_client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            pending_login_response = pending_client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "approved-user",
                    "password": "approved-pass-123",
                    "next_path": "/",
                },
                follow_redirects=True,
            )
            self.assertIn("Application Access Dashboard", pending_login_response.get_data(as_text=True))

            admin_client = app.test_client()
            admin_login_html = admin_client.get("/admin/login").get_data(as_text=True)
            admin_csrf = self._extract_csrf_token(admin_login_html)
            admin_client.post(
                "/admin/login",
                data={
                    "csrf_token": admin_csrf,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )
            dashboard_html = admin_client.get("/").get_data(as_text=True)
            accept_csrf = self._extract_csrf_token(dashboard_html)
            admin_client.post(
                "/applications/1/accept",
                data={
                    "csrf_token": accept_csrf,
                    "site_login_username": "approved-user",
                    "site_login_password": "",
                },
                follow_redirects=True,
            )

            upgraded_response = pending_client.get("/", follow_redirects=True)
            upgraded_html = upgraded_response.get_data(as_text=True)

            self.assertEqual(upgraded_response.status_code, 200)
            self.assertIn("Student Control Dashboard", upgraded_html)
            self.assertIn("Edit Profile", upgraded_html)
            self.assertNotIn("Application Access Dashboard", upgraded_html)

    def test_admin_dashboard_shows_application_request_credentials_for_admin_review(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.submit_application_request(
                applicant_name="Tapendra Chaudhary",
                student_label="Tapendra",
                user_name="23030682",
                password="erp-password",
                site_login_username="tapendra-site",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554766",
                timezone="Asia/Kolkata",
                reg_id="8027",
                note="Please add my profile for attendance alerts.",
                created_from_ip="website",
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            html = client.get("/").get_data(as_text=True)

            self.assertIn("Submitted ERP password", html)
            self.assertIn("erp-password", html)
            self.assertIn('name="site_login_username"', html)
            self.assertIn('name="site_login_password"', html)
            self.assertIn('value="tapendra-site"', html)
            self.assertIn("The applicant already created a website password during signup.", html)

    def test_admin_can_accept_application_request_from_dashboard(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            fake_session = FakeTelegramSession()
            service.telegram._session = fake_session

            service.submit_application_request(
                applicant_name="Tapendra Chaudhary",
                student_label="Tapendra",
                user_name="23030682",
                password="erp-password",
                site_login_username="tapendra-site",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554766",
                timezone="Asia/Kolkata",
                reg_id="8027",
                note="Please add my profile for attendance alerts.",
                created_from_ip="website",
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            self.assertIn("Approve Application", dashboard_html)
            accept_csrf = self._extract_csrf_token(dashboard_html)
            fake_session.calls.clear()

            response = client.post(
                "/applications/1/accept",
                data={
                    "csrf_token": accept_csrf,
                    "site_login_username": "tapendra-web",
                    "site_login_password": "site-pass-789",
                },
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)
            student = service.get_student_by_site_login_username("tapendra-web")
            application = service.get_application_request(1)

            self.assertEqual(response.status_code, 200)
            self.assertIsNotNone(student)
            assert student is not None
            self.assertEqual(student.student_label, "Tapendra")
            self.assertEqual(student.user_name, "23030682")
            self.assertEqual(student.site_login_username, "tapendra-web")
            self.assertEqual(student.telegram_chat_id, "5570554766")
            self.assertEqual(student.reg_id, "8027")
            self.assertIsNotNone(application)
            assert application is not None
            self.assertEqual(application.status, "accepted")
            self.assertIn("Application approved.", html)
            self.assertIn("username tapendra-web", html)
            self.assertIn("website password site-pass-789", html)
            texts = [str(call.get("json", {}).get("text") or "") for call in fake_session.calls]
            chats = [str(call.get("json", {}).get("chat_id") or "") for call in fake_session.calls]
            self.assertIn("5570554766", chats)
            self.assertIn("5570554765", chats)
            self.assertTrue(any("Your QUMS application has been approved." in text for text in texts))
            self.assertTrue(any("Website password: site-pass-789" in text for text in texts))
            self.assertTrue(any("Application approved from website dashboard." in text for text in texts))
            self.assertTrue(any("removed from the pending Telegram review queue" in text for text in texts))

            student_client = app.test_client()
            login_html = student_client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            login_response = student_client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "tapendra-web",
                    "password": "site-pass-789",
                    "next_path": "/",
                },
                follow_redirects=True,
            )

            self.assertEqual(login_response.status_code, 200)

    def test_admin_dashboard_shows_explicit_telegram_delivery_failures_on_application_approval(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.telegram = type("DisabledTelegram", (), {"configured": False})()

            service.submit_application_request(
                applicant_name="Tapendra Chaudhary",
                student_label="Tapendra",
                user_name="23030682",
                password="erp-password",
                site_login_username="tapendra-site",
                site_login_password="site-pass-123",
                telegram_chat_id="",
                timezone="Asia/Kolkata",
                reg_id="8027",
                note="Please add my profile for attendance alerts.",
                created_from_ip="website",
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            accept_csrf = self._extract_csrf_token(dashboard_html)

            response = client.post(
                "/applications/1/accept",
                data={
                    "csrf_token": accept_csrf,
                    "site_login_username": "tapendra-web",
                    "site_login_password": "site-pass-789",
                },
                follow_redirects=True,
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Application approved.", html)
            self.assertIn("Student Telegram notification was not sent: Telegram bot is not configured.", html)
            self.assertIn("Admin Telegram notification was not sent: Telegram bot is not configured.", html)

    def test_admin_can_reject_application_request_from_dashboard(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            fake_session = FakeTelegramSession()
            service.telegram._session = fake_session

            service.submit_application_request(
                applicant_name="Tapendra Chaudhary",
                student_label="Tapendra",
                user_name="23030682",
                password="erp-password",
                site_login_username="tapendra-site",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554766",
                timezone="Asia/Kolkata",
                reg_id="8027",
                note="Please add my profile for attendance alerts.",
                created_from_ip="website",
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            self.assertIn("Reject Application", dashboard_html)
            reject_csrf = self._extract_csrf_token(dashboard_html)
            fake_session.calls.clear()

            response = client.post(
                "/applications/1/reject",
                data={"csrf_token": reject_csrf},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)
            application = service.get_application_request(1)

            self.assertEqual(response.status_code, 200)
            self.assertIsNotNone(application)
            assert application is not None
            self.assertEqual(application.status, "rejected")
            self.assertEqual(service.list_students(), [])
            self.assertIn("Application rejected.", html)
            self.assertIn("All reviewed", html)
            self.assertIn("0 approved and 1 rejected", html)
            self.assertIn("Rejected / Reviewed", html)
            self.assertNotIn("1 awaiting review", html)
            texts = [str(call.get("json", {}).get("text") or "") for call in fake_session.calls]
            chats = [str(call.get("json", {}).get("chat_id") or "") for call in fake_session.calls]
            self.assertIn("5570554766", chats)
            self.assertTrue(any("Your QUMS application has been reviewed and was not approved." in text for text in texts))

            student_client = app.test_client()
            login_html = student_client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            login_response = student_client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "tapendra-site",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
                follow_redirects=True,
            )
            rejected_html = login_response.get_data(as_text=True)

            self.assertEqual(login_response.status_code, 200)
            self.assertIn("Application Closed", rejected_html)
            self.assertIn("reviewed and closed", rejected_html)
            self.assertNotIn("Application Under Review", rejected_html)

    def test_admin_can_clear_reviewed_application_request_from_dashboard(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]

            service.submit_application_request(
                applicant_name="Tapendra Chaudhary",
                student_label="Tapendra",
                user_name="23030682",
                password="erp-password",
                site_login_username="tapendra-site",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554766",
                timezone="Asia/Kolkata",
                reg_id="8027",
                note="Please add my profile for attendance alerts.",
                created_from_ip="website",
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            reject_csrf = self._extract_csrf_token(dashboard_html)
            client.post(
                "/applications/1/reject",
                data={"csrf_token": reject_csrf},
                follow_redirects=True,
            )

            reviewed_html = client.get("/").get_data(as_text=True)
            self.assertIn("Clear Request", reviewed_html)
            clear_csrf = self._extract_csrf_token(reviewed_html)

            response = client.post(
                "/applications/1/clear",
                data={"csrf_token": clear_csrf},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIsNone(service.get_application_request(1))
            self.assertIn("Application record for Tapendra was cleared from the dashboard.", html)
            self.assertIn("No public applications have been submitted yet.", html)
            self.assertNotIn("Tapendra Chaudhary", html)

    def test_database_init_creates_missing_parent_directories(self) -> None:
        db_path = self.tmp / "nested" / "data" / "bot.sqlite3"

        db = Database(db_path)
        db.init()

        self.assertTrue(db_path.parent.exists())
        self.assertTrue(db_path.exists())

    def test_admin_login_clears_stale_session_state(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            with client.session_transaction() as session_state:
                session_state["stale_key"] = "stale"

            response = client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            self.assertEqual(response.status_code, 302)
            with client.session_transaction() as session_state:
                self.assertTrue(session_state.get("admin_authenticated"))
                self.assertEqual(session_state.get("admin_username"), "admin")
                self.assertNotIn("stale_key", session_state)

    def test_dashboard_shows_admin_session_panel_after_login(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))

            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )
            response = client.get("/")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Dashboard Access", html)
            self.assertIn("admin", html)
            self.assertIn("/login", html)
            self.assertIn("Admin Security", html)
            self.assertIn("Logout", html)
            self.assertIn("@userinfo3bot", html)
            self.assertNotIn("Create Application", html)

    def test_student_login_shows_scoped_dashboard_without_admin_sections(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Scoped Student",
                user_name="scoped_erp",
                password="erp-pass-123",
                site_login_username="scoped-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(login_html)
            response = client.post(
                "/login",
                data={
                    "csrf_token": csrf_token,
                    "login_username": "scoped-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Account Security", html)
            self.assertIn("Edit Profile", html)
            self.assertIn("Message History", html)
            self.assertIn("Dead-Letter Queue", html)
            self.assertIn("scoped-user", html)
            self.assertIn("@userinfo3bot", html)
            self.assertNotIn("Admin Security", html)
            self.assertNotIn("Add Student", html)
            self.assertNotIn("Create Application", html)
            self.assertNotIn("Admin Audit Log", html)
            self.assertNotIn("Delete Profile", html)

    def test_student_profile_update_saves_changes_and_notifies_admin(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            fake_session = FakeTelegramSession()
            app.config["service"].telegram._session = fake_session
            client = app.test_client()
            student_id = app.config["service"].save_student(
                student_id=None,
                student_label="Editable Student",
                user_name="editable_erp",
                password="erp-pass-123",
                site_login_username="editable-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": csrf_token,
                    "login_username": "editable-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            update_csrf = self._extract_csrf_token(dashboard_html)
            response = client.post(
                "/student/profile/update",
                data={
                    "csrf_token": update_csrf,
                    "student_label": "Editable Student Updated",
                    "user_name": "editable_erp_2",
                    "password": "",
                    "telegram_chat_id": "5570554777",
                    "timezone": "UTC",
                },
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)
            student = app.config["service"].get_student(student_id)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Profile updated successfully. The admin dashboard and admin Telegram notification have been updated.", html)
            self.assertIsNotNone(student)
            assert student is not None
            self.assertEqual(student.student_label, "Editable Student Updated")
            self.assertEqual(student.user_name, "editable_erp_2")
            self.assertEqual(student.telegram_chat_id, "5570554777")
            self.assertEqual(student.timezone, "UTC")
            self.assertIn("Student profile self-service update:", student.last_bot_activity_text or "")
            self.assertIn("Telegram chat id: 5570554766 -> 5570554777", student.last_bot_activity_text or "")
            texts = [str(call.get("json", {}).get("text") or "") for call in fake_session.calls]
            self.assertTrue(any("Student profile updated from the self-service dashboard" in text for text in texts))
            self.assertTrue(any("Editable Student Updated" in text for text in texts))
            self.assertTrue(any("Telegram chat id: 5570554766 -> 5570554777" in text for text in texts))
            self.assertTrue(any("Admin dashboard status: synchronized automatically" in text for text in texts))

            admin_client = app.test_client()
            admin_login_html = admin_client.get("/admin/login").get_data(as_text=True)
            admin_csrf = self._extract_csrf_token(admin_login_html)
            admin_client.post(
                "/admin/login",
                data={
                    "csrf_token": admin_csrf,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )
            admin_html = admin_client.get("/").get_data(as_text=True)
            self.assertIn("5570554777", admin_html)
            self.assertIn("Telegram chat id: 5570554766 -&gt; 5570554777", admin_html)

    def test_student_dashboard_history_and_dead_letter_are_scoped(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            own_id = service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            other_id = service.save_student(
                student_id=None,
                student_label="Other Student",
                user_name="other_erp",
                password="erp-pass-123",
                site_login_username="other-user",
                site_login_password="site-pass-456",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.db.insert_message_history(
                student_id=own_id,
                channel="telegram",
                recipient="5570554765",
                category="attendance_update",
                message_kind="attendance",
                provider_sid="MSG-OWN",
                title="Own Message",
                body="Own body",
                idempotency_key="own-msg",
                delivery_status="delivered",
            )
            service.db.insert_message_history(
                student_id=other_id,
                channel="telegram",
                recipient="5570554766",
                category="attendance_update",
                message_kind="attendance",
                provider_sid="MSG-OTHER",
                title="Other Message",
                body="Other body",
                idempotency_key="other-msg",
                delivery_status="delivered",
            )
            service.db.claim_outbound_message(
                idempotency_key="own-dead",
                student_id=own_id,
                channel="telegram",
                recipient="5570554765",
                category="attendance_update",
                message_kind="attendance",
                title="Own Dead Letter",
                body="Own dead body",
            )
            service.db.mark_outbound_message_failed(
                "own-dead",
                "Own dead error",
                retry_limit=0,
                retry_backoff_seconds=60,
            )
            service.db.claim_outbound_message(
                idempotency_key="other-dead",
                student_id=other_id,
                channel="telegram",
                recipient="5570554766",
                category="attendance_update",
                message_kind="attendance",
                title="Other Dead Letter",
                body="Other dead body",
            )
            service.db.mark_outbound_message_failed(
                "other-dead",
                "Other dead error",
                retry_limit=0,
                retry_backoff_seconds=60,
            )

            login_html = client.get("/login").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": csrf_token,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            html = client.get("/").get_data(as_text=True)
            self.assertIn("Own Message", html)
            self.assertIn("Own Dead Letter", html)
            self.assertNotIn("Other Message", html)
            self.assertNotIn("Other Dead Letter", html)
            self.assertNotIn('action="/dead-letter/own-dead/retry"', html)

    def test_student_dashboard_shows_only_the_logged_in_students_profile(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.save_student(
                student_id=None,
                student_label="Other Student",
                user_name="other_erp",
                password="erp-pass-456",
                site_login_username="other-user",
                site_login_password="site-pass-456",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(login_html)
            response = client.post(
                "/login",
                data={
                    "csrf_token": csrf_token,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Own Student", html)
            self.assertNotIn("Other Student", html)
            self.assertIn("Login username: <code>own-user</code>", html)

    def test_student_dashboard_can_send_manual_substitution_report_for_own_profile(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            self.assertIn("Send Manual Substitution Report", dashboard_html)
            csrf_token = self._extract_csrf_token(dashboard_html)
            called: list[int] = []

            def fake_send_substitution(student_id_arg, force=False):
                called.append(student_id_arg)
                return "ok"

            service.send_substitution_report = fake_send_substitution

            response = client.post(
                f"/students/{student_id}/send-substitution-report",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(called, [student_id])
            self.assertIn("Manual substitution report sent to configured channels.", html)

    def test_student_dashboard_shows_full_manual_action_menu(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)

            self.assertIn('href="#student-profile-panel">Edit Profile</a>', dashboard_html)
            self.assertIn("Start ERP Login", dashboard_html)
            self.assertIn("Open Captcha", dashboard_html)
            self.assertIn("Preview Today", dashboard_html)
            self.assertIn("Send Attendance Summary", dashboard_html)
            self.assertIn("Send Morning Summary", dashboard_html)
            self.assertIn("Send Manual Substitution Report", dashboard_html)
            self.assertIn("Send Day Report", dashboard_html)
            self.assertIn("Send Shortage Report", dashboard_html)
            self.assertIn("Send Channel Test", dashboard_html)

    def test_student_dashboard_can_send_attendance_summary_for_own_profile(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(dashboard_html)
            called: list[int] = []

            def fake_send_attendance_summary(student_id_arg, force=False):
                called.append(student_id_arg)

            service.send_attendance_summary_report = fake_send_attendance_summary

            response = client.post(
                f"/students/{student_id}/send-attendance-summary",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(called, [student_id])
            self.assertIn("Attendance summary report sent to configured channels.", html)

    def test_student_dashboard_attendance_summary_auth_expiry_notifies_student_and_admin_on_telegram(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            fake_session = FakeTelegramSession()
            service.telegram._session = fake_session
            student_id = service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.db.update_student_session(
                student_id=student_id,
                cookies_json="[]",
                last_login_status="ERP session active.",
            )

            def fail_attendance_summary(student):
                raise AuthenticationRequired("expired")

            service.erp.get_attendance_summary = fail_attendance_summary

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(dashboard_html)
            response = client.post(
                f"/students/{student_id}/send-attendance-summary",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("ERP session expired. Open the dashboard and complete login again with a fresh captcha.", html)
            self.assertEqual(len(fake_session.calls), 2)
            chats = [str(call.get("json", {}).get("chat_id") or "") for call in fake_session.calls]
            texts = [str(call.get("json", {}).get("text") or "") for call in fake_session.calls]
            self.assertIn("5570554765", chats)
            self.assertIn("5570554766", chats)
            self.assertTrue(any("Status: Your ERP session has expired" in text for text in texts))
            self.assertTrue(any("Status: The student's ERP session has expired" in text for text in texts))
            self.assertTrue(any("Own Student" in text for text in texts))

    def test_student_dashboard_cannot_send_manual_substitution_report_for_other_profile(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            other_id = service.save_student(
                student_id=None,
                student_label="Other Student",
                user_name="other_erp",
                password="erp-pass-456",
                site_login_username="other-user",
                site_login_password="site-pass-456",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(dashboard_html)
            response = client.post(
                f"/students/{other_id}/send-substitution-report",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("You can only use actions for your own student profile.", html)

    def test_student_dashboard_cannot_send_attendance_summary_for_other_profile(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            other_id = service.save_student(
                student_id=None,
                student_label="Other Student",
                user_name="other_erp",
                password="erp-pass-456",
                site_login_username="other-user",
                site_login_password="site-pass-456",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(dashboard_html)
            response = client.post(
                f"/students/{other_id}/send-attendance-summary",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("You can only use actions for your own student profile.", html)

    def test_student_dashboard_can_open_captcha_for_own_profile(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.db.save_pending_login(
                PendingLogin(
                    student_id=student_id,
                    request_verification_token="token",
                    hdn_msg="QGC",
                    check_online="0",
                    client_ip="127.0.0.1",
                    captcha_data_url="data:image/png;base64,abc",
                    cookies_json="[]",
                    created_at="2026-03-13T12:00:00+00:00",
                )
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            response = client.get(f"/students/{student_id}/login")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Manual ERP Login", html)
            self.assertIn("Own Student", html)

    def test_student_dashboard_live_data_shows_only_the_logged_in_students_profile(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Own Student",
                user_name="own_erp",
                password="erp-pass-123",
                site_login_username="own-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.save_student(
                student_id=None,
                student_label="Other Student",
                user_name="other_erp",
                password="erp-pass-456",
                site_login_username="other-user",
                site_login_password="site-pass-456",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": csrf_token,
                    "login_username": "own-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )

            response = client.get("/dashboard/live-data")
            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertIn("Own Student", payload["student_cards_html"])
            self.assertNotIn("Other Student", payload["student_cards_html"])

    def test_admin_dashboard_edit_query_opens_the_selected_student_form(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Editable Admin Student",
                user_name="editable_admin_erp",
                password="erp-pass-123",
                site_login_username="editable-admin-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            response = client.get(f"/?edit={student_id}")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Edit Student", html)
            self.assertIn('value="Editable Admin Student"', html)

    def test_admin_can_update_student_controls_from_dashboard(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Controlled Student",
                user_name="controlled_erp",
                password="erp-pass-123",
                site_login_username="controlled-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            update_csrf = self._extract_csrf_token(dashboard_html)
            response = client.post(
                f"/students/{student_id}/controls",
                data={
                    "csrf_token": update_csrf,
                    "notification_channel_mode": "telegram_only",
                    "disabled_actions": ["send_morning", "send_channel_test"],
                },
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)
            student = service.get_student(student_id)

            self.assertEqual(response.status_code, 200)
            self.assertIsNotNone(student)
            assert student is not None
            self.assertFalse(student.enabled)
            self.assertEqual(student.notification_channel_mode, "telegram_only")
            self.assertEqual(
                service.get_student_disabled_actions(student),
                {"send_morning", "send_channel_test"},
            )
            self.assertIn("Student controls updated.", html)
            self.assertIn("Blocked", html)
            self.assertIn("Telegram Only", html)

    def test_blocked_student_send_route_is_rejected_from_admin_dashboard(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Blocked Student",
                user_name="blocked_erp",
                password="erp-pass-123",
                site_login_username="blocked-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.update_student_controls(
                student_id=student_id,
                enabled=False,
                notification_channel_mode="all",
                disabled_actions=[],
            )

            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            action_csrf = self._extract_csrf_token(dashboard_html)
            response = client.post(
                f"/students/{student_id}/send-morning",
                data={"csrf_token": action_csrf},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("This student profile is blocked.", html)

    def test_student_forgot_password_reset_sends_code_and_telegram_notifications(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            fake_session = FakeTelegramSession()
            app.config["service"].telegram._session = fake_session
            client = app.test_client()
            app.config["service"].save_student(
                student_id=None,
                student_label="Reset Student",
                user_name="reset_erp",
                password="erp-pass-123",
                site_login_username="reset-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            forgot_html = client.get("/forgot-password").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(forgot_html)
            request_response = client.post(
                "/forgot-password/request",
                data={"csrf_token": csrf_token, "login_username": "reset-user"},
            )
            self.assertEqual(request_response.status_code, 200)
            self.assertTrue(fake_session.calls)

            code_text = str(fake_session.calls[-1]["json"]["text"])
            reset_code = re.search(r"Code:\s*(\d{6})", code_text).group(1)
            reset_response = client.post(
                "/forgot-password/reset",
                data={
                    "csrf_token": csrf_token,
                    "login_username": "reset-user",
                    "reset_code": reset_code,
                    "new_password": "new-site-pass-123",
                    "confirm_password": "new-site-pass-123",
                },
            )

            self.assertEqual(reset_response.status_code, 302)
            self.assertEqual(reset_response.headers["Location"], "/login")
            self.assertGreaterEqual(len(fake_session.calls), 3)
            texts = [str(call.get("json", {}).get("text") or "") for call in fake_session.calls]
            self.assertTrue(any("QUMS Bot password updated" in text for text in texts))
            self.assertTrue(any("Student website password changed" in text for text in texts))

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            login_response = client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "reset-user",
                    "password": "new-site-pass-123",
                    "next_path": "/",
                },
            )
            self.assertEqual(login_response.status_code, 302)

    def test_signed_in_student_changes_password_with_telegram_code(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            fake_session = FakeTelegramSession()
            app.config["service"].telegram._session = fake_session
            client = app.test_client()
            app.config["service"].save_student(
                student_id=None,
                student_label="Change Student",
                user_name="change_erp",
                password="erp-pass-123",
                site_login_username="change-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            login_html = client.get("/login").get_data(as_text=True)
            login_csrf = self._extract_csrf_token(login_html)
            client.post(
                "/login",
                data={
                    "csrf_token": login_csrf,
                    "login_username": "change-user",
                    "password": "site-pass-123",
                    "next_path": "/",
                },
            )
            dashboard_html = client.get("/").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(dashboard_html)
            request_response = client.post(
                "/student/password/request",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            self.assertEqual(request_response.status_code, 200)
            code_text = str(fake_session.calls[-1]["json"]["text"])
            reset_code = re.search(r"Code:\s*(\d{6})", code_text).group(1)

            change_response = client.post(
                "/student/password/change",
                data={
                    "csrf_token": csrf_token,
                    "reset_code": reset_code,
                    "new_password": "changed-site-pass-123",
                    "confirm_password": "changed-site-pass-123",
                },
                follow_redirects=True,
            )
            html = change_response.get_data(as_text=True)

            self.assertEqual(change_response.status_code, 200)
            self.assertIn("Your website password has been changed.", html)
            texts = [str(call.get("json", {}).get("text") or "") for call in fake_session.calls]
            self.assertTrue(any("QUMS Bot password updated" in text for text in texts))
            self.assertTrue(any("Student website password changed" in text for text in texts))

            logout_csrf = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            client.post("/logout", data={"csrf_token": logout_csrf})
            relogin_html = client.get("/login").get_data(as_text=True)
            relogin_csrf = self._extract_csrf_token(relogin_html)
            relogin_response = client.post(
                "/login",
                data={
                    "csrf_token": relogin_csrf,
                    "login_username": "change-user",
                    "password": "changed-site-pass-123",
                    "next_path": "/",
                },
            )
            self.assertEqual(relogin_response.status_code, 302)

    def test_admin_delete_student_sends_telegram_notification(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            fake_session = FakeTelegramSession()
            app.config["service"].telegram._session = fake_session
            client = app.test_client()
            student_id = app.config["service"].save_student(
                student_id=None,
                student_label="Delete Student",
                user_name="delete_erp",
                password="erp-pass-123",
                site_login_username="delete-user",
                site_login_password="site-pass-123",
                telegram_chat_id="5570554766",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            admin_login_html = client.get("/admin/login").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(admin_login_html)
            client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            dashboard_html = client.get("/").get_data(as_text=True)
            delete_csrf = self._extract_csrf_token(dashboard_html)
            response = client.post(
                f"/students/{student_id}/delete",
                data={"csrf_token": delete_csrf},
                follow_redirects=True,
            )
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("Student profile deleted.", html)
            texts = [str(call.get("json", {}).get("text") or "") for call in fake_session.calls]
            self.assertTrue(any("Student profile deleted" in text for text in texts))
            self.assertTrue(any("Delete Student" in text for text in texts))

    def test_admin_account_update_changes_login_credentials_and_recovery_username(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            login_csrf = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            login_response = client.post(
                "/admin/login",
                data={
                    "csrf_token": login_csrf,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )
            self.assertEqual(login_response.status_code, 302)

            dashboard_html = client.get("/").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(dashboard_html)
            update_response = client.post(
                "/admin/account/update",
                data={
                    "csrf_token": csrf_token,
                    "login_username": "ops-admin",
                    "recovery_telegram_username": "@gunda872",
                    "current_password": "password",
                    "new_password": "new-pass-123",
                    "confirm_password": "new-pass-123",
                },
            )
            self.assertEqual(update_response.status_code, 302)

            logout_csrf = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            client.post("/admin/logout", data={"csrf_token": logout_csrf})

            relogin_csrf = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            bad_response = client.post(
                "/admin/login",
                data={
                    "csrf_token": relogin_csrf,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )
            self.assertEqual(bad_response.status_code, 401)

            relogin_csrf = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            good_response = client.post(
                "/admin/login",
                data={
                    "csrf_token": relogin_csrf,
                    "username": "ops-admin",
                    "password": "new-pass-123",
                    "next_path": "/",
                },
            )
            self.assertEqual(good_response.status_code, 302)

            self.assertEqual(app.config["service"].db.get_runtime_state("admin_username_override"), "ops-admin")
            self.assertEqual(
                app.config["service"].db.get_runtime_state("admin_telegram_username_override"),
                "@gunda872",
            )

    def test_forgot_password_reset_sends_code_to_telegram_and_accepts_new_credentials(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "ADMIN_TELEGRAM_USERNAME": "@gunda872",
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ADMIN_CHAT_IDS": "5570554765",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            fake_session = FakeTelegramSession()
            app.config["service"].telegram._session = fake_session
            client = app.test_client()

            forgot_html = client.get("/admin/forgot-password").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(forgot_html)
            request_response = client.post(
                "/admin/forgot-password/request",
                data={
                    "csrf_token": csrf_token,
                    "telegram_username": "@gunda872",
                },
            )
            self.assertEqual(request_response.status_code, 200)
            self.assertTrue(fake_session.calls)

            message_payload = fake_session.calls[-1]["json"] or {}
            message_text = str(message_payload.get("text") or "")
            code_match = re.search(r"Code:\s*(\d{6})", message_text)
            self.assertIsNotNone(code_match)
            reset_code = code_match.group(1)

            reset_response = client.post(
                "/admin/forgot-password/reset",
                data={
                    "csrf_token": csrf_token,
                    "telegram_username": "@gunda872",
                    "reset_code": reset_code,
                    "new_username": "recovered-admin",
                    "new_password": "recovered-pass-123",
                    "confirm_password": "recovered-pass-123",
                },
            )
            self.assertEqual(reset_response.status_code, 302)
            self.assertEqual(reset_response.headers["Location"], "/admin/login")

            login_csrf = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            login_response = client.post(
                "/admin/login",
                data={
                    "csrf_token": login_csrf,
                    "username": "recovered-admin",
                    "password": "recovered-pass-123",
                    "next_path": "/",
                },
            )
            self.assertEqual(login_response.status_code, 302)

    def test_dashboard_post_requires_csrf_token(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            bad_response = client.post(
                "/students",
                data={
                    "student_label": "No Token",
                    "user_name": "notoken",
                    "password": "password",
                    "timezone": "Asia/Kolkata",
                    "enabled": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(bad_response.status_code, 302)
            self.assertEqual(bad_response.headers["Location"], "/")

            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            good_response = client.post(
                "/students",
                data={
                    "csrf_token": csrf_token,
                    "student_label": "With Token",
                    "user_name": "withtoken",
                    "password": "password",
                    "timezone": "Asia/Kolkata",
                    "enabled": "on",
                },
            )
            self.assertEqual(good_response.status_code, 302)

    def test_invalid_csrf_for_ajax_post_returns_reload_hint(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            response = client.post(
                "/students",
                data={"student_label": "No Token"},
                headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
            )
            self.assertEqual(response.status_code, 400)
            payload = response.get_json()
            self.assertIsInstance(payload, dict)
            self.assertTrue(payload.get("reload"))
            self.assertIn("session expired", str(payload.get("message", "")).lower())

    def test_authenticated_dashboard_live_data_returns_reauth_hint_when_session_is_missing(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            response = client.get(
                "/dashboard/live-data",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                    "X-Dashboard-Auth-Role": "admin",
                },
            )

            self.assertEqual(response.status_code, 401)
            payload = response.get_json()
            self.assertIsInstance(payload, dict)
            self.assertTrue(payload.get("reload"))
            self.assertIn("/admin/login", str(payload.get("login_url", "")))
            self.assertIn("sign in again", str(payload.get("message", "")).lower())

    def test_admin_only_ajax_request_returns_reauth_hint_before_csrf_validation(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            response = client.post(
                "/dead-letter/test-message/retry",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
            )

            self.assertEqual(response.status_code, 401)
            payload = response.get_json()
            self.assertIsInstance(payload, dict)
            self.assertTrue(payload.get("reload"))
            self.assertIn("/admin/login", str(payload.get("login_url", "")))
            self.assertIn("admin sign-in required", str(payload.get("message", "")).lower())

    def test_html_pages_disable_caching(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            response = client.get("/admin/login")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store, no-cache, must-revalidate, max-age=0")
            self.assertEqual(response.headers.get("Pragma"), "no-cache")
            self.assertEqual(response.headers.get("Expires"), "0")

    def test_favicon_route_serves_static_asset(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()

            response = client.get("/favicon.ico", follow_redirects=True)
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/svg+xml")
                self.assertIn("<svg", response.get_data(as_text=True))
            finally:
                response.close()

    def test_http_production_deploy_does_not_force_secure_session_cookie(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "APP_ENV": "production",
            "USE_WAITRESS": "1",
            "RUN_SCHEDULER": "0",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
            "PUBLIC_BASE_URL": "http://45.196.196.19",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            self.assertFalse(app.config["SESSION_COOKIE_SECURE"])

            client = app.test_client()
            csrf_token = self._extract_csrf_token(client.get("/admin/login").get_data(as_text=True))
            response = client.post(
                "/admin/login",
                data={
                    "csrf_token": csrf_token,
                    "username": "admin",
                    "password": "password",
                    "next_path": "/",
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")

    def test_https_public_base_url_enables_secure_session_cookie(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "APP_ENV": "production",
            "USE_WAITRESS": "1",
            "RUN_SCHEDULER": "0",
            "PUBLIC_BASE_URL": "https://bot.example.com",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_send_morning_route_handles_telegram_delivery_failure(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Morning Route Student",
                user_name="route_user",
                password="password",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))

            def fail_send_morning(*args, **kwargs):
                raise TelegramError("telegram delivery failed")

            service.send_morning_update = fail_send_morning

            response = client.post(
                f"/students/{student_id}/send-morning",
                data={"csrf_token": csrf_token},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")

    def test_send_attendance_summary_route_handles_telegram_delivery_failure(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Attendance Route Student",
                user_name="attendance_route_user",
                password="password",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            dashboard_html = client.get("/").get_data(as_text=True)
            self.assertIn("Send Attendance Summary", dashboard_html)
            csrf_token = self._extract_csrf_token(dashboard_html)

            def fail_send_attendance_summary(*args, **kwargs):
                raise TelegramError("telegram delivery failed")

            service.send_attendance_summary_report = fail_send_attendance_summary

            response = client.post(
                f"/students/{student_id}/send-attendance-summary",
                data={"csrf_token": csrf_token},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")

    def test_send_substitution_report_route_handles_telegram_delivery_failure(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "TELEGRAM_BOT_TOKEN": "token",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Substitution Route Student",
                user_name="substitution_route_user",
                password="password",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            dashboard_html = client.get("/").get_data(as_text=True)
            self.assertIn("Send Manual Substitution Report", dashboard_html)
            csrf_token = self._extract_csrf_token(dashboard_html)

            def fail_send_substitution_report(*args, **kwargs):
                raise TelegramError("telegram delivery failed")

            service.send_substitution_report = fail_send_substitution_report

            response = client.post(
                f"/students/{student_id}/send-substitution-report",
                data={"csrf_token": csrf_token},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")

    def test_dashboard_separates_erp_session_from_recent_bot_activity(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Status Student",
                user_name="status_user",
                password="password",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.db.update_student_session(
                student_id=student_id,
                cookies_json='[{"name":"session","value":"ok"}]',
                last_login_status="ERP sync completed for 2026-03-13.",
                reg_id="8027",
                student_name="Status Student",
            )
            service.db.update_student_status(
                student_id,
                "Telegram delivery is not configured for this student profile.",
            )
            service.db.mark_student_erp_sync(student_id, synced_at="2026-03-13T11:00:00+00:00")

            html = client.get("/").get_data(as_text=True)

            self.assertIn("ERP session: ERP session saved.", html)
            self.assertIn("Last ERP sync at:", html)
            self.assertIn("Last bot action at:", html)
            self.assertIn("Recent bot activity: Telegram delivery is not configured", html)
            self.assertNotIn("Status: Telegram delivery is not configured", html)

    def test_dashboard_only_links_open_captcha_when_pending_login_exists(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Captcha Student",
                user_name="captcha_user",
                password="password",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )

            html = client.get("/").get_data(as_text=True)
            self.assertIn('Open Captcha</span>', html)
            self.assertNotIn(f'/students/{student_id}/login">Open Captcha</a>', html)

            service.db.save_pending_login(
                PendingLogin(
                    student_id=student_id,
                    request_verification_token="token",
                    hdn_msg="QGC",
                    check_online="0",
                    client_ip="127.0.0.1",
                    captcha_data_url="data:image/png;base64,abc",
                    cookies_json="[]",
                    created_at="2026-03-13T12:00:00+00:00",
                )
            )

            html = client.get("/").get_data(as_text=True)
            self.assertIn(f'/students/{student_id}/login">Open Captcha</a>', html)
            self.assertIn("Captcha entry pending", html)

    def test_dashboard_message_history_and_audit_log_filters(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Filter Student",
                user_name="filter_user",
                password="password",
                telegram_chat_id="5570554765",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.db.insert_message_history(
                student_id=student_id,
                channel="telegram",
                recipient="5570554765",
                category="attendance_summary_report",
                message_kind="attendance",
                provider_sid="tg-1",
                title="Attendance Summary Report",
                body="Unique attendance body",
                idempotency_key="summary-1",
                delivery_status="accepted",
            )
            service.db.insert_message_history(
                student_id=student_id,
                channel="telegram",
                recipient="+911234567892",
                category="morning_summary",
                message_kind="morning",
                provider_sid="wa-1",
                title="Morning Schedule Update",
                body="Different morning body",
                idempotency_key="morning-1",
                delivery_status="accepted",
            )
            service.db.insert_admin_audit_log(
                actor="dashboard",
                action="retry_dead_letter_message",
                target_type="outbound_message",
                target_id="retry-1",
                details="Unique retry action",
            )
            service.db.insert_admin_audit_log(
                actor="dashboard",
                action="save_student",
                target_type="student",
                target_id=str(student_id),
                details="Other action",
            )

            html = client.get(
                "/?message_q=Unique+attendance&message_channel=telegram&message_category=attendance_summary_report"
                "&audit_q=Unique+retry&audit_action=retry_dead_letter_message"
            ).get_data(as_text=True)

            self.assertIn("Unique attendance body", html)
            self.assertNotIn("Different morning body", html)
            self.assertIn("Unique retry action", html)
            self.assertNotIn("Other action", html)

    def test_dashboard_message_history_and_audit_log_pagination(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Page Student",
                user_name="page_user",
                password="password",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(25):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="telegram",
                    recipient="+911234567894",
                    category="attendance_update",
                    message_kind="attendance",
                    provider_sid=f"wa-{index}",
                    title=f"Message {index}",
                    body=f"Body {index}",
                    idempotency_key=f"message-{index}",
                    delivery_status="accepted",
                )
                service.db.insert_admin_audit_log(
                    actor="dashboard",
                    action="preview_today",
                    target_type="student",
                    target_id=str(student_id),
                    details=f"Audit {index}",
                )

            html = client.get("/?message_page=2&audit_page=2").get_data(as_text=True)

            self.assertIn("Message 4", html)
            self.assertNotIn("Message 24", html)
            self.assertIn("Page 2 of 2", html)
            self.assertIn("Audit 4", html)
            self.assertNotIn("Audit 24", html)

    def test_dead_letter_retry_route_retries_message_from_dashboard(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Retry Route Student",
                user_name="retry_route_user",
                password="password",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            dashboard_html = client.get("/").get_data(as_text=True)
            csrf_token = self._extract_csrf_token(dashboard_html)

            service.retry_dead_letter_message = lambda key: f"Dead-letter message retried successfully for {key}."

            response = client.post(
                "/dead-letter/attendance_update:route-test/retry",
                data={"csrf_token": csrf_token},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")

    def test_dead_letter_retry_route_returns_json_for_ajax_requests(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.retry_dead_letter_message = lambda key: f"Dead-letter message retried successfully for {key}."
            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))

            response = client.post(
                "/dead-letter/attendance_update:ajax-test/retry",
                data={"csrf_token": csrf_token},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertIn("retried successfully", payload["message"].lower())
            self.assertEqual(payload["idempotency_key"], "attendance_update:ajax-test")
            self.assertIn("queue", payload)

    def test_csv_exports_include_full_history_not_just_first_500_rows(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Export Student",
                user_name="export_user",
                password="password",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(505):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="telegram",
                    recipient="+911234567895",
                    category="attendance_update",
                    message_kind="attendance",
                    provider_sid=f"wa-export-{index}",
                    title=f"Message {index}",
                    body=f"Body {index}",
                    idempotency_key=f"message-export-{index}",
                    delivery_status="accepted",
                )
                service.db.insert_admin_audit_log(
                    actor="dashboard",
                    action="save_student",
                    target_type="student",
                    target_id=str(student_id),
                    details=f"Audit {index}",
                )

            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            message_csv = client.post(
                "/exports/message-history.csv",
                data={"csrf_token": csrf_token},
            ).get_data(as_text=True)
            audit_csv = client.post(
                "/exports/audit-log.csv",
                data={"csrf_token": csrf_token},
            ).get_data(as_text=True)

            self.assertIn("Message 504", message_csv)
            self.assertEqual(len(message_csv.strip().splitlines()), 506)
            self.assertIn("Audit 504", audit_csv)
            self.assertGreaterEqual(len(audit_csv.strip().splitlines()), 506)

    def test_csv_exports_support_paginated_post_scope(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Export Page Student",
                user_name="export_page_user",
                password="password",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(12):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="telegram",
                    recipient="5570554765" if index % 2 else "+911234567896",
                    category="attendance_update",
                    message_kind="attendance",
                    provider_sid=f"wa-page-{index}",
                    title=f"Message {index}",
                    body=f"Body {index}",
                    idempotency_key=f"message-page-{index}",
                    delivery_status="accepted",
                )
                service.db.insert_admin_audit_log(
                    actor="dashboard",
                    action="preview_today",
                    target_type="student",
                    target_id=str(student_id),
                    details=f"Audit {index}",
                )

            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            message_csv = client.post(
                "/exports/message-history.csv",
                data={
                    "csrf_token": csrf_token,
                    "page": 2,
                    "per_page": 5,
                },
            ).get_data(as_text=True)
            audit_csv = client.post(
                "/exports/audit-log.csv",
                data={
                    "csrf_token": csrf_token,
                    "audit_action": "preview_today",
                    "page": 3,
                    "per_page": 4,
                },
            ).get_data(as_text=True)

            self.assertIn("Message 6", message_csv)
            self.assertIn("Message 2", message_csv)
            self.assertNotIn("Message 11", message_csv)
            self.assertEqual(len(message_csv.strip().splitlines()), 6)
            self.assertIn("Audit 3", audit_csv)
            self.assertIn("Audit 0", audit_csv)
            self.assertNotIn("Audit 11", audit_csv)
            self.assertEqual(len(audit_csv.strip().splitlines()), 5)

    def test_dashboard_live_data_endpoint_refreshes_message_history_and_dead_letter_queue(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Live Data Student",
                user_name="live_data_user",
                password="password",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.db.insert_message_history(
                student_id=student_id,
                channel="telegram",
                recipient="+911234567897",
                category="attendance_update",
                message_kind="attendance",
                provider_sid="wa-live-1",
                title="Attendance Update",
                body="Unique live body",
                idempotency_key="live-message-1",
                delivery_status="accepted",
            )
            claimed = service.db.claim_outbound_message(
                idempotency_key="live-dead-letter-1",
                student_id=student_id,
                channel="telegram",
                recipient="+911234567897",
                category="attendance_update",
                message_kind="attendance",
                title="Retry Title",
                body="Retry body",
            )
            self.assertTrue(claimed)
            service.db.mark_outbound_message_failed(
                "live-dead-letter-1",
                "Permanent failure",
                retry_limit=1,
                retry_backoff_seconds=1,
            )

            response = client.get("/dashboard/live-data?message_q=Unique+live")

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertIn("hero_live_grid_html", payload)
            self.assertIn("student_cards_html", payload)
            self.assertIn("action_center_html", payload)
            self.assertIn("Dashboard Time", payload["hero_live_grid_html"])
            self.assertIn("Unique live body", payload["message_history_html"])
            self.assertIn("Retry Title", payload["dead_letter_html"])
            self.assertIn("audit_log_html", payload)
            self.assertIn("Live Data Student", payload["student_cards_html"])
            self.assertEqual(payload["outbound_summary"]["dead_letter"], 1)

    def test_load_settings_sanitizes_invalid_operational_env_values(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "WAITRESS_THREADS": "0",
            "DASHBOARD_AUTO_REFRESH_SECONDS": "-10",
            "LOCAL_TIMEZONE": "Mars/Olympus",
            "ATTENDANCE_POLL_INTERVAL_MINUTES": "0",
            "SUBSTITUTION_POLL_INTERVAL_MINUTES": "-5",
            "MONITOR_POLL_INTERVAL_MINUTES": "abc",
            "SANDBOX_EXPIRY_WARNING_MINUTES": "0",
            "LECTURE_GRACE_MINUTES": "-1",
            "ATTENDANCE_CORRECTION_LOOKBACK_DAYS": "0",
            "DELIVERY_RETRY_BACKOFF_SECONDS": "0",
            "FLASK_PORT": "99999",
            "SENTRY_TRACES_SAMPLE_RATE": "2.5",
        }
        with env_context(env_values):
            settings = load_settings()

        self.assertEqual(settings.waitress_threads, 1)
        self.assertEqual(settings.dashboard_auto_refresh_seconds, 0)
        self.assertEqual(settings.local_timezone, "Asia/Kolkata")
        self.assertEqual(settings.attendance_poll_interval_minutes, 1)
        self.assertEqual(settings.substitution_poll_interval_minutes, 1)
        self.assertEqual(settings.monitor_poll_interval_minutes, 10)
        self.assertEqual(settings.sandbox_expiry_warning_minutes, 1)
        self.assertEqual(settings.lecture_grace_minutes, 0)
        self.assertEqual(settings.attendance_correction_lookback_days, 1)
        self.assertEqual(settings.delivery_retry_backoff_seconds, 1)
        self.assertEqual(settings.flask_port, 65535)
        self.assertEqual(settings.sentry_traces_sample_rate, 1.0)

    def test_load_settings_rejects_default_secret_in_production(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_ENV": "production",
            "APP_SECRET": "change-this-secret",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "secret",
        }
        with env_context(env_values):
            with self.assertRaises(AppConfigurationError):
                load_settings()

    def test_load_settings_rejects_missing_admin_auth_in_production(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_ENV": "production",
            "APP_SECRET": "real-secret",
            "ADMIN_USERNAME": "",
            "ADMIN_PASSWORD": "",
        }
        with env_context(env_values):
            with self.assertRaises(AppConfigurationError):
                load_settings()

    def test_dashboard_history_sql_pagination_reaches_records_beyond_first_200_rows(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            student_id = service.save_student(
                student_id=None,
                student_label="Large History Student",
                user_name="large_history_user",
                password="password",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(205):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="telegram",
                    recipient="+911234567898",
                    category="attendance_update",
                    message_kind="attendance",
                    provider_sid=f"wa-large-{index}",
                    title=f"Message {index}",
                    body=f"Body {index}",
                    idempotency_key=f"message-large-{index}",
                    delivery_status="accepted",
                )
                service.db.insert_admin_audit_log(
                    actor="dashboard",
                    action="preview_today",
                    target_type="student",
                    target_id=str(student_id),
                    details=f"Audit {index}",
                )

            html = client.get("/?message_page=11&audit_page=11").get_data(as_text=True)

            self.assertIn("Message 4", html)
            self.assertIn("Audit 4", html)
            self.assertIn("Page 11 of 11", html)

    def test_save_student_route_logs_unexpected_errors(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            with patch.object(app.config["service"], "save_student", side_effect=RuntimeError("db down")):
                with patch.object(app.logger, "exception") as logger_exception:
                    response = client.post(
                        "/students",
                        data={
                            "csrf_token": csrf_token,
                            "student_label": "Student",
                            "user_name": "user",
                            "password": "password",
                            "telegram_chat_id": "",
                            "timezone": "Asia/Kolkata",
                            "enabled": "on",
                        },
                    )

            self.assertEqual(response.status_code, 500)
            self.assertIn("internal error", response.get_data(as_text=True).lower())
            logger_exception.assert_called_once()

    def test_save_student_route_returns_validation_errors_as_400(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            with patch.object(app.config["service"], "save_student", side_effect=StudentValidationError("Invalid timezone: Mars/Olympus")):
                response = client.post(
                    "/students",
                    data={
                        "csrf_token": csrf_token,
                        "student_label": "Student",
                        "user_name": "user",
                        "password": "password",
                        "telegram_chat_id": "",
                        "timezone": "Mars/Olympus",
                        "enabled": "on",
                    },
                )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Invalid timezone: Mars/Olympus", response.get_data(as_text=True))

    def test_admin_audit_logging_failures_are_logged(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            with patch.object(app.config["service"], "log_admin_action", side_effect=RuntimeError("audit unavailable")):
                with patch.object(app.logger, "exception") as logger_exception:
                    response = client.post(
                        "/exports/message-history.csv",
                        data={"csrf_token": csrf_token},
                    )

            self.assertEqual(response.status_code, 200)
            logger_exception.assert_called_once()


if __name__ == "__main__":
    unittest.main()



