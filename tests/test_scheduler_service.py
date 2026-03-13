from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from qums_bot.config import Settings
from qums_bot.db import Database
from qums_bot.erp_client import AuthenticationRequired
from qums_bot.models import LectureSlot
from qums_bot.parsers import parse_attendance_summary
from qums_bot.scheduler import build_scheduler
from qums_bot.service import BotService, NotificationDeliveryError
from qums_bot.task_queue import TaskDispatcher
from qums_bot.whatsapp import WhatsAppChannelStatus


def make_settings(db_path: Path) -> Settings:
    return Settings(
        base_url="https://example.com",
        database_path=db_path,
        app_secret="secret",
        app_env="development",
        use_waitress=False,
        waitress_threads=8,
        dashboard_auto_refresh_seconds=30,
        run_scheduler=True,
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
        public_base_url="https://example.com",
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
        twilio_status_callback_url="https://example.com/webhooks/twilio/status",
        twilio_content_sid_default="",
        twilio_content_sid_morning="",
        twilio_content_sid_attendance="",
        telegram_admin_chat_ids=("5570554765",),
        telegram_poll_interval_seconds=5,
    )


class FakeERP:
    def __init__(
        self,
        *,
        timetable_payload: dict | None = None,
        substitutions_payload: dict | None = None,
        attendance_payload: dict | None = None,
        detail_payload: dict | None = None,
        auth_required: bool = False,
    ) -> None:
        self.timetable_payload = timetable_payload or {"state": []}
        self.substitutions_payload = substitutions_payload or {"state": []}
        self.attendance_payload = attendance_payload or {"state": []}
        self.detail_payload = detail_payload or {"state": [{"StudentName": "Demo Student"}]}
        self.auth_required = auth_required

    def get_timetable(self, student):
        return self.timetable_payload

    def get_substitutions(self, student):
        return self.substitutions_payload

    def get_attendance_summary(self, student):
        return self.attendance_payload

    def get_student_detail(self, student):
        return self.detail_payload

    def ensure_authenticated(self, student):
        if self.auth_required:
            raise AuthenticationRequired("expired")
        return object()


class FakeWhatsApp:
    def __init__(self, status: WhatsAppChannelStatus | None = None) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self._status = status or WhatsAppChannelStatus(
            configured=True,
            mode="sandbox",
            sender="whatsapp:+14155238886",
            ready=True,
            state="sandbox_ready",
            detail="ok",
            join_command="join demo-code",
            last_inbound_at=None,
            sandbox_expires_at=None,
            last_outbound_status=None,
            last_error_code=None,
        )

    def send_text(self, to_number: str, body: str, *, message_kind: str = "generic") -> str:
        self.messages.append((to_number, message_kind, body))
        return f"fake-{len(self.messages)}"

    def get_channel_status(self, to_number: str) -> WhatsAppChannelStatus:
        return self._status


class BrokenWhatsApp(FakeWhatsApp):
    def get_channel_status(self, to_number: str) -> WhatsAppChannelStatus:
        raise RuntimeError("twilio unavailable")


class FlakyWhatsApp(FakeWhatsApp):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    def send_text(self, to_number: str, body: str, *, message_kind: str = "generic") -> str:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("temporary delivery failure")
        return super().send_text(to_number, body, message_kind=message_kind)


class FakeTelegram:
    configured = False

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.documents: list[tuple[str, str, bytes, str | None]] = []
        self.callback_answers: list[tuple[str, str | None, bool]] = []
        self.updates: list[dict] = []
        self.edits: list[tuple[str, str, str]] = []
        self.commands: list[dict[str, str]] = []

    def send_text(self, chat_id: str, body: str, *, message_kind: str = "generic", reply_markup=None) -> str:
        self.messages.append((chat_id, message_kind, body))
        return f"tg-{len(self.messages)}"

    def edit_text(self, *, chat_id: str, message_id: str, body: str, reply_markup=None) -> str:
        self.edits.append((chat_id, message_id, body))
        return str(message_id)

    def send_document(self, *, chat_id: str, filename: str, content_bytes: bytes, caption: str | None = None) -> str:
        self.documents.append((chat_id, filename, content_bytes, caption))
        return f"doc-{len(self.documents)}"

    def get_updates(self, *, offset=None, timeout_seconds: int = 0, allowed_updates=None):
        results = [item for item in self.updates if offset is None or int(item.get("update_id", 0)) >= int(offset)]
        self.updates = []
        return results

    def answer_callback_query(self, *, callback_query_id: str, text: str | None = None, show_alert: bool = False) -> None:
        self.callback_answers.append((callback_query_id, text, show_alert))

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self.commands = list(commands)


class NullEmail:
    configured = False


class DummyService:
    def run_scheduled_dispatch(self):
        return None

    def run_due_checks(self):
        return None

    def run_substitution_sweep(self):
        return None

    def run_monitor_sweep(self):
        return None

    def run_retry_sweep(self):
        return None

    def run_telegram_inbound_sweep(self):
        return None

    def run_telegram_admin_refresh_sweep(self):
        return None


class SchedulerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="qums-tests-"))
        self.db_path = self.tmp / "bot.sqlite3"
        self.db = Database(self.db_path)
        self.db.init()
        self.settings = make_settings(self.db_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_student(self, *, label: str, timezone: str) -> int:
        return self.db.upsert_student(
            student_id=None,
            student_label=label,
            user_name=label.lower().replace(" ", "_"),
            password_encrypted="secret",
            whatsapp_number="+911234567890",
            telegram_chat_id="",
            email_address="",
            enabled=True,
            timezone=timezone,
        )

    def _make_service(
        self,
        *,
        erp: FakeERP | None = None,
        whatsapp: FakeWhatsApp | None = None,
        telegram: FakeTelegram | None = None,
    ) -> BotService:
        return BotService(
            settings=self.settings,
            db=self.db,
            erp_client=erp or FakeERP(),
            whatsapp=whatsapp or FakeWhatsApp(),
            telegram=telegram or FakeTelegram(),
            emailer=NullEmail(),  # type: ignore[arg-type]
        )

    def test_scheduler_registers_expected_jobs(self) -> None:
        scheduler = build_scheduler(self.settings, DummyService())
        jobs = scheduler.get_jobs()
        job_ids = {job.id for job in jobs}
        self.assertEqual(
            job_ids,
            {
                "scheduled-dispatch",
                "attendance-checks",
                "substitution-checks",
                "monitor-checks",
                "delivery-retry-checks",
                "telegram-inbound-checks",
                "telegram-admin-refresh-checks",
            },
        )
        self.assertTrue(scheduler._job_defaults["coalesce"])
        self.assertEqual(scheduler._job_defaults["max_instances"], 1)
        self.assertEqual(scheduler._job_defaults["misfire_grace_time"], 60)

    def test_scheduled_dispatch_honors_student_timezones(self) -> None:
        student_india = self._add_student(label="India Student", timezone="Asia/Kolkata")
        student_utc = self._add_student(label="UTC Student", timezone="UTC")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 10:00", "Subject": "Mathematics", "Employee": "Prof A", "Time": "09:00 - 10:00"}
                ]
            }
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)
        now = datetime(2026, 3, 13, 6, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service.run_scheduled_dispatch(now=now)

        self.assertEqual(len(whatsapp.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].category, "morning_summary")
        self.assertTrue(self.db.has_notification_event(student_india, "morning_digest", "2026-03-13"))
        self.assertFalse(self.db.has_notification_event(student_utc, "morning_digest", "2026-03-13"))

    def test_save_student_normalizes_whatsapp_number(self) -> None:
        service = self._make_service()

        student_id = service.save_student(
            student_id=None,
            student_label="Normalize Student",
            user_name="normalize_user",
            password="secret",
            whatsapp_number="whatsapp: +91 98765-43210",
            telegram_chat_id="",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        student = self.db.get_student(student_id)
        assert student is not None
        self.assertEqual(student.whatsapp_number, "+919876543210")

    def test_save_student_normalizes_telegram_username_url(self) -> None:
        service = self._make_service()

        student_id = service.save_student(
            student_id=None,
            student_label="Telegram Student",
            user_name="telegram_user",
            password="secret",
            whatsapp_number="+919876543210",
            telegram_chat_id="https://t.me/gunda872",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        student = self.db.get_student(student_id)
        assert student is not None
        self.assertEqual(student.telegram_chat_id, "@gunda872")

    def test_send_test_message_dispatches_to_whatsapp_and_telegram(self) -> None:
        student_id = self._add_student(label="Multi Channel Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        self.db.upsert_student(
            student_id=student_id,
            student_label=student.student_label,
            user_name=student.user_name,
            password_encrypted=student.password_encrypted,
            whatsapp_number=student.whatsapp_number,
            telegram_chat_id="123456789",
            email_address="",
            enabled=student.enabled,
            timezone=student.timezone,
        )
        telegram = FakeTelegram()
        telegram.configured = True
        whatsapp = FakeWhatsApp()
        service = self._make_service(whatsapp=whatsapp, telegram=telegram)

        service.send_test_message(student_id)

        self.assertEqual(len(whatsapp.messages), 1)
        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 2)
        self.assertEqual({item.channel for item in history}, {"whatsapp", "telegram"})

    def test_delivery_targets_respect_notification_channel_mode(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Routing Student",
            user_name="routing_user",
            password_encrypted="secret",
            whatsapp_number="+911234567890",
            telegram_chat_id="123456789",
            email_address="",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json="[]",
            timezone="Asia/Kolkata",
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(whatsapp=FakeWhatsApp(), telegram=telegram)

        student = self.db.get_student(student_id)
        assert student is not None
        self.assertEqual(service._delivery_targets(student), [("telegram", "123456789")])

        self.db.update_student_controls(
            student_id=student_id,
            enabled=True,
            notification_channel_mode="paused",
            disabled_actions_json="[]",
        )
        paused_student = self.db.get_student(student_id)
        assert paused_student is not None
        self.assertEqual(service._delivery_targets(paused_student), [])

    def test_save_student_preserves_existing_controls_when_profile_is_edited(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Control Student",
            user_name="control_user",
            password_encrypted="secret",
            whatsapp_number="+911234567890",
            telegram_chat_id="123456789",
            email_address="",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json='["send_morning","send_day_report"]',
            timezone="Asia/Kolkata",
        )
        service = self._make_service()

        service.save_student(
            student_id=student_id,
            student_label="Control Student Updated",
            user_name="control_user_updated",
            password="",
            whatsapp_number="+911234567890",
            telegram_chat_id="123456789",
            email_address="",
            enabled=True,
            timezone="UTC",
        )

        updated_student = self.db.get_student(student_id)
        assert updated_student is not None
        self.assertEqual(updated_student.notification_channel_mode, "telegram_only")
        self.assertEqual(
            service.get_student_disabled_actions(updated_student),
            {"send_morning", "send_day_report"},
        )

    def test_attendance_summary_report_prefers_timetable_faculty_names(self) -> None:
        student_id = self._add_student(label="Attendance Summary Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Day/Period": "Monday",
                        "P1": "Compiler Design(CS36313) (A-010),MD. IQBAL",
                        "P2": "Artificial Intelligence(CS36311) (A-010),AMIT KUMAR",
                    }
                ],
                "col": "Day/Period,P1,P2",
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Compiler Design",
                        "SubjectCode": "CS36313",
                        "EMPNAME": "Wrong Monthly Faculty",
                        "TotalLecture": "19",
                        "TotalPresent": "16",
                        "Percentage": "84.21%",
                    },
                    {
                        "Subject": "Artificial Intelligence",
                        "SubjectCode": "CS36311",
                        "EMPNAME": "Wrong AI Faculty",
                        "TotalLecture": "14",
                        "TotalPresent": "11",
                        "Percentage": "78.57%",
                    },
                ]
            },
        )
        service = self._make_service(erp=erp, whatsapp=FakeWhatsApp(), telegram=FakeTelegram())

        body = service.send_attendance_summary_report(student_id, target_date=date(2026, 3, 16), force=True)

        self.assertIn("Compiler Design (CS36313) | Faculty: MD. IQBAL", body)
        self.assertIn("Artificial Intelligence (CS36311) | Faculty: AMIT KUMAR", body)
        self.assertNotIn("Wrong Monthly Faculty", body)
        self.assertNotIn("Wrong AI Faculty", body)

    def test_send_attendance_summary_report_dispatches_to_whatsapp_and_telegram(self) -> None:
        student_id = self._add_student(label="Attendance Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        self.db.upsert_student(
            student_id=student_id,
            student_label=student.student_label,
            user_name=student.user_name,
            password_encrypted=student.password_encrypted,
            whatsapp_number=student.whatsapp_number,
            telegram_chat_id="123456789",
            email_address="",
            enabled=student.enabled,
            timezone=student.timezone,
        )
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof Timetable",
                        "Time": "09:00 - 10:00",
                    },
                    {
                        "Period": "10:00 - 11:00",
                        "Subject": "Physics Lab",
                        "Employee": "Prof Timetable Physics",
                        "Time": "10:00 - 11:00",
                    },
                ]
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "8",
                        "TotalPresent": "6",
                        "Percentage": "75.00%",
                    },
                    {
                        "Subject": "Physics Lab",
                        "SubjectCode": "PHYL102",
                        "EMPNAME": "Prof B",
                        "TotalLecture": "10",
                        "TotalPresent": "9",
                        "Percentage": "90.00%",
                    },
                ]
            }
        )
        telegram = FakeTelegram()
        telegram.configured = True
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp, telegram=telegram)

        body = service.send_attendance_summary_report(student_id, force=True)

        self.assertIn("Attendance Summary Report", body)
        self.assertIn("Totals present: 15", body)
        self.assertIn("Total lectures: 18", body)
        self.assertIn("Total absent: 3", body)
        self.assertIn("Subject-wise Attendance", body)
        self.assertIn("Mathematics (MATH101)", body)
        self.assertIn("Faculty: Prof Timetable", body)
        self.assertIn("Percentage: 75.00%", body)
        self.assertIn("Total lectures: 8", body)
        self.assertIn("Present: 6", body)
        self.assertIn("Absent: 2", body)
        self.assertIn("Physics Lab (PHYL102)", body)
        self.assertIn("Faculty: Prof Timetable Physics", body)
        self.assertEqual(len(whatsapp.messages), 1)
        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 2)
        self.assertEqual({item.category for item in history}, {"attendance_summary_report"})

    def test_telegram_admin_add_student_flow_saves_profile(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        chat_id = "5570554765"

        service._handle_telegram_callback(
            {
                "id": "cb-add",
                "data": "tg:student:add",
                "message": {"chat": {"id": chat_id}},
            }
        )
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "Demo Student"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "demo_user"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "skip"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "skip"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "demo-pass"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "+919876543210"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "self"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "Asia/Kolkata"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "yes"})
        service._handle_telegram_callback(
            {
                "id": "cb-save",
                "data": "tg:session:save",
                "message": {"chat": {"id": chat_id}},
            }
        )

        students = self.db.list_students()
        self.assertEqual(len(students), 1)
        self.assertEqual(students[0].student_label, "Demo Student")
        self.assertEqual(students[0].telegram_chat_id, chat_id)
        self.assertIsNone(self.db.get_telegram_admin_session(chat_id))

    def test_public_telegram_start_offers_application_flow(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)

        service._handle_telegram_message({"chat": {"id": "999001"}, "text": "/start"})

        self.assertTrue(telegram.messages)
        self.assertIn("QUMS Telegram Bot", telegram.messages[-1][2])
        self.assertIn("/apply", telegram.messages[-1][2])

    def test_public_telegram_apply_flow_submits_application_request(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        chat_id = "999001"

        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "/apply"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "Tapendra Chaudhary"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "Tapendra"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "23030682"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "erp-pass"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "8027"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "+919634549096"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "self"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "Asia/Kolkata"})
        service._handle_telegram_message({"chat": {"id": chat_id}, "text": "Please add my profile."})
        service._handle_telegram_callback(
            {
                "id": "cb-apply-save",
                "data": "tgpub:session:save",
                "message": {"chat": {"id": chat_id}},
            }
        )

        requests = service.list_application_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].applicant_name, "Tapendra Chaudhary")
        self.assertEqual(requests[0].student_label, "Tapendra")
        self.assertTrue(any(msg_chat == "5570554765" and "New student application request" in body for msg_chat, _, body in telegram.messages))
        self.assertTrue(any(msg_chat == chat_id and "Application submitted successfully." in body for msg_chat, _, body in telegram.messages))

    def test_student_telegram_menu_shows_only_the_linked_profile(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        self.db.upsert_student(
            student_id=None,
            student_label="Own Student",
            user_name="own_erp",
            password_encrypted="secret",
            whatsapp_number="+911111111111",
            telegram_chat_id="2001",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.upsert_student(
            student_id=None,
            student_label="Other Student",
            user_name="other_erp",
            password_encrypted="secret",
            whatsapp_number="+922222222222",
            telegram_chat_id="2002",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        service._handle_telegram_message({"chat": {"id": "2001"}, "text": "/menu"})

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[-1][2]
        self.assertIn("Student: Own Student", body)
        self.assertIn("ERP user id: own_erp", body)
        self.assertNotIn("Other Student", body)
        self.assertNotIn("QUMS Admin Control", body)
        self.assertNotIn("Student Profiles", body)

    def test_student_telegram_students_command_remains_scoped_to_own_profile(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        self.db.upsert_student(
            student_id=None,
            student_label="Own Student",
            user_name="own_erp",
            password_encrypted="secret",
            whatsapp_number="+911111111111",
            telegram_chat_id="2001",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.upsert_student(
            student_id=None,
            student_label="Other Student",
            user_name="other_erp",
            password_encrypted="secret",
            whatsapp_number="+922222222222",
            telegram_chat_id="2002",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        service._handle_telegram_message({"chat": {"id": "2001"}, "text": "/students"})

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[-1][2]
        self.assertIn("Student: Own Student", body)
        self.assertNotIn("Other Student", body)

    def test_student_telegram_preview_callback_uses_only_the_linked_profile(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof A",
                        "Time": "09:00 - 10:00",
                    }
                ]
            }
        )
        service = self._make_service(telegram=telegram, erp=erp)
        self.db.upsert_student(
            student_id=None,
            student_label="Own Student",
            user_name="own_erp",
            password_encrypted="secret",
            whatsapp_number="+911111111111",
            telegram_chat_id="2001",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.upsert_student(
            student_id=None,
            student_label="Other Student",
            user_name="other_erp",
            password_encrypted="secret",
            whatsapp_number="+922222222222",
            telegram_chat_id="2002",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        service._handle_telegram_callback(
            {
                "id": "cb-self-preview",
                "data": "tgs:preview",
                "message": {"chat": {"id": "2001"}},
            }
        )

        self.assertTrue(telegram.messages)
        self.assertIn("Morning Schedule Update", telegram.messages[-1][2])
        self.assertEqual(telegram.callback_answers[-1], ("cb-self-preview", "Processing request.", False))

    def test_admin_telegram_applications_command_lists_saved_requests(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass",
            whatsapp_number="+919634549096",
            telegram_chat_id="@gunda872",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )
        telegram.messages.clear()

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/applications"})
        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/application 1"})

        self.assertEqual(len(telegram.messages), 2)
        self.assertIn("Application Requests", telegram.messages[0][2])
        self.assertIn("Tapendra", telegram.messages[0][2])
        self.assertIn("ERP password: erp-pass", telegram.messages[1][2])

    def test_removed_telegram_export_callback_returns_removed_alert(self) -> None:
        student_id = self._add_student(label="Export Student", timezone="Asia/Kolkata")
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True
        service.send_test_message(student_id)

        service._handle_telegram_callback(
            {
                "id": "cb-export",
                "data": "tg:export:messages",
                "message": {"chat": {"id": "5570554765"}},
            }
        )

        self.assertEqual(service.telegram.documents, [])
        self.assertEqual(service.telegram.callback_answers[-1], ("cb-export", "This Telegram control has been removed.", True))

    def test_telegram_slash_preview_command_sends_student_preview(self) -> None:
        student_id = self._add_student(label="Preview Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof A",
                        "Time": "09:00 - 10:00",
                    }
                ]
            }
        )
        service = self._make_service(telegram=FakeTelegram(), erp=erp)
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": f"/preview {student_id}"})

        self.assertTrue(service.telegram.messages)
        self.assertIn("Morning Schedule Update", service.telegram.messages[-1][2])
        self.assertIn("Mathematics", service.telegram.messages[-1][2])

    def test_telegram_slash_attendance_command_sends_student_summary(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Attendance Student",
            user_name="attendance_student",
            password_encrypted="secret",
            whatsapp_number="+919876543210",
            telegram_chat_id="5570554765",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof Timetable",
                        "Time": "09:00 - 10:00",
                    }
                ]
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "8",
                        "TotalPresent": "6",
                        "Percentage": "75.00%",
                    }
                ]
            }
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram, erp=erp)

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": f"/attendance {student_id}"})

        self.assertEqual(len(service.telegram.messages), 1)
        self.assertIn("Attendance Summary Report", service.telegram.messages[-1][2])
        self.assertIn("Totals present: 6", service.telegram.messages[-1][2])
        self.assertIn("Faculty: Prof Timetable", service.telegram.messages[-1][2])

    def test_telegram_preview_callback_acknowledges_before_sending_preview(self) -> None:
        student_id = self._add_student(label="Preview Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof A",
                        "Time": "09:00 - 10:00",
                    }
                ]
            }
        )
        service = self._make_service(erp=erp, telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_callback(
            {
                "id": "cb-preview",
                "data": f"tg:student:{student_id}:preview",
                "message": {"chat": {"id": "5570554765"}},
            }
        )

        self.assertEqual(service.telegram.callback_answers[-1], ("cb-preview", "Processing request.", False))
        self.assertTrue(service.telegram.messages)
        self.assertIn("Morning Schedule Update", service.telegram.messages[-1][2])

    def test_telegram_test_callback_is_debounced_within_cooldown_window(self) -> None:
        student_id = self._add_student(label="Debounce Student", timezone="Asia/Kolkata")
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)

        callback = {
            "id": "cb-test-1",
            "data": f"tg:student:{student_id}:test",
            "message": {"chat": {"id": "5570554765"}},
        }
        service._handle_telegram_callback(callback)
        callback["id"] = "cb-test-2"
        service._handle_telegram_callback(callback)

        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(telegram.callback_answers[-1], ("cb-test-2", "This action was already sent recently.", True))

    def test_telegram_shortage_command_does_not_echo_duplicate_report_to_same_chat(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Tapendra",
            user_name="23030682",
            password_encrypted="secret",
            whatsapp_number="+919634549096",
            telegram_chat_id="5570554765",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Day/Period": "Friday",
                        "P1": "Mathematics(MATH101) (A-010),Prof A",
                        "P2": "Chemistry(CHEM101) (A-011),Prof B",
                    }
                ],
                "col": "Day/Period,P1,P2",
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "8",
                        "TotalPresent": "6",
                        "Percentage": "75.00%",
                    }
                ]
            }
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(erp=erp, telegram=telegram)

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": f"/shortage {student_id}"})

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Attendance Shortage Report", telegram.messages[0][2])

    def test_telegram_shortage_callback_does_not_echo_duplicate_report_to_same_chat(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Tapendra",
            user_name="23030682",
            password_encrypted="secret",
            whatsapp_number="+919634549096",
            telegram_chat_id="5570554765",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Day/Period": "Friday",
                        "P1": "Mathematics(MATH101) (A-010),Prof A",
                        "P2": "Chemistry(CHEM101) (A-011),Prof B",
                    }
                ],
                "col": "Day/Period,P1,P2",
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "8",
                        "TotalPresent": "6",
                        "Percentage": "75.00%",
                    }
                ]
            }
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(erp=erp, telegram=telegram)

        service._handle_telegram_callback(
            {
                "id": "cb-shortage",
                "data": f"tg:student:{student_id}:shortage",
                "message": {"chat": {"id": "5570554765"}},
            }
        )

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Attendance Shortage Report", telegram.messages[0][2])
        self.assertEqual(telegram.callback_answers[-1], ("cb-shortage", "Processing request.", False))

    def test_removed_telegram_admin_command_returns_removed_message(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/exportmessages"})
        chat_state = self.db.get_telegram_admin_chat("5570554765")

        self.assertIsNotNone(chat_state)
        self.assertTrue(chat_state.auto_refresh_enabled)
        self.assertIn("removed", service.telegram.messages[-1][2].lower())
        self.assertEqual(service.telegram.documents, [])

    def test_telegram_dashboard_command_sends_control_panel(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Tapendra",
            user_name="23030682",
            password_encrypted="secret",
            whatsapp_number="+919634549096",
            telegram_chat_id="5570554765",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP sync completed for 2026-03-13.",
            reg_id="8027",
            student_name="Tapendra",
        )
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/dashboard"})

        self.assertTrue(service.telegram.messages)
        body = service.telegram.messages[-1][2]
        self.assertIn("QUMS Admin Control", body)
        self.assertNotIn("Auto refresh", body)
        self.assertIn("Tapendra", body)
        self.assertIn("ERP user id: 23030682", body)
        self.assertIn("WhatsApp: +919634549096", body)
        self.assertIn("Telegram: 5570554765", body)
        self.assertIn("RegID: 8027", body)
        self.assertIn("Session updated:", body)
        self.assertIn("ERP session: ERP session saved.", body)
        self.assertIn("Recent bot activity: ERP sync completed for 2026-03-13.", body)
        chat_state = self.db.get_telegram_admin_chat("5570554765")
        self.assertIsNotNone(chat_state)
        self.assertTrue(chat_state.auto_refresh_enabled)
        self.assertTrue(chat_state.last_dashboard_sent_at)

    def test_repeated_telegram_dashboard_command_reuses_existing_dashboard_message(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/dashboard"})
        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/dashboard"})

        self.assertEqual(len(service.telegram.messages), 1)
        self.assertEqual(len(service.telegram.edits), 1)

    def test_telegram_student_menu_text_matches_dashboard_fields(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Tapendra",
            user_name="23030682",
            password_encrypted="secret",
            whatsapp_number="+919634549096",
            telegram_chat_id="5570554765",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP sync completed for 2026-03-13.",
            reg_id="8027",
            student_name="Tapendra",
        )
        student = self.db.get_student(student_id)
        assert student is not None
        service = self._make_service(telegram=FakeTelegram())

        body = service._build_telegram_student_menu_text(student)

        self.assertIn("Student: Tapendra", body)
        self.assertIn("ERP user id: 23030682", body)
        self.assertIn("WhatsApp: +919634549096", body)
        self.assertIn("Telegram: 5570554765", body)
        self.assertIn("RegID: 8027", body)
        self.assertIn("Session updated:", body)
        self.assertIn("Timezone: Asia/Kolkata", body)
        self.assertIn("Enabled: Yes", body)
        self.assertIn("ERP session: ERP session saved.", body)
        self.assertIn("Recent bot activity: ERP sync completed for 2026-03-13.", body)

    def test_telegram_students_text_matches_dashboard_fields(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Tapendra",
            user_name="23030682",
            password_encrypted="secret",
            whatsapp_number="+919634549096",
            telegram_chat_id="5570554765",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP sync completed for 2026-03-13.",
            reg_id="8027",
            student_name="Tapendra",
        )
        service = self._make_service(telegram=FakeTelegram())

        body = service._build_telegram_students_text()

        self.assertIn("Student Profiles", body)
        self.assertIn(f"{student_id}. Tapendra", body)
        self.assertIn("ERP user id: 23030682", body)
        self.assertIn("WhatsApp: +919634549096", body)
        self.assertIn("Telegram: 5570554765", body)
        self.assertIn("RegID: 8027", body)
        self.assertIn("Session updated:", body)
        self.assertIn("ERP session: ERP session saved.", body)
        self.assertIn("Recent bot activity: ERP sync completed for 2026-03-13.", body)

    def test_telegram_menu_command_opens_dashboard_instead_of_help_text(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/menu"})

        self.assertTrue(service.telegram.messages)
        body = service.telegram.messages[-1][2]
        self.assertIn("QUMS Admin Control", body)
        self.assertNotIn("QUMS Telegram Admin", body)

    def test_telegram_live_refresh_sweep_skips_unchanged_dashboard(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/dashboard"})
        self.assertEqual(len(service.telegram.messages), 1)

        service.run_telegram_admin_refresh_sweep(now=datetime(2026, 3, 13, 15, 0, tzinfo=ZoneInfo("Asia/Kolkata")))

        self.assertEqual(len(service.telegram.messages), 1)
        self.assertEqual(len(service.telegram.edits), 0)

    def test_telegram_inbound_sweep_registers_bot_commands(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)

        service.run_telegram_inbound_sweep()

        self.assertTrue(telegram.commands)
        self.assertTrue(any(item["command"] == "menu" for item in telegram.commands))
        self.assertTrue(any(item["command"] == "attendance" for item in telegram.commands))
        self.assertFalse(any(item["command"] == "autorefresh" for item in telegram.commands))
        self.assertFalse(any(item["command"] == "runchecks" for item in telegram.commands))
        self.assertFalse(any(item["command"] == "exportmessages" for item in telegram.commands))
        self.assertFalse(any(item["command"] == "exportaudit" for item in telegram.commands))

    def test_telegram_inbound_sweep_reregisters_bot_commands_when_signature_changes(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        self.db.upsert_runtime_state(
            state_key="telegram_bot_commands_registered",
            state_value="stale-signature",
        )

        service.run_telegram_inbound_sweep()

        self.assertTrue(telegram.commands)
        self.assertEqual(
            self.db.get_runtime_state("telegram_bot_commands_registered"),
            service._telegram_bot_commands_state_value(),
        )

    def test_telegram_inbound_sweep_advances_offset_even_when_one_update_fails(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)

        original = service._handle_telegram_update

        def flaky_handle(update):
            if int(update.get("update_id", 0)) == 101:
                raise RuntimeError("boom")
            return original(update)

        service._handle_telegram_update = flaky_handle  # type: ignore[method-assign]
        telegram.updates = [
            {"update_id": 101, "message": {"chat": {"id": "5570554765"}, "text": "/dashboard"}},
            {"update_id": 102, "message": {"chat": {"id": "5570554765"}, "text": "/students"}},
        ]

        service.run_telegram_inbound_sweep()

        self.assertEqual(self.db.get_runtime_state("telegram_update_offset"), "103")
        self.assertTrue(service.telegram.messages)

    def test_telegram_invalid_slash_command_argument_returns_error_without_breaking_flow(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/preview not-a-number"})

        self.assertTrue(service.telegram.messages)
        self.assertIn("requires a numeric student id", service.telegram.messages[-1][2])

    def test_telegram_test_command_is_debounced_within_cooldown_window(self) -> None:
        student_id = self._add_student(label="Debounce Student", timezone="Asia/Kolkata")
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": f"/test {student_id}"})
        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": f"/test {student_id}"})

        self.assertEqual(len(service.telegram.messages), 2)
        self.assertIn("configured delivery channels are working", service.telegram.messages[0][2])
        self.assertIn("already sent recently", service.telegram.messages[1][2])

    def test_save_student_rejects_invalid_timezone(self) -> None:
        service = self._make_service()

        with self.assertRaises(ValueError):
            service.save_student(
                student_id=None,
                student_label="Timezone Student",
                user_name="timezone_user",
                password="secret",
                whatsapp_number="+919876543210",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Mars/Olympus",
            )

    def test_save_student_rejects_duplicate_user_name_and_whatsapp(self) -> None:
        service = self._make_service()
        service.save_student(
            student_id=None,
            student_label="First Student",
            user_name="duplicate_user",
            password="secret",
            whatsapp_number="+919876543210",
            telegram_chat_id="",
            email_address="",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        with self.assertRaises(ValueError):
            service.save_student(
                student_id=None,
                student_label="Second Student",
                user_name="duplicate_user",
                password="secret",
                whatsapp_number="+919876543211",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )

        with self.assertRaises(ValueError):
            service.save_student(
                student_id=None,
                student_label="Third Student",
                user_name="another_user",
                password="secret",
                whatsapp_number="whatsapp:+91 98765 43210",
                telegram_chat_id="",
                email_address="",
                enabled=True,
                timezone="Asia/Kolkata",
            )

    def test_preview_today_uses_full_timetable_and_stable_subject_key(self) -> None:
        student_id = self._add_student(label="Audit Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 10:00", "Subject": "Mathematics", "Employee": "Prof A", "Time": "09:00 - 10:00"},
                    {"Period": "10:00 - 11:00", "Subject": "Physics", "Employee": "Prof B", "Time": "10:00 - 11:00"},
                ]
            },
            substitutions_payload={
                "state": [
                    {
                        "SubsDate": date.today().strftime("%d/%m/%Y"),
                        "Period": "10:00 - 11:00",
                        "Time": "10:00 - 11:00",
                        "Subject": "Physics",
                        "Employee": "Prof B",
                        "SubsSubject": "Physics Lab",
                        "SubsEmployee": "Prof C",
                    }
                ]
            },
        )
        service = self._make_service(erp=erp)

        message = service.preview_today(student_id, target_date=date.today())
        events = self.db.get_lecture_events_for_day(student_id, date.today())
        lecture_events = [event for event in events if not event.is_break]

        self.assertIn("Mathematics", message)
        self.assertIn("Physics Lab", message)
        self.assertIn("Substitute Lectures", message)
        self.assertEqual(len(lecture_events), 2)
        physics_event = next(event for event in lecture_events if event.slot_label == "10:00 - 11:00")
        self.assertEqual(physics_event.subject_key, "physics")
        self.assertEqual(physics_event.teacher_name, "Prof C")

    def test_holiday_rows_do_not_enter_attendance_pipeline(self) -> None:
        student_id = self._add_student(label="Holiday Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 10:00", "Subject": "Holiday", "Employee": "", "Time": "09:00 - 10:00"},
                    {"Period": "10:00 - 11:00", "Subject": "No Class", "Employee": "", "Time": "10:00 - 11:00"},
                ]
            }
        )
        service = self._make_service(erp=erp)

        message = service.preview_today(student_id, target_date=date(2026, 3, 14))
        events = self.db.get_lecture_events_for_day(student_id, date(2026, 3, 14))
        pending = self.db.get_pending_lecture_events()

        self.assertIn("No-Class Day Update", message)
        self.assertIn("Status: Holiday", message)
        self.assertIn("There are no scheduled classes for today.", message)
        self.assertTrue(all(event.is_break for event in events))
        self.assertEqual(pending, [])

    def test_empty_sunday_timetable_uses_off_day_fallback(self) -> None:
        student_id = self._add_student(label="Sunday Student", timezone="Asia/Kolkata")
        service = self._make_service(erp=FakeERP())

        message = service.preview_today(student_id, target_date=date(2026, 3, 15))
        events = self.db.get_lecture_events_for_day(student_id, date(2026, 3, 15))

        self.assertIn("No-Class Day Update", message)
        self.assertIn("Status: Off Day", message)
        self.assertIn("Happy Sunday", message)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].is_break)
        self.assertEqual(events[0].subject_name, "Off Day")
        self.assertEqual(self.db.get_pending_lecture_events(), [])

    def test_monitor_sweep_dedupes_sandbox_and_erp_alerts(self) -> None:
        student_id = self._add_student(label="Monitor Student", timezone="Asia/Kolkata")
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        expires_at = (datetime.now(tz=ZoneInfo("UTC")) + timedelta(minutes=5)).isoformat()
        whatsapp = FakeWhatsApp(
            status=WhatsAppChannelStatus(
                configured=True,
                mode="sandbox",
                sender="whatsapp:+14155238886",
                ready=True,
                state="sandbox_ready",
                detail="ok",
                join_command="join demo-code",
                last_inbound_at=None,
                sandbox_expires_at=expires_at,
                last_outbound_status=None,
                last_error_code=None,
            )
        )
        service = self._make_service(erp=FakeERP(auth_required=True), whatsapp=whatsapp)
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        service.run_monitor_sweep(now=now)
        service.run_monitor_sweep(now=now)

        history = service.list_message_history()
        categories = {item.category for item in history}
        self.assertEqual(len(history), 2)
        self.assertIn("sandbox_expiry_warning", categories)
        self.assertIn("erp_session_expired", categories)

    def test_get_whatsapp_status_returns_fallback_for_dashboard_failures(self) -> None:
        student_id = self._add_student(label="Dashboard Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        service = self._make_service(whatsapp=BrokenWhatsApp())

        status = service.get_whatsapp_status(student)

        self.assertFalse(status.ready)
        self.assertEqual(status.state, "status_check_failed")
        self.assertIn("twilio unavailable", status.detail)
        self.assertEqual(status.join_command, "join demo-code")

    def test_monitor_sweep_handles_status_lookup_failure_during_erp_expiry(self) -> None:
        student_id = self._add_student(label="Expired Student", timezone="Asia/Kolkata")
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        service = self._make_service(erp=FakeERP(auth_required=True), whatsapp=BrokenWhatsApp())
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        service.run_monitor_sweep(now=now)

        history = service.list_message_history()
        self.assertEqual(history, [])
        refreshed = self.db.get_student(student_id)
        assert refreshed is not None
        assert refreshed.session_updated_at is not None
        self.assertFalse(self.db.has_notification_event(student_id, "erp_session_expired", refreshed.session_updated_at))
        self.assertIn("WhatsApp status lookup failed", refreshed.last_login_status or "")

    def test_attendance_timestamp_is_persisted_and_reported_after_resync(self) -> None:
        student_id = self._add_student(label="Attendance Student", timezone="Asia/Kolkata")
        target_date = date(2026, 3, 13)
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof A",
                        "Time": "09:00 - 10:00",
                    }
                ]
            }
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        service.preview_today(student_id, target_date=target_date)
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        recorded_at = datetime(2026, 3, 13, 10, 25, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.db.mark_event_status(event.id, "notified_present", status_recorded_at=recorded_at)

        service.preview_today(student_id, target_date=target_date)
        refreshed_event = self.db.get_lecture_events_for_day(student_id, target_date)[0]

        self.assertEqual(refreshed_event.status, "notified_present")
        self.assertEqual(refreshed_event.status_recorded_at, recorded_at)

        report = service.send_evening_report(student_id, target_date=target_date, force=True)

        self.assertIn("Generated at:", report)
        self.assertIn("Marked at: Friday, 13 March 2026 at 10:25", report)
        self.assertIn("Present", report)

    def test_idempotent_send_prevents_duplicate_delivery(self) -> None:
        student_id = self._add_student(label="Idempotent Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        whatsapp = FakeWhatsApp()
        service = self._make_service(whatsapp=whatsapp)

        service._send_whatsapp(
            student,
            "Attendance Update\n\nStatus: Present",
            message_kind="attendance",
            history_category="attendance_update",
            idempotency_key="attendance_update:test-key",
        )
        service._send_whatsapp(
            student,
            "Attendance Update\n\nStatus: Present",
            message_kind="attendance",
            history_category="attendance_update",
            idempotency_key="attendance_update:test-key",
        )

        self.assertEqual(len(whatsapp.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].idempotency_key, "attendance_update:test-key:whatsapp")

    def test_task_dispatcher_claims_each_periodic_slot_once(self) -> None:
        self._add_student(label="Dispatch Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 10:00", "Subject": "Mathematics", "Employee": "Prof A", "Time": "09:00 - 10:00"}
                ]
            }
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)
        dispatcher = TaskDispatcher(settings=self.settings, db=self.db, service=service)
        now = datetime(2026, 3, 13, 6, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        first = dispatcher.dispatch_periodic(
            job_name="scheduled-dispatch",
            callback_name="run_scheduled_dispatch",
            interval_minutes=1,
            now=now,
        )
        second = dispatcher.dispatch_periodic(
            job_name="scheduled-dispatch",
            callback_name="run_scheduled_dispatch",
            interval_minutes=1,
            now=now,
        )

        self.assertTrue(first.dispatched)
        self.assertFalse(second.dispatched)
        self.assertEqual(len(whatsapp.messages), 1)

    def test_low_attendance_and_shortage_alerts_are_sent(self) -> None:
        student_id = self._add_student(label="Risk Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof Timetable",
                        "Time": "09:00 - 10:00",
                    }
                ]
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof ERP",
                        "TotalLecture": "4",
                        "TotalPresent": "3",
                        "Percentage": "75.00%",
                    }
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        service.send_morning_update(student_id, target_date=date(2026, 3, 15))

        categories = {item.category for item in service.list_message_history()}
        self.assertIn("attendance_shortage_warning", categories)
        shortage_messages = [body for _, _, body in whatsapp.messages if "Attendance Shortage Warning" in body]
        self.assertEqual(len(shortage_messages), 1)
        subject_shortage = next(body for body in shortage_messages if "Subject: Mathematics (MATH101)" in body)
        self.assertIn("Faculty: Prof Timetable", subject_shortage)
        self.assertIn("Total absent: 1", subject_shortage)
        self.assertIn("Threshold monitored: 75%", subject_shortage)
        self.assertNotIn("Prof ERP", subject_shortage)

    def test_subject_shortage_warning_is_sent_when_attendance_drops_below_75_percent(self) -> None:
        student_id = self._add_student(label="Below Threshold Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "11:00 - 12:00",
                        "Subject": "Physics",
                        "Employee": "Prof Timetable Physics",
                        "Time": "11:00 - 12:00",
                    }
                ]
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Physics",
                        "SubjectCode": "PHY101",
                        "EMPNAME": "Prof ERP Physics",
                        "TotalLecture": "10",
                        "TotalPresent": "7",
                        "Percentage": "70.00%",
                    }
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        service.send_morning_update(student_id, target_date=date(2026, 3, 16))

        shortage_messages = [body for _, _, body in whatsapp.messages if "Attendance Shortage Warning" in body]
        self.assertEqual(len(shortage_messages), 1)
        subject_shortage = next(body for body in shortage_messages if "Subject: Physics (PHY101)" in body)
        self.assertIn("Faculty: Prof Timetable Physics", subject_shortage)
        self.assertIn("Current attendance: 7/10 (70.00%)", subject_shortage)
        self.assertIn("Total absent: 3", subject_shortage)
        self.assertIn("Threshold monitored: 75%", subject_shortage)

    def test_subject_shortage_warning_is_not_sent_above_75_percent(self) -> None:
        student_id = self._add_student(label="Above Threshold Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "12:00 - 13:00",
                        "Subject": "Biology",
                        "Employee": "Prof Biology",
                        "Time": "12:00 - 13:00",
                    }
                ]
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Biology",
                        "SubjectCode": "BIO101",
                        "EMPNAME": "Prof ERP Biology",
                        "TotalLecture": "10",
                        "TotalPresent": "8",
                        "Percentage": "80.00%",
                    }
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        service.send_morning_update(student_id, target_date=date(2026, 3, 16))

        shortage_messages = [body for _, _, body in whatsapp.messages if "Attendance Shortage Warning" in body]
        self.assertEqual(shortage_messages, [])
        categories = {item.category for item in service.list_message_history()}
        self.assertNotIn("attendance_shortage_warning", categories)

    def test_manual_shortage_report_lists_subjects_at_or_below_75_percent(self) -> None:
        student_id = self._add_student(label="Shortage Report Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof Timetable",
                        "Time": "09:00 - 10:00",
                    },
                    {
                        "Period": "10:00 - 11:00",
                        "Subject": "Chemistry",
                        "Employee": "Prof Chem",
                        "Time": "10:00 - 11:00",
                    },
                ]
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof ERP Math",
                        "TotalLecture": "8",
                        "TotalPresent": "6",
                        "Percentage": "75.00%",
                    },
                    {
                        "Subject": "Chemistry",
                        "SubjectCode": "CHEM101",
                        "EMPNAME": "Prof ERP Chem",
                        "TotalLecture": "10",
                        "TotalPresent": "9",
                        "Percentage": "90.00%",
                    },
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        report = service.send_shortage_report(student_id, target_date=date(2026, 3, 16), force=True)

        self.assertIn("Attendance Shortage Report", report)
        self.assertIn("Threshold: 75% per subject", report)
        self.assertIn("Scope: totals below include only subjects currently at risk.", report)
        self.assertIn("Totals present: 6", report)
        self.assertIn("Total lectures: 8", report)
        self.assertIn("Total absent: 2", report)
        self.assertIn("Mathematics (MATH101)", report)
        self.assertIn("Faculty: Prof Timetable", report)
        self.assertNotIn("Chemistry (CHEM101)", report)
        self.assertEqual(len(whatsapp.messages), 1)
        self.assertEqual(service.list_message_history()[0].category, "attendance_shortage_report")

    def test_attendance_summary_change_scan_silently_builds_initial_baseline(self) -> None:
        student_id = self._add_student(label="Baseline Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Day/Period": "Friday",
                        "P1": "Mathematics(MATH101) (A-010),Prof A",
                        "P2": "Chemistry(CHEM101) (A-011),Prof B",
                    }
                ],
                "col": "Day/Period,P1,P2",
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "10",
                        "TotalPresent": "9",
                        "Percentage": "90.00%",
                    },
                    {
                        "Subject": "Chemistry",
                        "SubjectCode": "CHEM101",
                        "EMPNAME": "Prof B",
                        "TotalLecture": "8",
                        "TotalPresent": "7",
                        "Percentage": "87.50%",
                    },
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 13, 17, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertFalse(any("Attendance Summary Change Update" in body for _, _, body in whatsapp.messages))
        snapshots = self.db.get_attendance_snapshots(student_id)
        self.assertEqual(len(snapshots), 2)

    def test_attendance_summary_change_alert_reports_overall_and_subject_deltas(self) -> None:
        student_id = self._add_student(label="Summary Change Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="math101",
            subject_name="Mathematics",
            subject_code="MATH101",
            teacher_name="Prof A",
            total_lecture=10,
            total_present=9,
            percentage="90.00%",
        )
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="chem101",
            subject_name="Chemistry",
            subject_code="CHEM101",
            teacher_name="Prof B",
            total_lecture=8,
            total_present=7,
            percentage="87.50%",
        )
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Day/Period": "Friday",
                        "P1": "Mathematics(MATH101) (A-010),Prof A",
                        "P2": "Chemistry(CHEM101) (A-011),Prof B",
                    }
                ],
                "col": "Day/Period,P1,P2",
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "11",
                        "TotalPresent": "10",
                        "Percentage": "90.91%",
                    },
                    {
                        "Subject": "Chemistry",
                        "SubjectCode": "CHEM101",
                        "EMPNAME": "Prof B",
                        "TotalLecture": "8",
                        "TotalPresent": "7",
                        "Percentage": "87.50%",
                    },
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 13, 17, 5, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        messages = [body for _, _, body in whatsapp.messages if "Attendance Summary Change Update" in body]
        self.assertEqual(len(messages), 1)
        body = messages[0]
        self.assertIn("Overall attendance", body)
        self.assertIn("Previous: 16/18 (88.89%) | Absent: 2", body)
        self.assertIn("Current: 17/19 (89.47%) | Absent: 2", body)
        self.assertIn("Change: present +1, lectures +1, absent +0", body)
        self.assertIn("Mathematics (MATH101)", body)
        self.assertIn("Faculty: Prof A", body)
        self.assertIn("Previous: 9/10 (90.00%) | Absent: 1", body)
        self.assertIn("Current: 10/11 (90.91%) | Absent: 1", body)

    def test_attendance_summary_change_alert_does_not_repeat_on_unchanged_followup_scan(self) -> None:
        student_id = self._add_student(label="No Repeat Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="math101",
            subject_name="Mathematics",
            subject_code="MATH101",
            teacher_name="Prof A",
            total_lecture=10,
            total_present=9,
            percentage="90.00%",
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "11",
                        "TotalPresent": "10",
                        "Percentage": "90.91%",
                    }
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)
        now = datetime(2026, 3, 13, 17, 10, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_attendance_scan(student, [], now)
        service._process_attendance_scan(student, [], now + timedelta(minutes=1))

        messages = [body for _, _, body in whatsapp.messages if "Attendance Summary Change Update" in body]
        self.assertEqual(len(messages), 1)

    def test_preview_today_does_not_send_risk_alert_side_effects(self) -> None:
        student_id = self._add_student(label="Preview Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 10:00", "Subject": "Mathematics", "Employee": "Prof A", "Time": "09:00 - 10:00"}
                ]
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "4",
                        "TotalPresent": "3",
                        "Percentage": "75.00%",
                    }
                ]
            },
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)

        preview = service.preview_today(student_id, target_date=date(2026, 3, 15))

        self.assertIn("Morning Schedule Update", preview)
        self.assertEqual(len(whatsapp.messages), 0)
        self.assertEqual(service.list_message_history(), [])

    def test_morning_summary_includes_class_location_from_timetable(self) -> None:
        student_id = self._add_student(label="Room Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Day/Period": "Friday",
                        "P1 09:00 - 09:55": "Artificial Intelligence(CS36311) (A-010),AMIT KUMAR",
                    }
                ],
                "col": "Day/Period,P1 09:00 - 09:55",
            }
        )
        service = self._make_service(erp=erp, whatsapp=FakeWhatsApp())

        preview = service.preview_today(student_id, target_date=date(2026, 3, 13))

        self.assertIn("Class: A-010", preview)
        self.assertIn("Faculty: AMIT KUMAR", preview)

    def test_evening_report_includes_class_location(self) -> None:
        student_id = self._add_student(label="Evening Room Student", timezone="Asia/Kolkata")
        target_date = date(2026, 3, 13)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="artificial_intelligence",
                    subject_name="Artificial Intelligence",
                    teacher_name="AMIT KUMAR",
                    raw_cell="Artificial Intelligence(CS36311) (A-010),AMIT KUMAR",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        self.db.mark_event_status(
            event.id,
            "notified_present",
            status_recorded_at=datetime(2026, 3, 13, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        service = self._make_service(whatsapp=FakeWhatsApp())

        report = service.send_evening_report(student_id, target_date=target_date, force=True)

        self.assertIn("Class: A-010", report)
        self.assertIn("Faculty: AMIT KUMAR", report)

    def test_retry_sweep_recovers_failed_outbound_message(self) -> None:
        student_id = self._add_student(label="Retry Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        whatsapp = FlakyWhatsApp()
        service = self._make_service(whatsapp=whatsapp)

        with self.assertRaises(NotificationDeliveryError):
            service._send_whatsapp(
                student,
                "Attendance Update\n\nStatus: Present",
                message_kind="attendance",
                history_category="attendance_update",
                idempotency_key="attendance_update:retry-test",
            )

        before = self.db.get_outbound_queue_summary()
        self.assertEqual(before["failed"], 1)

        service.run_retry_sweep(now=datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(seconds=2))

        after = self.db.get_outbound_queue_summary()
        self.assertEqual(after["failed"], 0)
        self.assertEqual(after["sent"], 1)
        self.assertEqual(len(service.list_message_history()), 1)

    def test_retry_sweep_survives_history_logging_failure(self) -> None:
        student_id = self._add_student(label="Retry History Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        whatsapp = FlakyWhatsApp()
        service = self._make_service(whatsapp=whatsapp)

        with self.assertRaises(NotificationDeliveryError):
            service._send_whatsapp(
                student,
                "Attendance Update\n\nStatus: Present",
                message_kind="attendance",
                history_category="attendance_update",
                idempotency_key="attendance_update:retry-history-test",
            )

        with patch.object(service.db, "insert_message_history", side_effect=RuntimeError("history store failed")):
            service.run_retry_sweep(now=datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(seconds=2))

        refreshed = self.db.get_student(student_id)
        assert refreshed is not None
        self.assertIn("history logging failed", refreshed.last_login_status or "")
        self.assertEqual(self.db.get_outbound_queue_summary()["sent"], 1)

    def test_manual_dead_letter_retry_delivers_message(self) -> None:
        student_id = self._add_student(label="Dead Letter Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        whatsapp = FakeWhatsApp()
        service = self._make_service(whatsapp=whatsapp)

        claimed = self.db.claim_outbound_message(
            idempotency_key="attendance_update:dead-letter-test",
            student_id=student_id,
            channel="whatsapp",
            recipient=student.whatsapp_number,
            category="attendance_update",
            message_kind="attendance",
            title="Attendance Update",
            body="Attendance Update\n\nStatus: Present",
        )
        self.assertTrue(claimed)
        self.db.mark_outbound_message_failed(
            "attendance_update:dead-letter-test",
            "temporary failure",
            retry_limit=1,
            retry_backoff_seconds=1,
        )

        result = service.retry_dead_letter_message("attendance_update:dead-letter-test")

        self.assertIn("retried successfully", result.lower())
        self.assertEqual(self.db.get_outbound_queue_summary()["dead_letter"], 0)
        self.assertEqual(self.db.get_outbound_queue_summary()["sent"], 1)
        self.assertEqual(len(whatsapp.messages), 1)
        self.assertEqual(len(service.list_message_history()), 1)

    def test_force_resend_evening_report_uses_new_delivery_idempotency(self) -> None:
        student_id = self._add_student(label="Force Report Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 15)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 10:00",
                    subject_key="math101",
                    subject_name="Mathematics",
                    teacher_name="Prof A",
                    raw_cell="Mathematics\nProf A\n09:00 - 10:00",
                    start_time=time(9, 0),
                    end_time=time(10, 0),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        self.db.mark_event_status(
            event.id,
            "notified_present",
            status_recorded_at=datetime(2026, 3, 15, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(whatsapp=whatsapp)

        first = service.send_evening_report(student_id, target_date=target_date, force=True)
        second = service.send_evening_report(student_id, target_date=target_date, force=True)

        self.assertIn("End-of-Day Attendance Report", first)
        self.assertIn("End-of-Day Attendance Report", second)
        self.assertEqual(len(whatsapp.messages), 2)

    def test_force_resend_morning_update_uses_new_delivery_idempotency(self) -> None:
        student_id = self._add_student(label="Force Morning Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof A",
                        "Time": "09:00 - 10:00",
                    }
                ]
            }
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)
        target_date = date(2026, 3, 15)

        first = service.send_morning_update(student_id, target_date=target_date)
        second = service.send_morning_update(student_id, target_date=target_date, force=True)

        self.assertIn("Morning Schedule Update", first)
        self.assertIn("Morning Schedule Update", second)
        self.assertEqual(len(whatsapp.messages), 2)

    def test_delete_student_cleans_outbound_messages(self) -> None:
        student_id = self._add_student(label="Delete Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        service = self._make_service()
        service._send_whatsapp(
            student,
            "Attendance Update\n\nStatus: Present",
            message_kind="attendance",
            history_category="attendance_update",
            idempotency_key="attendance_update:delete-test",
        )

        self.assertEqual(self.db.get_outbound_queue_summary()["sent"], 1)
        self.assertTrue(self.db.delete_student(student_id))
        summary = self.db.get_outbound_queue_summary()
        self.assertEqual(summary["sent"], 0)

    def test_delayed_substitute_absent_alert_includes_substitute_details(self) -> None:
        student_id = self._add_student(label="Late Substitute Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 11)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="10:00 - 11:00",
                    subject_key="physics",
                    subject_name="Physics",
                    teacher_name="Prof B",
                    raw_cell="Physics\nProf B\n10:00 - 11:00",
                    start_time=time(10, 0),
                    end_time=time(11, 0),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        self.db.update_lecture_event_assignment(
            event.id,
            subject_key="physics",
            subject_name="Physics Lab",
            teacher_name="Prof C",
            raw_cell="Physics Lab\nProf C\n10:00 - 11:00",
            note="Substitute lecture assigned | Original faculty: Prof B | Ends at: 11:00",
        )
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="phy101",
            subject_name="Physics",
            subject_code="PHY101",
            teacher_name="Prof B",
            total_lecture=4,
            total_present=4,
            percentage="100.00%",
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Physics Lab",
                        "SubjectCode": "PHY101",
                        "EMPNAME": "Prof C",
                        "TotalLecture": "5",
                        "TotalPresent": "4",
                        "Percentage": "80.00%",
                    }
                ]
            }
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        detected_at = datetime(2026, 3, 12, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_due_events(
            student,
            [event],
            detected_at,
            attendance=parse_attendance_summary(erp.get_attendance_summary(student)),
            snapshots=self.db.get_attendance_snapshots(student_id),
        )

        self.assertEqual(len(whatsapp.messages), 1)
        body = whatsapp.messages[0][2]
        self.assertIn("Final status: Absent", body)
        self.assertIn("Lecture type: Substitute lecture", body)
        self.assertIn("Substitute faculty: Prof C", body)
        self.assertIn("Original faculty: Prof B", body)
        self.assertIn("Late update: This lecture was marked 1 day(s) after the lecture date.", body)

    def test_attendance_alert_uses_substitute_teacher_when_erp_marker_differs_from_stale_event_teacher(self) -> None:
        student_id = self._add_student(label="Substitute Marker Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 11)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="10:00 - 11:00",
                    subject_key="physics",
                    subject_name="Physics",
                    teacher_name="Prof B",
                    raw_cell="Physics\nProf B\n10:00 - 11:00",
                    start_time=time(10, 0),
                    end_time=time(11, 0),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        self.db.update_lecture_event_assignment(
            event.id,
            subject_key="physics",
            subject_name="Physics Lab",
            teacher_name="Prof B",
            raw_cell="Physics Lab\nProf B\n10:00 - 11:00",
            note="Substitute lecture assigned | Original faculty: Prof B | Ends at: 11:00",
        )
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="phy101",
            subject_name="Physics",
            subject_code="PHY101",
            teacher_name="Prof B",
            total_lecture=4,
            total_present=4,
            percentage="100.00%",
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Physics Lab",
                        "SubjectCode": "PHY101",
                        "EMPNAME": "Prof C",
                        "TotalLecture": "5",
                        "TotalPresent": "5",
                        "Percentage": "100.00%",
                    }
                ]
            }
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        detected_at = datetime(2026, 3, 11, 11, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_due_events(
            student,
            [event],
            detected_at,
            attendance=parse_attendance_summary(erp.get_attendance_summary(student)),
            snapshots=self.db.get_attendance_snapshots(student_id),
        )

        self.assertEqual(len(whatsapp.messages), 1)
        body = whatsapp.messages[0][2]
        self.assertIn("Final status: Present", body)
        self.assertIn("Faculty: Prof C", body)
        self.assertIn("Attendance marked by: Prof C", body)
        self.assertIn("Lecture type: Substitute lecture", body)
        self.assertIn("Substitute faculty: Prof C", body)
        self.assertIn("Original faculty: Prof B", body)

    def test_multiple_old_pending_lectures_for_same_subject_all_get_alerts(self) -> None:
        student_id = self._add_student(label="Batch Pending Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        for target_date in [date(2026, 3, 10), date(2026, 3, 11)]:
            self.db.replace_lecture_events(
                student_id=student_id,
                event_date=target_date,
                slots=[
                    LectureSlot(
                        slot_label="09:00 - 10:00",
                        subject_key="math101",
                        subject_name="Mathematics",
                        teacher_name="Prof A",
                        raw_cell="Mathematics\nProf A\n09:00 - 10:00",
                        start_time=time(9, 0),
                        end_time=time(10, 0),
                        is_break=False,
                    )
                ],
                grace_minutes=self.settings.lecture_grace_minutes,
            )
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="math101",
            subject_name="Mathematics",
            subject_code="MATH101",
            teacher_name="Prof A",
            total_lecture=4,
            total_present=4,
            percentage="100.00%",
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "6",
                        "TotalPresent": "6",
                        "Percentage": "100.00%",
                    }
                ]
            }
        )
        whatsapp = FakeWhatsApp()
        service = self._make_service(erp=erp, whatsapp=whatsapp)
        events = self.db.get_pending_lecture_events()
        detected_at = datetime(2026, 3, 12, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_due_events(
            student,
            events,
            detected_at,
            attendance=parse_attendance_summary(erp.get_attendance_summary(student)),
            snapshots=self.db.get_attendance_snapshots(student_id),
        )

        self.assertEqual(len(whatsapp.messages), 2)
        self.assertTrue(all("Final status: Present" in message[2] for message in whatsapp.messages))
        self.assertTrue(all("ERP updated 2 pending lecture(s)" in message[2] for message in whatsapp.messages))
        refreshed = self.db.get_lecture_events_between(student_id, date(2026, 3, 10), date(2026, 3, 11))
        self.assertTrue(all(event.status == "notified_present" for event in refreshed))


if __name__ == "__main__":
    unittest.main()
