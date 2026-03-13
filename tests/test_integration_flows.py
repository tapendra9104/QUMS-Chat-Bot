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

from twilio.request_validator import RequestValidator

from qums_bot.app import create_app
from qums_bot.config import Settings, load_settings
from qums_bot.errors import AppConfigurationError, StudentValidationError
from qums_bot.erp_client import AuthenticationRequired, ERPClient
from qums_bot.models import PendingLogin
from qums_bot.parsers import extract_login_state, parse_attendance_summary, parse_timetable_slots
from qums_bot.telegram import TelegramSender
from qums_bot.telegram import TelegramError
from qums_bot.whatsapp import WhatsAppSender


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


class FakeFetchedMessage:
    def __init__(self, sid: str, status: str = "sent") -> None:
        self.sid = sid
        self.status = status
        self.error_code = None
        self.error_message = None


class FakeMessagesApi:
    def __init__(self) -> None:
        self.created_payloads: list[dict[str, str]] = []

    def create(self, **kwargs):
        self.created_payloads.append(kwargs)
        return FakeFetchedMessage(f"SM{len(self.created_payloads)}", status="queued")

    def __call__(self, sid: str):
        class Resource:
            def __init__(self, sid_value: str) -> None:
                self.sid_value = sid_value

            def fetch(self):
                return FakeFetchedMessage(self.sid_value, status="sent")

        return Resource(sid)

    def list(self, limit: int = 50):
        return []


class FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = FakeMessagesApi()


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


class IntegrationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qums-integration-"))

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

    def test_whatsapp_sender_attaches_status_callback(self) -> None:
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
            twilio_account_sid="sid",
            twilio_auth_token="token",
            twilio_whatsapp_mode="sandbox",
            twilio_whatsapp_from="whatsapp:+14155238886",
            twilio_sandbox_join_code="demo-code",
            twilio_status_message_limit=50,
            twilio_status_callback_url="",
            twilio_content_sid_default="",
            twilio_content_sid_morning="",
            twilio_content_sid_attendance="",
        )
        sender = WhatsAppSender(settings)
        sender._client = FakeTwilioClient()

        sender.send_text("+911234567890", "Test delivery", message_kind="attendance")

        payload = sender._client.messages.created_payloads[0]
        self.assertEqual(payload["status_callback"], "https://bot.example.com/webhooks/twilio/status")

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
            telegram_bot_token="token",
            telegram_api_base_url="https://api.telegram.org",
            telegram_admin_chat_ids=("5570554765",),
            telegram_poll_interval_seconds=5,
        )
        sender = TelegramSender(settings)
        sender._session = FakeTelegramSession()

        sender.send_text("5570554765", "Plain Telegram test")

        self.assertEqual(len(sender._session.calls), 1)
        payload = sender._session.calls[0]["json"]
        self.assertNotIn("reply_markup", payload)

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
        client = ERPClient(settings)
        session = FakeERPSession(FakeERPResponse(status_code=403, text="Forbidden"))

        with self.assertRaises(AuthenticationRequired):
            client._post_json(session, "/Account/GetStudentDetail", {})

    def test_twilio_status_webhook_requires_valid_signature_and_updates_history(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "PUBLIC_BASE_URL": "https://bot.example.com",
            "TWILIO_ACCOUNT_SID": "sid",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
            "TWILIO_WHATSAPP_MODE": "sandbox",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.whatsapp._client = FakeTwilioClient()
            student_id = service.save_student(
                student_id=None,
                student_label="Webhook Student",
                user_name="demo",
                password="password",
                whatsapp_number="+911234567890",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            student = service.get_student(student_id)
            assert student is not None
            service._send_whatsapp(
                student,
                "Attendance Update\n\nStatus: Present",
                message_kind="attendance",
                history_category="attendance_update",
                idempotency_key="attendance_update:webhook-test",
            )
            history = service.list_message_history()
            self.assertEqual(len(history), 1)
            provider_sid = history[0].provider_sid

            form_data = {
                "MessageSid": provider_sid,
                "MessageStatus": "delivered",
                "ErrorCode": "",
                "ErrorMessage": "",
            }
            validator = RequestValidator("token")
            signature = validator.compute_signature(
                "https://bot.example.com/webhooks/twilio/status",
                form_data,
            )

            bad_response = client.post("/webhooks/twilio/status", data=form_data)
            self.assertEqual(bad_response.status_code, 403)

            good_response = client.post(
                "/webhooks/twilio/status",
                data=form_data,
                headers={"X-Twilio-Signature": signature},
            )
            self.assertEqual(good_response.status_code, 204)

            updated = service.list_message_history()[0]
            self.assertEqual(updated.delivery_status, "delivered")

    def test_twilio_inbound_webhook_supports_help_command(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "PUBLIC_BASE_URL": "https://bot.example.com",
            "TWILIO_ACCOUNT_SID": "sid",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
            "TWILIO_WHATSAPP_MODE": "sandbox",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Inbound Student",
                user_name="demo",
                password="password",
                whatsapp_number="+911234567890",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            form_data = {
                "From": "whatsapp:+911234567890",
                "Body": "help",
            }
            validator = RequestValidator("token")
            signature = validator.compute_signature(
                "https://bot.example.com/webhooks/twilio/inbound",
                form_data,
            )

            response = client.post(
                "/webhooks/twilio/inbound",
                data=form_data,
                headers={"X-Twilio-Signature": signature},
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("QUMS Bot Commands", response.get_data(as_text=True))

    def test_twilio_inbound_webhook_matches_canonical_whatsapp_numbers(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "0",
            "PUBLIC_BASE_URL": "https://bot.example.com",
            "TWILIO_ACCOUNT_SID": "sid",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
            "TWILIO_WHATSAPP_MODE": "sandbox",
        }
        with env_context(env_values):
            app = create_app(start_scheduler=False)
            client = app.test_client()
            service = app.config["service"]
            service.save_student(
                student_id=None,
                student_label="Canonical Student",
                user_name="demo_canonical",
                password="password",
                whatsapp_number="whatsapp: +91 98765 43210",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            form_data = {
                "From": "whatsapp:+919876543210",
                "Body": "help",
            }
            validator = RequestValidator("token")
            signature = validator.compute_signature(
                "https://bot.example.com/webhooks/twilio/inbound",
                form_data,
            )

            response = client.post(
                "/webhooks/twilio/inbound",
                data=form_data,
                headers={"X-Twilio-Signature": signature},
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("QUMS Bot Commands", response.get_data(as_text=True))

    def test_healthz_reports_runtime_scheduler_state(self) -> None:
        db_path = self.tmp / "bot.sqlite3"
        env_values = {
            "DATABASE_PATH": str(db_path),
            "APP_SECRET": "secret",
            "USE_WAITRESS": "0",
            "RUN_SCHEDULER": "1",
            "TWILIO_ACCOUNT_SID": "sid",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
            "TWILIO_WHATSAPP_MODE": "sandbox",
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

    def test_admin_login_rejects_external_next_path(self) -> None:
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
                self.assertNotIn("stale_key", session_state)

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
                    "whatsapp_number": "+911234567890",
                    "timezone": "Asia/Kolkata",
                    "enabled": "on",
                },
            )
            self.assertEqual(bad_response.status_code, 400)

            csrf_token = self._extract_csrf_token(client.get("/").get_data(as_text=True))
            good_response = client.post(
                "/students",
                data={
                    "csrf_token": csrf_token,
                    "student_label": "With Token",
                    "user_name": "withtoken",
                    "password": "password",
                    "whatsapp_number": "+911234567890",
                    "timezone": "Asia/Kolkata",
                    "enabled": "on",
                },
            )
            self.assertEqual(good_response.status_code, 302)

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
                whatsapp_number="+911234567890",
                telegram_chat_id="5570554765",
                email_address="",
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
                whatsapp_number="+911234567890",
                telegram_chat_id="5570554765",
                email_address="",
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
                whatsapp_number="+911234567890",
                telegram_chat_id="5570554765",
                email_address="",
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
                "Twilio sandbox is not ready. The message `join <code>` must be sent manually from the recipient's WhatsApp again.",
            )
            service.db.mark_student_erp_sync(student_id, synced_at="2026-03-13T11:00:00+00:00")

            html = client.get("/").get_data(as_text=True)

            self.assertIn("ERP session: ERP session saved.", html)
            self.assertIn("Last ERP sync at:", html)
            self.assertIn("Last bot action at:", html)
            self.assertIn("Recent bot activity: Twilio sandbox is not ready.", html)
            self.assertNotIn("Status: Twilio sandbox is not ready.", html)

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
                whatsapp_number="+911234567891",
                telegram_chat_id="",
                email_address="",
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
                whatsapp_number="+911234567892",
                telegram_chat_id="5570554765",
                email_address="",
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
                channel="whatsapp",
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
                whatsapp_number="+911234567894",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(25):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="whatsapp",
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
                whatsapp_number="+911234567893",
                telegram_chat_id="",
                email_address="",
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
                whatsapp_number="+911234567895",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(505):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="whatsapp",
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
                whatsapp_number="+911234567896",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(12):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="telegram" if index % 2 else "whatsapp",
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
                whatsapp_number="+911234567897",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            service.db.insert_message_history(
                student_id=student_id,
                channel="whatsapp",
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
                channel="whatsapp",
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
            self.assertIn("Unique live body", payload["message_history_html"])
            self.assertIn("Retry Title", payload["dead_letter_html"])
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
        self.assertEqual(settings.monitor_poll_interval_minutes, 1)
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
                whatsapp_number="+911234567898",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )
            for index in range(205):
                service.db.insert_message_history(
                    student_id=student_id,
                    channel="whatsapp",
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
                            "whatsapp_number": "+911234567899",
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
                        "whatsapp_number": "+911234567899",
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
