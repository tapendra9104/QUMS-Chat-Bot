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
from qums_bot.errors import StudentValidationError
from qums_bot.erp_client import AuthenticationRequired
from qums_bot.models import LectureSlot, PendingLogin
from qums_bot.parsers import parse_attendance_summary
from qums_bot.scheduler import build_scheduler
from qums_bot.service import BotService, ERP_SESSION_EXPIRED_STATUS_TEXT, NotificationDeliveryError
from qums_bot.task_queue import TaskDispatcher


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
        telegram_admin_chat_ids=("5570554765",),
        telegram_poll_interval_seconds=5,
        lecture_schedule_poll_interval_seconds=30,
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

    def start_manual_login(self, student):
        return PendingLogin(
            student_id=student.id,
            request_verification_token="token",
            hdn_msg="QGC",
            check_online="0",
            client_ip="127.0.0.1",
            captcha_data_url="data:image/png;base64,abc",
            cookies_json="[]",
            created_at="2026-03-14T10:05:00+05:30",
        )


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


class ConfiguredTelegram(FakeTelegram):
    configured = True


class BrokenTelegram(ConfiguredTelegram):
    def send_text(self, chat_id: str, body: str, *, message_kind: str = "generic", reply_markup=None) -> str:
        raise RuntimeError("telegram unavailable")


class FlakyTelegram(ConfiguredTelegram):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    def send_text(self, chat_id: str, body: str, *, message_kind: str = "generic", reply_markup=None) -> str:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("temporary delivery failure")
        return super().send_text(chat_id, body, message_kind=message_kind, reply_markup=reply_markup)



class DummyService:
    def run_scheduled_dispatch(self):
        return None

    def run_lecture_schedule_sweep(self):
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
        tmp_root = Path("tmp-test2")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_root / self.id().replace(".", "_")
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
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
            telegram_chat_id="5570554766",
            enabled=True,
            timezone=timezone,
        )

    def _set_student_activity_times(
        self,
        student_id: int,
        *,
        session_updated_at: str | None = None,
        last_erp_sync_at: str | None = None,
    ) -> None:
        with self.db._connect() as conn:
            conn.execute(
                """
                UPDATE students
                SET session_updated_at = COALESCE(?, session_updated_at),
                    last_erp_sync_at = COALESCE(?, last_erp_sync_at),
                    updated_at = COALESCE(?, updated_at)
                WHERE id = ?
                """,
                (
                    session_updated_at,
                    last_erp_sync_at,
                    session_updated_at or last_erp_sync_at,
                    student_id,
                ),
            )

    def _make_service(
        self,
        *,
        erp: FakeERP | None = None,
        telegram: FakeTelegram | None = None,
    ) -> BotService:
        return BotService(
            settings=self.settings,
            db=self.db,
            erp_client=erp or FakeERP(),
            telegram=telegram or ConfiguredTelegram(),
        )

    def test_scheduler_registers_expected_jobs(self) -> None:
        scheduler = build_scheduler(self.settings, DummyService())
        jobs = scheduler.get_jobs()
        job_ids = {job.id for job in jobs}
        jobs_by_id = {job.id: job for job in jobs}
        self.assertEqual(
            job_ids,
            {
                "scheduled-dispatch",
                "lecture-schedule-checks",
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
        self.assertEqual(
            jobs_by_id["lecture-schedule-checks"].trigger.interval.total_seconds(),
            self.settings.lecture_schedule_poll_interval_seconds,
        )
        self.assertEqual(jobs_by_id["telegram-inbound-checks"].trigger.interval.total_seconds(), 5)
        self.assertEqual(jobs_by_id["telegram-admin-refresh-checks"].trigger.interval.total_seconds(), 30)

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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)
        now = datetime(2026, 3, 13, 6, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service.run_scheduled_dispatch(now=now)

        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].category, "morning_summary")
        self.assertTrue(self.db.has_notification_event(student_india, "morning_digest", "2026-03-13"))
        self.assertFalse(self.db.has_notification_event(student_utc, "morning_digest", "2026-03-13"))

    def test_lecture_schedule_notifications_respect_send_morning_disabled_action(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Schedule Blocked Student",
            user_name="schedule_blocked_student",
            password_encrypted="secret",
            telegram_chat_id="123456789",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json='["send_morning"]',
            timezone="Asia/Kolkata",
        )
        target_date = date(2026, 3, 16)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="math101",
                    subject_name="Mathematics",
                    teacher_name="Prof A",
                    raw_cell="Mathematics\nProf A\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=FakeERP(), telegram=telegram)

        service.run_lecture_schedule_sweep(now=datetime(2026, 3, 16, 8, 59, tzinfo=ZoneInfo("Asia/Kolkata")))

        self.assertEqual(telegram.messages, [])
        self.assertEqual(service.list_message_history(), [])

    def test_attendance_notifications_respect_send_attendance_summary_disabled_action(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Attendance Blocked Student",
            user_name="attendance_blocked_student",
            password_encrypted="secret",
            telegram_chat_id="123456789",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json='["send_attendance_summary"]',
            timezone="Asia/Kolkata",
        )
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=date(2026, 3, 14),
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="math101",
                    subject_name="Mathematics",
                    teacher_name="Prof A",
                    raw_cell="Mathematics\nProf A\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Mathematics",
                        "SubjectCode": "MATH101",
                        "EMPNAME": "Prof A",
                        "TotalLecture": "1",
                        "TotalPresent": "1",
                        "Percentage": "100.00%",
                    }
                ]
            }
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)
        service._local_now = lambda: datetime(2026, 3, 14, 10, 30, tzinfo=ZoneInfo("Asia/Kolkata"))  # type: ignore[method-assign]

        service.run_due_checks()

        self.assertEqual(telegram.messages, [])
        self.assertEqual(service.list_message_history(), [])
        event = self.db.get_lecture_events_for_day(student_id, date(2026, 3, 14))[0]
        self.assertEqual(event.status, "notified_present")

    def test_erp_session_expired_alert_still_sends_when_attendance_feature_is_disabled(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Critical Alert Student",
            user_name="critical_alert_student",
            password_encrypted="secret",
            telegram_chat_id="5570554766",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json='["send_attendance_summary"]',
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        stale_at = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
        self._set_student_activity_times(student_id, session_updated_at=stale_at, last_erp_sync_at=stale_at)
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(erp=FakeERP(auth_required=True), telegram=telegram)

        service.run_monitor_sweep(now=datetime.now(ZoneInfo("Asia/Kolkata")))

        self.assertEqual(len(telegram.messages), 2)
        categories = {item.category for item in service.list_message_history()}
        self.assertEqual(categories, {"erp_session_expired", "erp_session_expired_admin"})

    def test_morning_summary_still_sends_when_shortage_notifications_are_disabled(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Shortage Blocked Student",
            user_name="shortage_blocked_student",
            password_encrypted="secret",
            telegram_chat_id="123456789",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json='["send_shortage_report"]',
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
                        "EMPNAME": "Prof ERP",
                        "TotalLecture": "4",
                        "TotalPresent": "3",
                        "Percentage": "75.00%",
                    }
                ]
            },
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service.send_morning_update(student_id, target_date=date(2026, 3, 15), force=True)

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Morning Schedule Update", telegram.messages[0][2])
        categories = {item.category for item in service.list_message_history()}
        self.assertEqual(categories, {"morning_summary"})

    def test_substitution_alerts_respect_send_substitution_report_disabled_action(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Substitution Blocked Student",
            user_name="substitution_blocked_student",
            password_encrypted="secret",
            telegram_chat_id="123456789",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json='["send_substitution_report"]',
            timezone="Asia/Kolkata",
        )
        target_date = date(2026, 3, 16)
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
        erp = FakeERP(
            substitutions_payload={
                "state": [
                    {
                        "SubsDate": target_date.strftime("%d/%m/%Y"),
                        "Period": "10:00 - 11:00",
                        "Time": "10:00 - 11:00",
                        "Subject": "Physics",
                        "Employee": "Prof B",
                        "SubsSubject": "Physics Lab",
                        "SubsEmployee": "Prof C",
                    }
                ]
            }
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service.run_substitution_sweep(now=datetime(2026, 3, 16, 9, 45, tzinfo=ZoneInfo("Asia/Kolkata")))

        self.assertEqual(telegram.messages, [])
        self.assertEqual(service.list_message_history(), [])

    def test_database_creates_parent_directory_for_custom_path(self) -> None:
        nested_db_path = self.tmp / "nested" / "data" / "bot.sqlite3"

        db = Database(nested_db_path)
        db.init()

        self.assertTrue(nested_db_path.parent.exists())
        self.assertTrue(nested_db_path.exists())

    def test_save_student_rejects_telegram_username_input(self) -> None:
        service = self._make_service()

        with self.assertRaises(StudentValidationError):
            service.save_student(
                student_id=None,
                student_label="Telegram Student",
                user_name="telegram_user",
                password="secret",
                telegram_chat_id="https://t.me/gunda872",
                enabled=True,
                timezone="Asia/Kolkata",
            )

    def test_send_test_message_dispatches_to_telegram_only(self) -> None:
        student_id = self._add_student(label="Multi Channel Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        self.db.upsert_student(
            student_id=student_id,
            student_label=student.student_label,
            user_name=student.user_name,
            password_encrypted=student.password_encrypted,
            telegram_chat_id="123456789",
            enabled=student.enabled,
            timezone=student.timezone,
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        service.send_test_message(student_id)

        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual({item.channel for item in history}, {"telegram"})

    def test_delivery_targets_respect_notification_channel_mode(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Routing Student",
            user_name="routing_user",
            password_encrypted="secret",
            telegram_chat_id="123456789",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json="[]",
            timezone="Asia/Kolkata",
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)

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
            telegram_chat_id="123456789",
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
            telegram_chat_id="123456789",
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
        service = self._make_service(erp=erp)

        body = service.send_attendance_summary_report(student_id, target_date=date(2026, 3, 16), force=True)

        self.assertIn("Compiler Design (CS36313) | Faculty: MD. IQBAL", body)
        self.assertIn("Artificial Intelligence (CS36311) | Faculty: AMIT KUMAR", body)
        self.assertNotIn("Wrong Monthly Faculty", body)
        self.assertNotIn("Wrong AI Faculty", body)

    def test_send_attendance_summary_report_dispatches_to_telegram_only(self) -> None:
        student_id = self._add_student(label="Attendance Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        self.db.upsert_student(
            student_id=student_id,
            student_label=student.student_label,
            user_name=student.user_name,
            password_encrypted=student.password_encrypted,
            telegram_chat_id="123456789",
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        body = service.send_attendance_summary_report(student_id, force=True)

        self.assertIn("Attendance Summary Report", body)
        self.assertIn("Totals present: 15", body)
        self.assertIn("Total lectures: 18", body)
        self.assertIn("Total absent: 3", body)
        self.assertIn("Attendance Shortage Report", body)
        self.assertIn("Threshold: 75% per subject", body)
        self.assertIn("Scope: totals below include only subjects currently at risk.", body)
        self.assertIn("Subjects At Risk", body)
        self.assertIn("Attendance: 6/8 (75.00%)", body)
        self.assertIn("Risk: No more safe absences remain.", body)
        self.assertIn("Subject-wise Attendance", body)
        self.assertIn("Mathematics (MATH101)", body)
        self.assertIn("Faculty: Prof Timetable", body)
        self.assertIn("Percentage: 75.00%", body)
        self.assertIn("Total lectures: 8", body)
        self.assertIn("Present: 6", body)
        self.assertIn("Absent: 2", body)
        self.assertIn("Physics Lab (PHYL102)", body)
        self.assertIn("Faculty: Prof Timetable Physics", body)
        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual({item.category for item in history}, {"attendance_summary_report"})

    def test_send_substitution_report_dispatches_to_telegram_only(self) -> None:
        student_id = self._add_student(label="Substitution Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        self.db.upsert_student(
            student_id=student_id,
            student_label=student.student_label,
            user_name=student.user_name,
            password_encrypted=student.password_encrypted,
            telegram_chat_id="123456789",
            enabled=student.enabled,
            timezone=student.timezone,
        )
        target_date = date(2026, 3, 16)
        erp = FakeERP(
            detail_payload={"state": [{"StudentName": "Demo Student"}]},
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 10:00", "Subject": "Mathematics", "Employee": "Prof A", "Time": "09:00 - 10:00"},
                    {"Period": "10:00 - 11:00", "Subject": "Physics", "Employee": "Prof B", "Time": "10:00 - 11:00"},
                ]
            },
            substitutions_payload={
                "state": [
                    {
                        "SubsDate": target_date.strftime("%d/%m/%Y"),
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        body = service.send_substitution_report(student_id, target_date=target_date, force=True)

        self.assertIn("Substitution Check Report", body)
        self.assertIn("Generated at:", body)
        self.assertIn("Today's Lectures", body)
        self.assertIn("Substitute Lectures", body)
        self.assertIn("Physics Lab", body)
        self.assertIn("Faculty: Prof C", body)
        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(telegram.messages[0][0], "123456789")
        self.assertEqual(telegram.messages[0][1], "attendance")
        self.assertIn("Substitution Check Report", telegram.messages[0][2])
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual({item.category for item in history}, {"substitution_report"})

    def test_attendance_summary_report_marks_shortage_status_clear_when_all_subjects_are_safe(self) -> None:
        student_id = self._add_student(label="Safe Attendance Student", timezone="Asia/Kolkata")
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Biology",
                        "Employee": "Prof Biology",
                        "Time": "09:00 - 10:00",
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
        service = self._make_service(erp=erp, telegram=ConfiguredTelegram())

        body = service.send_attendance_summary_report(student_id, target_date=date(2026, 3, 16), force=True)

        self.assertIn("Attendance Shortage Report", body)
        self.assertIn("Threshold: 75% per subject", body)
        self.assertIn("Status: Clear", body)
        self.assertIn("No subject is currently at or below the attendance shortage threshold.", body)

    def test_manual_attendance_summary_auth_expiry_notifies_student_and_admin_on_telegram(self) -> None:
        class AttendanceAuthERP(FakeERP):
            def get_attendance_summary(self, student):
                raise AuthenticationRequired("expired")

        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Manual Expired Student",
            user_name="manual_expired_student",
            password_encrypted="secret",
            telegram_chat_id="5570554766",
            enabled=True,
            notification_channel_mode="telegram_only",
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(
            erp=AttendanceAuthERP(),
            telegram=telegram,
        )

        with self.assertRaises(AuthenticationRequired):
            service.send_attendance_summary_report(student_id, target_date=date(2026, 3, 16), force=True)

        self.assertEqual(len(telegram.messages), 2)
        chats = [item[0] for item in telegram.messages]
        bodies = [item[2] for item in telegram.messages]
        self.assertIn("5570554766", chats)
        self.assertIn("5570554765", chats)
        self.assertTrue(any("Status: Your ERP session has expired" in body for body in bodies))
        self.assertTrue(any("Status: The student's ERP session has expired" in body for body in bodies))
        self.assertTrue(any("Manual Expired Student" in body for body in bodies))
        self.assertTrue(any(f"/students/{student_id}/login" in body for body in bodies))
        self.assertIsNotNone(self.db.get_pending_login(student_id))
        history = service.list_message_history()
        self.assertEqual(len(history), 2)
        self.assertEqual({item.category for item in history}, {"erp_session_expired", "erp_session_expired_admin"})

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

    def test_approve_application_request_allows_custom_website_credentials(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        result = service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass-123",
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="5570554766",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )
        telegram.messages.clear()

        approval = service.approve_application_request(
            int(result["id"]),
            site_login_username="tapendra-web",
            site_login_password="site-pass-789",
        )

        student = service.get_student(int(approval["student_id"]))
        application = service.get_application_request(int(result["id"]))
        self.assertIsNotNone(student)
        assert student is not None
        self.assertEqual(student.student_label, "Tapendra")
        self.assertEqual(student.user_name, "23030682")
        self.assertEqual(student.site_login_username, "tapendra-web")
        self.assertEqual(student.telegram_chat_id, "5570554766")
        self.assertEqual(student.reg_id, "8027")
        self.assertTrue(student.enabled)
        self.assertIsNotNone(application)
        assert application is not None
        self.assertEqual(application.status, "accepted")
        self.assertEqual(approval["website_password_source"], "custom")
        self.assertEqual(approval["site_login_password_display"], "site-pass-789")
        self.assertEqual(len(telegram.messages), 2)
        chats = [message[0] for message in telegram.messages]
        bodies = [message[2] for message in telegram.messages]
        self.assertIn("5570554766", chats)
        self.assertIn("5570554765", chats)
        self.assertTrue(any("application has been approved" in body.lower() for body in bodies))
        self.assertTrue(any("Website password: site-pass-789" in body for body in bodies))
        self.assertTrue(any("/login" in body for body in bodies))
        self.assertTrue(any("Application approved from website dashboard." in body for body in bodies))

    def test_approve_application_request_reports_explicit_telegram_errors_when_notifications_cannot_be_sent(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = False
        service = self._make_service(telegram=telegram)
        result = service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass-123",
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )

        approval = service.approve_application_request(int(result["id"]))

        self.assertFalse(approval["applicant_notification_sent"])
        self.assertFalse(approval["admin_notification_sent"])
        self.assertEqual(approval["applicant_notification_error"], "Telegram bot is not configured.")
        self.assertEqual(approval["admin_notification_error"], "Telegram bot is not configured.")

    def test_student_telegram_menu_shows_only_the_linked_profile(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        self.db.upsert_student(
            student_id=None,
            student_label="Own Student",
            user_name="own_erp",
            password_encrypted="secret",
            telegram_chat_id="2001",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.upsert_student(
            student_id=None,
            student_label="Other Student",
            user_name="other_erp",
            password_encrypted="secret",
            telegram_chat_id="2002",
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
            telegram_chat_id="2001",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.upsert_student(
            student_id=None,
            student_label="Other Student",
            user_name="other_erp",
            password_encrypted="secret",
            telegram_chat_id="2002",
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
            telegram_chat_id="2001",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.upsert_student(
            student_id=None,
            student_label="Other Student",
            user_name="other_erp",
            password_encrypted="secret",
            telegram_chat_id="2002",
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
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="5570554766",
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
        self.assertIn("Website login username: tapendra-site", telegram.messages[1][2])
        self.assertIn("ERP password: erp-pass", telegram.messages[1][2])

    def test_admin_telegram_applications_command_hides_accepted_requests(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        result = service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass-123",
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="5570554766",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )
        service.approve_application_request(
            int(result["id"]),
            site_login_username="tapendra-web",
            site_login_password="site-pass-789",
        )
        telegram.messages.clear()

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/applications"})

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("No pending application requests right now.", telegram.messages[0][2])
        self.assertNotIn("Tapendra", telegram.messages[0][2])

    def test_telegram_application_accept_callback_creates_student_profile(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass-123",
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="5570554766",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )
        telegram.messages.clear()

        service._handle_telegram_callback(
            {
                "id": "cb-application-accept",
                "data": "tg:application:1:accept",
                "message": {"chat": {"id": "5570554765"}},
            }
        )

        application = service.get_application_request(1)
        students = service.list_students()
        self.assertIsNotNone(application)
        assert application is not None
        self.assertEqual(application.status, "accepted")
        self.assertEqual(len(students), 1)
        self.assertEqual(students[0].site_login_username, "tapendra-site")
        self.assertEqual(telegram.callback_answers[-1], ("cb-application-accept", "Application approved.", False))
        self.assertTrue(any(msg_chat == "5570554765" and "Application approved." in body for msg_chat, _, body in telegram.messages))
        self.assertTrue(any("password you created during website sign-up" in body for _, _, body in telegram.messages))
        self.assertTrue(any(msg_chat == "5570554766" and "approved" in body.lower() for msg_chat, _, body in telegram.messages))

    def test_telegram_application_reject_callback_closes_request(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram)
        service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass-123",
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="5570554766",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )
        telegram.messages.clear()

        service._handle_telegram_callback(
            {
                "id": "cb-application-reject",
                "data": "tg:application:1:reject",
                "message": {"chat": {"id": "5570554765"}},
            }
        )

        application = service.get_application_request(1)
        self.assertIsNotNone(application)
        assert application is not None
        self.assertEqual(application.status, "rejected")
        self.assertEqual(service.list_students(), [])
        self.assertEqual(telegram.callback_answers[-1], ("cb-application-reject", "Application rejected.", False))
        self.assertTrue(any(msg_chat == "5570554765" and "Application rejected." in body for msg_chat, _, body in telegram.messages))
        self.assertTrue(any(msg_chat == "5570554766" and "reviewed and was not approved" in body.lower() for msg_chat, _, body in telegram.messages))

    def test_clear_application_request_requires_reviewed_status(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        result = service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass-123",
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="5570554766",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )

        with self.assertRaisesRegex(StudentValidationError, "Review the application before clearing it from the dashboard."):
            service.clear_application_request(int(result["id"]))

    def test_clear_application_request_removes_reviewed_record(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        result = service.submit_application_request(
            applicant_name="Tapendra Chaudhary",
            student_label="Tapendra",
            user_name="23030682",
            password="erp-pass-123",
            site_login_username="tapendra-site",
            site_login_password="site-pass-123",
            telegram_chat_id="5570554766",
            timezone="Asia/Kolkata",
            reg_id="8027",
            note="Please add my profile.",
            created_from_ip="website",
        )
        service.reject_application_request(int(result["id"]))

        cleared = service.clear_application_request(int(result["id"]))

        self.assertEqual(cleared.student_label, "Tapendra")
        self.assertIsNone(service.get_application_request(int(result["id"])))

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
            telegram_chat_id="5570554765",
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

    def test_telegram_substitution_command_sends_student_report(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Substitution Student",
            user_name="sub_student",
            password_encrypted="secret",
            telegram_chat_id="5570554765",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        target_date = date(2026, 3, 16)
        erp = FakeERP(
            detail_payload={"state": [{"StudentName": "Substitution Student"}]},
            timetable_payload={
                "state": [
                    {
                        "Period": "09:00 - 10:00",
                        "Subject": "Mathematics",
                        "Employee": "Prof A",
                        "Time": "09:00 - 10:00",
                    },
                    {
                        "Period": "10:00 - 11:00",
                        "Subject": "Physics",
                        "Employee": "Prof B",
                        "Time": "10:00 - 11:00",
                    },
                ]
            },
            substitutions_payload={
                "state": [
                    {
                        "SubsDate": target_date.strftime("%d/%m/%Y"),
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
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(telegram=telegram, erp=erp)

        with patch.object(service, "_now_for_student", return_value=datetime(2026, 3, 16, 9, 5, tzinfo=ZoneInfo("Asia/Kolkata"))):
            service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": f"/substitution {student_id}"})

        self.assertEqual(len(service.telegram.messages), 1)
        self.assertIn("Substitution Check Report", service.telegram.messages[-1][2])
        self.assertIn("Physics Lab", service.telegram.messages[-1][2])
        self.assertIn("automatic", service.telegram.messages[-1][2].lower())

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

    def test_telegram_substitution_callback_acknowledges_before_sending_report(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Substitution Student",
            user_name="sub_student",
            password_encrypted="secret",
            telegram_chat_id="5570554765",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        target_date = date(2026, 3, 16)
        erp = FakeERP(
            detail_payload={"state": [{"StudentName": "Substitution Student"}]},
            timetable_payload={
                "state": [
                    {
                        "Period": "10:00 - 11:00",
                        "Subject": "Physics",
                        "Employee": "Prof B",
                        "Time": "10:00 - 11:00",
                    }
                ]
            },
            substitutions_payload={
                "state": [
                    {
                        "SubsDate": target_date.strftime("%d/%m/%Y"),
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
        service = self._make_service(erp=erp, telegram=FakeTelegram())
        service.telegram.configured = True

        with patch.object(service, "_now_for_student", return_value=datetime(2026, 3, 16, 9, 5, tzinfo=ZoneInfo("Asia/Kolkata"))):
            service._handle_telegram_callback(
                {
                    "id": "cb-substitution",
                    "data": f"tg:student:{student_id}:substitution",
                    "message": {"chat": {"id": "5570554765"}},
                }
            )

        self.assertEqual(service.telegram.callback_answers[-1], ("cb-substitution", "Processing request.", False))
        self.assertTrue(service.telegram.messages)
        self.assertIn("Substitution Check Report", service.telegram.messages[-1][2])
        self.assertIn("Physics Lab", service.telegram.messages[-1][2])

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

        self.assertEqual(len(telegram.messages), 2)
        self.assertEqual(telegram.callback_answers[-1], ("cb-test-2", "This action was already sent recently.", True))

    def test_telegram_shortage_command_does_not_echo_duplicate_report_to_same_chat(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Tapendra",
            user_name="23030682",
            password_encrypted="secret",
            telegram_chat_id="5570554765",
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
            telegram_chat_id="5570554765",
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

    def test_student_telegram_substitution_command_uses_only_the_linked_profile(self) -> None:
        telegram = FakeTelegram()
        telegram.configured = True
        target_date = date(2026, 3, 16)
        erp = FakeERP(
            detail_payload={"state": [{"StudentName": "Own Student"}]},
            timetable_payload={
                "state": [
                    {
                        "Period": "10:00 - 11:00",
                        "Subject": "Physics",
                        "Employee": "Prof B",
                        "Time": "10:00 - 11:00",
                    }
                ]
            },
            substitutions_payload={
                "state": [
                    {
                        "SubsDate": target_date.strftime("%d/%m/%Y"),
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
        service = self._make_service(telegram=telegram, erp=erp)
        self.db.upsert_student(
            student_id=None,
            student_label="Own Student",
            user_name="own_erp",
            password_encrypted="secret",
            telegram_chat_id="2001",
            enabled=True,
            timezone="Asia/Kolkata",
        )
        self.db.upsert_student(
            student_id=None,
            student_label="Other Student",
            user_name="other_erp",
            password_encrypted="secret",
            telegram_chat_id="2002",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        with patch.object(service, "_now_for_student", return_value=datetime(2026, 3, 16, 9, 5, tzinfo=ZoneInfo("Asia/Kolkata"))):
            service._handle_telegram_message({"chat": {"id": "2001"}, "text": "/substitution"})

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Substitution Check Report", telegram.messages[-1][2])
        self.assertIn("Student: Own Student", telegram.messages[-1][2])
        self.assertNotIn("Other Student", telegram.messages[-1][2])

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
            telegram_chat_id="5570554765",
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
        self.assertIn("QUMS Admin Operations", body)
        self.assertNotIn("Auto refresh", body)
        self.assertIn("Dashboard sync: Live sync on change", body)
        self.assertIn("Tapendra", body)
        self.assertIn("ERP user id: 23030682", body)
        self.assertIn("Telegram: 5570554765", body)
        self.assertIn("RegID: 8027", body)
        self.assertIn("Session updated:", body)
        self.assertIn("Last ERP sync:", body)
        self.assertIn("Last bot action:", body)
        self.assertIn("Profile status: Active", body)
        self.assertIn("Delivery: Telegram Only", body)
        self.assertIn("Disabled features: None", body)
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
            telegram_chat_id="5570554765",
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
        self.assertIn("Telegram: 5570554765", body)
        self.assertIn("RegID: 8027", body)
        self.assertIn("Session updated:", body)
        self.assertIn("Last ERP sync:", body)
        self.assertIn("Last bot action:", body)
        self.assertIn("Timezone: Asia/Kolkata", body)
        self.assertIn("Enabled: Yes", body)
        self.assertIn("Profile status: Active", body)
        self.assertIn("Delivery: Telegram Only", body)
        self.assertIn("Disabled features: None", body)
        self.assertIn("ERP session: ERP session saved.", body)
        self.assertIn("Recent bot activity: ERP sync completed for 2026-03-13.", body)

    def test_telegram_students_text_matches_dashboard_fields(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Tapendra",
            user_name="23030682",
            password_encrypted="secret",
            telegram_chat_id="5570554765",
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
        self.assertIn("Telegram: 5570554765", body)
        self.assertIn("RegID: 8027", body)
        self.assertIn("Session updated:", body)
        self.assertIn("Last ERP sync:", body)
        self.assertIn("Last bot action:", body)
        self.assertIn("Profile status: Active", body)
        self.assertIn("Delivery: Telegram Only", body)
        self.assertIn("Disabled features: None", body)
        self.assertIn("ERP session: ERP session saved.", body)
        self.assertIn("Recent bot activity: ERP sync completed for 2026-03-13.", body)

    def test_telegram_menu_command_opens_dashboard_instead_of_help_text(self) -> None:
        service = self._make_service(telegram=FakeTelegram())
        service.telegram.configured = True

        service._handle_telegram_message({"chat": {"id": "5570554765"}, "text": "/menu"})

        self.assertTrue(service.telegram.messages)
        body = service.telegram.messages[-1][2]
        self.assertIn("QUMS Admin Operations", body)
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
        self.assertTrue(any(item["command"] == "substitution" for item in telegram.commands))
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

        self.assertEqual(len(service.telegram.messages), 3)
        self.assertIn("configured delivery channels are working", service.telegram.messages[0][2])
        self.assertIn("already sent recently", service.telegram.messages[-1][2])

    def test_save_student_rejects_invalid_timezone(self) -> None:
        service = self._make_service()

        with self.assertRaises(ValueError):
            service.save_student(
                student_id=None,
                student_label="Timezone Student",
                user_name="timezone_user",
                password="secret",
                telegram_chat_id="",
                enabled=True,
                timezone="Mars/Olympus",
            )

    def test_save_student_rejects_duplicate_user_name_and_telegram_chat_id(self) -> None:
        service = self._make_service()
        service.save_student(
            student_id=None,
            student_label="First Student",
            user_name="duplicate_user",
            password="secret",
            telegram_chat_id="123456789",
            enabled=True,
            timezone="Asia/Kolkata",
        )

        with self.assertRaises(ValueError):
            service.save_student(
                student_id=None,
                student_label="Second Student",
                user_name="duplicate_user",
                password="secret",
                telegram_chat_id="",
                enabled=True,
                timezone="Asia/Kolkata",
            )

        with self.assertRaises(ValueError):
            service.save_student(
                student_id=None,
                student_label="Third Student",
                user_name="another_user",
                password="secret",
                telegram_chat_id="123456789",
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

    def test_monitor_sweep_dedupes_repeated_erp_expiry_alerts(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Monitor Student",
            user_name="monitor_student",
            password_encrypted="secret",
            telegram_chat_id="5570554765",
            enabled=True,
            notification_channel_mode="telegram_only",
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        stale_at = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
        self._set_student_activity_times(student_id, session_updated_at=stale_at, last_erp_sync_at=stale_at)
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(
            erp=FakeERP(auth_required=True),
            telegram=telegram,
        )
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        service.run_monitor_sweep(now=now)
        service.run_monitor_sweep(now=now)

        history = service.list_message_history()
        categories = {item.category for item in history}
        self.assertEqual(len(history), 1)
        self.assertIn("erp_session_expired", categories)

    def test_monitor_sweep_handles_status_lookup_failure_during_erp_expiry(self) -> None:
        student_id = self._add_student(label="Expired Student", timezone="Asia/Kolkata")
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        stale_at = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
        self._set_student_activity_times(student_id, session_updated_at=stale_at, last_erp_sync_at=stale_at)
        service = self._make_service(erp=FakeERP(auth_required=True), telegram=BrokenTelegram())
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        service.run_monitor_sweep(now=now)

        history = service.list_message_history()
        self.assertEqual(history, [])
        refreshed = self.db.get_student(student_id)
        assert refreshed is not None
        assert refreshed.session_updated_at is not None
        self.assertFalse(self.db.has_notification_event(student_id, "erp_session_expired", refreshed.session_updated_at))
        self.assertIn("ERP session expired", refreshed.last_login_status or "")

    def test_monitor_sweep_sends_erp_expiry_alert_via_telegram_when_telegram_lookup_fails(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Telegram Expired Student",
            user_name="telegram_expired_student",
            password_encrypted="secret",
            telegram_chat_id="5570554766",
            enabled=True,
            notification_channel_mode="telegram_only",
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        stale_at = (datetime.now(tz=ZoneInfo("UTC")) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
        self._set_student_activity_times(student_id, session_updated_at=stale_at, last_erp_sync_at=stale_at)
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(
            erp=FakeERP(auth_required=True),
            telegram=telegram,
        )
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        service.run_monitor_sweep(now=now)

        self.assertEqual(len(telegram.messages), 2)
        chats = [item[0] for item in telegram.messages]
        bodies = [item[2] for item in telegram.messages]
        self.assertIn("5570554766", chats)
        self.assertIn("5570554765", chats)
        self.assertTrue(any("ERP Session Alert" in body for body in bodies))
        self.assertTrue(any("Lecture-end attendance scans and summary checks are paused" in body for body in bodies))
        self.assertTrue(any("Telegram Expired Student" in body for body in bodies))
        self.assertTrue(any(f"/students/{student_id}/login" in body for body in bodies))
        self.assertIsNotNone(self.db.get_pending_login(student_id))
        history = service.list_message_history()
        self.assertEqual(len(history), 2)
        self.assertEqual({item.channel for item in history}, {"telegram"})
        self.assertEqual({item.category for item in history}, {"erp_session_expired", "erp_session_expired_admin"})

    def test_monitor_sweep_skips_fresh_session_probe_right_after_login(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Fresh Login Student",
            user_name="fresh_login_student",
            password_encrypted="secret",
            telegram_chat_id="5570554765",
            enabled=True,
            notification_channel_mode="telegram_only",
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(
            erp=FakeERP(auth_required=True),
            telegram=telegram,
        )
        now = datetime.now(ZoneInfo("Asia/Kolkata"))

        service.run_monitor_sweep(now=now)

        self.assertEqual(telegram.messages, [])
        refreshed = self.db.get_student(student_id)
        assert refreshed is not None
        self.assertEqual(refreshed.erp_status_text, "ERP session active.")

    def test_run_due_checks_sends_erp_expiry_alert_when_attendance_scan_hits_authentication_required(self) -> None:
        class AttendanceAuthERP(FakeERP):
            def get_attendance_summary(self, student):
                raise AuthenticationRequired("expired")

        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Due Check Expired Student",
            user_name="due_check_expired_student",
            password_encrypted="secret",
            telegram_chat_id="5570554766",
            enabled=True,
            notification_channel_mode="telegram_only",
            timezone="Asia/Kolkata",
        )
        self.db.update_student_session(
            student_id=student_id,
            cookies_json="[]",
            last_login_status="ERP session active.",
        )
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=date(2026, 3, 14),
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="tsdv",
                    subject_name="Technical Skills Development-V",
                    teacher_name="Prof Live",
                    raw_cell="Technical Skills Development-V\nProf Live\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(
            erp=AttendanceAuthERP(),
            telegram=telegram,
        )
        service._local_now = lambda: datetime(2026, 3, 14, 10, 5, tzinfo=ZoneInfo("Asia/Kolkata"))  # type: ignore[method-assign]

        service.run_due_checks()

        self.assertEqual(len(telegram.messages), 2)
        chats = [item[0] for item in telegram.messages]
        bodies = [item[2] for item in telegram.messages]
        self.assertIn("5570554766", chats)
        self.assertIn("5570554765", chats)
        self.assertTrue(any("ERP Session Alert" in body for body in bodies))
        self.assertTrue(any("Lecture-end attendance scans and summary checks are paused" in body for body in bodies))
        self.assertTrue(any("Due Check Expired Student" in body for body in bodies))
        self.assertTrue(any(f"/students/{student_id}/login" in body for body in bodies))
        self.assertIsNotNone(self.db.get_pending_login(student_id))
        history = service.list_message_history()
        self.assertEqual(len(history), 2)
        self.assertEqual({item.channel for item in history}, {"telegram"})
        self.assertEqual({item.category for item in history}, {"erp_session_expired", "erp_session_expired_admin"})

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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

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

    def test_substitution_sweep_clears_stale_timeout_message_after_recovery(self) -> None:
        student_id = self._add_student(label="Recovery Student", timezone="Asia/Kolkata")
        self.db.update_student_status(student_id, "Substitution check failed: temporary upstream timeout")
        service = self._make_service(
            erp=FakeERP(substitutions_payload={"state": []}),
            telegram=ConfiguredTelegram(),
        )
        now = datetime(2026, 3, 15, 14, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service.run_substitution_sweep(now=now)

        student = self.db.get_student(student_id)
        assert student is not None
        self.assertEqual(student.last_bot_activity_text, "Substitution check recovered for 2026-03-15.")
        self.assertEqual(student.last_login_status, "Substitution check recovered for 2026-03-15.")
        self.assertIsNotNone(student.last_erp_sync_at)

    def test_idempotent_send_prevents_duplicate_delivery(self) -> None:
        student_id = self._add_student(label="Idempotent Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        service._send_notification(
            student,
            "Attendance Update\n\nStatus: Present",
            message_kind="attendance",
            history_category="attendance_update",
            idempotency_key="attendance_update:test-key",
        )
        service._send_notification(
            student,
            "Attendance Update\n\nStatus: Present",
            message_kind="attendance",
            history_category="attendance_update",
            idempotency_key="attendance_update:test-key",
        )

        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].idempotency_key, "attendance_update:test-key:telegram")

    def test_task_dispatcher_claims_each_periodic_slot_once(self) -> None:
        self.db.upsert_student(
            student_id=None,
            student_label="Dispatch Student",
            user_name="dispatch_student",
            password_encrypted="secret",
            telegram_chat_id="5570554765",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json="[]",
            timezone="Asia/Kolkata",
        )
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 10:00", "Subject": "Mathematics", "Employee": "Prof A", "Time": "09:00 - 10:00"}
                ]
            }
        )
        telegram = FakeTelegram()
        telegram.configured = True
        service = self._make_service(erp=erp, telegram=telegram)
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
        self.assertEqual(len(telegram.messages), 1)

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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service.send_morning_update(student_id, target_date=date(2026, 3, 15))

        categories = {item.category for item in service.list_message_history()}
        self.assertIn("attendance_shortage_report", categories)
        shortage_messages = [body for _, _, body in telegram.messages if "Attendance Shortage Report" in body]
        self.assertEqual(len(shortage_messages), 1)
        subject_shortage = next(body for body in shortage_messages if "Mathematics (MATH101)" in body)
        self.assertIn("Threshold: 75% per subject", subject_shortage)
        self.assertIn("Faculty: Prof Timetable", subject_shortage)
        self.assertIn("Total absent: 1", subject_shortage)
        self.assertNotIn("Prof ERP", subject_shortage)

    def test_subject_shortage_report_is_sent_when_attendance_drops_below_75_percent(self) -> None:
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service.send_morning_update(student_id, target_date=date(2026, 3, 16))

        shortage_messages = [body for _, _, body in telegram.messages if "Attendance Shortage Report" in body]
        self.assertEqual(len(shortage_messages), 1)
        subject_shortage = next(body for body in shortage_messages if "Physics (PHY101)" in body)
        self.assertIn("Faculty: Prof Timetable Physics", subject_shortage)
        self.assertIn("Attendance: 7/10 (70.00%)", subject_shortage)
        self.assertIn("Total absent: 3", subject_shortage)
        self.assertIn("Threshold: 75% per subject", subject_shortage)

    def test_subject_shortage_report_is_not_sent_above_75_percent(self) -> None:
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service.send_morning_update(student_id, target_date=date(2026, 3, 16))

        shortage_messages = [body for _, _, body in telegram.messages if "Attendance Shortage Report" in body]
        self.assertEqual(shortage_messages, [])
        categories = {item.category for item in service.list_message_history()}
        self.assertNotIn("attendance_shortage_report", categories)

    def test_shortage_report_notification_does_not_repeat_on_unchanged_followup_scan(self) -> None:
        student_id = self._add_student(label="Shortage Dedup Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service.send_morning_update(student_id, target_date=date(2026, 3, 15))
        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 15, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        shortage_messages = [body for _, _, body in telegram.messages if "Attendance Shortage Report" in body]
        self.assertEqual(len(shortage_messages), 1)

    def test_shortage_report_scan_uses_weekly_reference_faculty_when_today_has_no_matching_event(self) -> None:
        student_id = self._add_student(label="Weekly Faculty Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {
                        "Day/Period": "Friday",
                        "P1": "Employability Skills III(Personality Development Program)(SE36366) (A-010),MANVI TYAGI",
                    }
                ],
                "col": "Day/Period,P1",
            },
            attendance_payload={
                "state": [
                    {
                        "Subject": "Employability Skills III(Personality Development Program)",
                        "SubjectCode": "SE36366",
                        "EMPNAME": "",
                        "TotalLecture": "15",
                        "TotalPresent": "11",
                        "Percentage": "73.33%",
                    }
                ]
            },
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 14, 17, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        shortage_messages = [body for _, _, body in telegram.messages if "Attendance Shortage Report" in body]
        self.assertEqual(len(shortage_messages), 1)
        self.assertIn("Faculty: MANVI TYAGI", shortage_messages[0])
        self.assertNotIn("Faculty: Not available", shortage_messages[0])

    def test_live_attendance_scan_sends_present_alert_during_lecture_before_due_window(self) -> None:
        student_id = self._add_student(label="Live Present Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 14)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="tsdv",
                    subject_name="Technical Skills Development-V",
                    teacher_name="Prof Live",
                    raw_cell="Technical Skills Development-V\nProf Live\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="tsdv101",
            subject_name="Technical Skills Development-V",
            subject_code="TSDV101",
            teacher_name="Prof Live",
            total_lecture=4,
            total_present=4,
            percentage="100.00%",
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Technical Skills Development-V",
                        "SubjectCode": "TSDV101",
                        "EMPNAME": "Prof Live",
                        "TotalLecture": "5",
                        "TotalPresent": "5",
                        "Percentage": "100.00%",
                    }
                ]
            }
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 14, 9, 42, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[0][2]
        self.assertIn("Attendance Update", body)
        self.assertIn("Final status: Present", body)
        self.assertIn("Technical Skills Development-V", body)
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        self.assertEqual(event.status, "notified_present")

    def test_live_attendance_scan_sends_pending_alert_at_lecture_end_before_grace_window(self) -> None:
        student_id = self._add_student(label="Live Pending Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 14)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="tsdv",
                    subject_name="Technical Skills Development-V",
                    teacher_name="Prof Live",
                    raw_cell="Technical Skills Development-V\nProf Live\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="tsdv101",
            subject_name="Technical Skills Development-V",
            subject_code="TSDV101",
            teacher_name="Prof Live",
            total_lecture=4,
            total_present=4,
            percentage="100.00%",
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Technical Skills Development-V",
                        "SubjectCode": "TSDV101",
                        "EMPNAME": "Prof Live",
                        "TotalLecture": "4",
                        "TotalPresent": "4",
                        "Percentage": "100.00%",
                    }
                ]
            }
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 14, 9, 56, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[0][2]
        self.assertIn("Attendance Pending Update", body)
        self.assertIn("Current status: Not marked yet", body)
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        self.assertEqual(event.status, "notified_unmarked")
        self.assertIsNotNone(event.check_after)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 14, 10, 1, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        self.assertEqual(len(telegram.messages), 1)

    def test_live_attendance_scan_sends_lecture_finished_status_once_after_present_marking(self) -> None:
        student_id = self._add_student(label="Lecture Finish Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 14)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="tsdv",
                    subject_name="Technical Skills Development-V",
                    teacher_name="Prof Live",
                    raw_cell="Technical Skills Development-V\nProf Live\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        self.db.upsert_attendance_snapshot(
            student_id=student_id,
            subject_key="tsdv101",
            subject_name="Technical Skills Development-V",
            subject_code="TSDV101",
            teacher_name="Prof Live",
            total_lecture=4,
            total_present=4,
            percentage="100.00%",
        )
        erp = FakeERP(
            attendance_payload={
                "state": [
                    {
                        "Subject": "Technical Skills Development-V",
                        "SubjectCode": "TSDV101",
                        "EMPNAME": "Prof Live",
                        "TotalLecture": "5",
                        "TotalPresent": "5",
                        "Percentage": "100.00%",
                    }
                ]
            }
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 14, 9, 42, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Attendance Update", telegram.messages[0][2])

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 14, 9, 56, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        self.assertEqual(len(telegram.messages), 2)
        lecture_finish_body = telegram.messages[1][2]
        self.assertIn("Lecture Finished Attendance Status", lecture_finish_body)
        self.assertIn("Final status: Present", lecture_finish_body)
        self.assertIn("Technical Skills Development-V", lecture_finish_body)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 14, 10, 2, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        self.assertEqual(len(telegram.messages), 2)

    def test_automation_status_reports_poll_based_next_scan_and_next_lecture_milestone(self) -> None:
        student_id = self._add_student(label="Automation Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 14)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="tsdv",
                    subject_name="Technical Skills Development-V",
                    teacher_name="Prof Live",
                    raw_cell="Technical Skills Development-V\nProf Live\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        self.db.mark_student_erp_sync(student_id, synced_at="2026-03-14T04:12:38+00:00")
        student = self.db.get_student(student_id)
        assert student is not None
        service = self._make_service()

        status = service.get_student_automation_status(
            student,
            now=datetime(2026, 3, 14, 9, 42, 38, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertEqual(status["next_attendance_subject"], "Technical Skills Development-V")
        self.assertEqual(status["attendance_poll_interval_minutes"], str(self.settings.attendance_poll_interval_minutes))
        self.assertIsNotNone(status["next_attendance_scan_iso"])
        self.assertIn("09:52", status["next_attendance_scan_label"] or "")
        self.assertIn("10:15", status["next_due_attendance_check_label"] or "")
        self.assertEqual(status["timetable_lecture_label"], "Current timetable lecture")
        self.assertEqual(status["timetable_lecture_subject"], "Technical Skills Development-V")
        self.assertEqual(status["timetable_lecture_time_label"], "09:00 - 09:55")

    def test_automation_status_uses_current_timetable_lecture_when_pending_subject_is_stale(self) -> None:
        student_id = self._add_student(label="Automation Context Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        target_date = date(2026, 3, 14)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="tsdv",
                    subject_name="Technical Skills Development-V",
                    teacher_name="Prof A",
                    raw_cell="Technical Skills Development-V\nProf A\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                ),
                LectureSlot(
                    slot_label="10:30 - 11:25",
                    subject_key="maths",
                    subject_name="Engineering Mathematics",
                    teacher_name="Prof B",
                    raw_cell="Engineering Mathematics\nProf B\n10:30 - 11:25",
                    start_time=time(10, 30),
                    end_time=time(11, 25),
                    is_break=False,
                ),
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        events = self.db.get_lecture_events_for_day(student_id, target_date)
        self.db.mark_event_status(
            events[0].id,
            "notified_unmarked",
            next_check_after=datetime(2026, 3, 14, 11, 22),
        )
        student = self.db.get_student(student_id)
        assert student is not None
        service = self._make_service()

        status = service.get_student_automation_status(
            student,
            now=datetime(2026, 3, 14, 11, 5, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertEqual(status["next_attendance_subject"], "Technical Skills Development-V")
        self.assertEqual(status["timetable_lecture_label"], "Current timetable lecture")
        self.assertEqual(status["timetable_lecture_subject"], "Engineering Mathematics")
        self.assertEqual(status["timetable_lecture_time_label"], "10:30 - 11:25")

    def test_run_lecture_schedule_sweep_sends_one_minute_upcoming_reminder_once_even_after_routine_resync(self) -> None:
        student_id = self._add_student(label="Upcoming Reminder Student", timezone="Asia/Kolkata")
        target_date = date(2026, 3, 16)
        slot = LectureSlot(
            slot_label="09:00 - 09:55",
            subject_key="math101",
            subject_name="Mathematics",
            teacher_name="Prof A",
            raw_cell="Mathematics\nProf A\n09:00 - 09:55",
            start_time=time(9, 0),
            end_time=time(9, 55),
            is_break=False,
        )
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[slot],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=FakeERP(), telegram=telegram)
        now = datetime(2026, 3, 16, 8, 59, tzinfo=ZoneInfo("Asia/Kolkata"))

        service.run_lecture_schedule_sweep(now=now)

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[0][2]
        self.assertIn("Upcoming Lecture Reminder", body)
        self.assertIn("Mathematics", body)
        self.assertIn("Lecture time: 09:00 - 09:55", body)
        self.assertIn("Substitute status: No substitute assigned", body)

        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[slot],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        later = datetime(2026, 3, 16, 8, 59, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service.run_lecture_schedule_sweep(now=later)

        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].category, "lecture_upcoming_alert")

    def test_run_lecture_schedule_sweep_sends_live_update_with_substitute_details_once(self) -> None:
        student_id = self._add_student(label="Live Substitute Reminder Student", timezone="Asia/Kolkata")
        target_date = date(2026, 3, 16)
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=FakeERP(), telegram=telegram)
        now = datetime(2026, 3, 16, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

        service.run_lecture_schedule_sweep(now=now)

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[0][2]
        self.assertIn("Live Lecture Update", body)
        self.assertIn("Physics Lab", body)
        self.assertIn("Current status: Lecture is now running", body)
        self.assertIn("Substitute status: Assigned for Physics Lab", body)
        self.assertIn("Substitute faculty: Prof C", body)
        self.assertIn("Original faculty: Prof B", body)

        service.run_lecture_schedule_sweep(now=datetime(2026, 3, 16, 10, 5, tzinfo=ZoneInfo("Asia/Kolkata")))

        self.assertEqual(len(telegram.messages), 1)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].category, "lecture_live_alert")

    def test_run_lecture_schedule_sweep_does_not_depend_on_attendance_summary_endpoint(self) -> None:
        class AttendanceAuthERP(FakeERP):
            def get_attendance_summary(self, student):
                raise AuthenticationRequired("expired")

        student_id = self._add_student(label="Reminder Independence Student", timezone="Asia/Kolkata")
        target_date = date(2026, 3, 16)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="math101",
                    subject_name="Mathematics",
                    teacher_name="Prof A",
                    raw_cell="Mathematics\nProf A\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=AttendanceAuthERP(), telegram=telegram)

        service.run_lecture_schedule_sweep(
            now=datetime(2026, 3, 16, 8, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
        )

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("Upcoming Lecture Reminder", telegram.messages[0][2])

    def test_run_lecture_schedule_sweep_skips_cached_alerts_when_erp_session_is_known_expired(self) -> None:
        student_id = self._add_student(label="Expired Reminder Student", timezone="Asia/Kolkata")
        target_date = date(2026, 3, 16)
        self.db.replace_lecture_events(
            student_id=student_id,
            event_date=target_date,
            slots=[
                LectureSlot(
                    slot_label="09:00 - 09:55",
                    subject_key="math101",
                    subject_name="Mathematics",
                    teacher_name="Prof A",
                    raw_cell="Mathematics\nProf A\n09:00 - 09:55",
                    start_time=time(9, 0),
                    end_time=time(9, 55),
                    is_break=False,
                )
            ],
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        self.db.update_student_erp_status(student_id, ERP_SESSION_EXPIRED_STATUS_TEXT)
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=FakeERP(), telegram=telegram)

        service.run_lecture_schedule_sweep(
            now=datetime(2026, 3, 16, 8, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
        )

        self.assertEqual(telegram.messages, [])
        self.assertEqual(service.list_message_history(), [])

    def test_run_due_checks_bootstraps_missing_todays_routine_and_sends_one_alert_per_finished_lecture(self) -> None:
        student_id = self.db.upsert_student(
            student_id=None,
            student_label="Bootstrap Student",
            user_name="bootstrap_student",
            password_encrypted="secret",
            telegram_chat_id="123456789",
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json="[]",
            timezone="Asia/Kolkata",
        )
        telegram = FakeTelegram()
        telegram.configured = True
        erp = FakeERP(
            timetable_payload={
                "state": [
                    {"Period": "09:00 - 09:55", "Subject": "Mathematics", "Employee": "Prof A", "Time": "09:00 - 09:55"},
                    {"Period": "10:00 - 10:55", "Subject": "Physics", "Employee": "Prof B", "Time": "10:00 - 10:55"},
                    {"Period": "11:00 - 11:55", "Subject": "Chemistry", "Employee": "Prof C", "Time": "11:00 - 11:55"},
                ]
            },
            attendance_payload={
                "state": [
                    {"Subject": "Mathematics", "SubjectCode": "MATH101", "EMPNAME": "Prof A", "TotalLecture": "1", "TotalPresent": "1", "Percentage": "100.00%"},
                    {"Subject": "Physics", "SubjectCode": "PHY101", "EMPNAME": "Prof B", "TotalLecture": "1", "TotalPresent": "1", "Percentage": "100.00%"},
                    {"Subject": "Chemistry", "SubjectCode": "CHEM101", "EMPNAME": "Prof C", "TotalLecture": "1", "TotalPresent": "1", "Percentage": "100.00%"},
                ]
            },
        )
        service = self._make_service(erp=erp, telegram=telegram)
        service._local_now = lambda: datetime(2026, 3, 14, 12, 30, tzinfo=ZoneInfo("Asia/Kolkata"))  # type: ignore[method-assign]

        service.run_due_checks()

        self.assertEqual(len(telegram.messages), 3)
        self.assertTrue(all("Attendance Update" in body for _, _, body in telegram.messages))
        self.assertEqual(len(service.list_message_history()), 3)
        tracked_events = self.db.get_lecture_events_for_day(student_id, date(2026, 3, 14))
        self.assertEqual(len([event for event in tracked_events if not event.is_break]), 3)
        self.assertTrue(all(event.status == "notified_present" for event in tracked_events if not event.is_break))

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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

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
        self.assertEqual(len(telegram.messages), 1)
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 13, 17, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        self.assertFalse(any("Attendance Summary Change Update" in body for _, _, body in telegram.messages))
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        service._process_attendance_scan(
            student,
            [],
            datetime(2026, 3, 13, 17, 5, tzinfo=ZoneInfo("Asia/Kolkata")),
        )

        messages = [body for _, _, body in telegram.messages if "Attendance Summary Change Update" in body]
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)
        now = datetime(2026, 3, 13, 17, 10, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_attendance_scan(student, [], now)
        service._process_attendance_scan(student, [], now + timedelta(minutes=1))

        messages = [body for _, _, body in telegram.messages if "Attendance Summary Change Update" in body]
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)

        preview = service.preview_today(student_id, target_date=date(2026, 3, 15))

        self.assertIn("Morning Schedule Update", preview)
        self.assertEqual(len(telegram.messages), 0)
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
        service = self._make_service(erp=erp, telegram=ConfiguredTelegram())

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
        service = self._make_service(telegram=ConfiguredTelegram())

        report = service.send_evening_report(student_id, target_date=target_date, force=True)

        self.assertIn("Class: A-010", report)
        self.assertIn("Faculty: AMIT KUMAR", report)

    def test_retry_sweep_recovers_failed_outbound_message(self) -> None:
        student_id = self._add_student(label="Retry Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        telegram = FlakyTelegram()
        service = self._make_service(telegram=telegram)

        with self.assertRaises(NotificationDeliveryError):
            service._send_notification(
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
        telegram = FlakyTelegram()
        service = self._make_service(telegram=telegram)

        with self.assertRaises(NotificationDeliveryError):
            service._send_notification(
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
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        claimed = self.db.claim_outbound_message(
            idempotency_key="attendance_update:dead-letter-test",
            student_id=student_id,
            channel="telegram",
            recipient=student.telegram_chat_id,
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
        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(len(service.list_message_history()), 1)

    def test_manual_dead_letter_retry_recovers_legacy_telegram_message_without_recipient(self) -> None:
        student_id = self._add_student(label="Legacy Dead Letter Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        claimed = self.db.claim_outbound_message(
            idempotency_key="attendance_update:legacy-dead-letter-test",
            student_id=student_id,
            channel="telegram",
            recipient="",
            category="attendance_update",
            message_kind="attendance",
            title="Attendance Update",
            body="Attendance Update\n\nStatus: Present",
        )
        self.assertTrue(claimed)
        self.db.mark_outbound_message_failed(
            "attendance_update:legacy-dead-letter-test",
            "legacy delivery target removed during channel migration",
            retry_limit=1,
            retry_backoff_seconds=1,
        )

        result = service.retry_dead_letter_message("attendance_update:legacy-dead-letter-test")

        self.assertIn("retried successfully", result.lower())
        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(telegram.messages[0][0], student.telegram_chat_id)
        history = service.list_message_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].recipient, student.telegram_chat_id)

    def test_manual_dead_letter_retry_uses_current_telegram_target_after_profile_change(self) -> None:
        student_id = self._add_student(label="Moved Chat Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        claimed = self.db.claim_outbound_message(
            idempotency_key="attendance_update:moved-chat-dead-letter-test",
            student_id=student_id,
            channel="telegram",
            recipient="5570554001",
            category="attendance_update",
            message_kind="attendance",
            title="Attendance Update",
            body="Attendance Update\n\nStatus: Present",
        )
        self.assertTrue(claimed)
        self.db.mark_outbound_message_failed(
            "attendance_update:moved-chat-dead-letter-test",
            "temporary failure",
            retry_limit=1,
            retry_backoff_seconds=1,
        )
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE students SET telegram_chat_id = ?, updated_at = ? WHERE id = ?",
                ("5570554999", datetime.now(ZoneInfo("UTC")).replace(microsecond=0).isoformat(), student_id),
            )

        result = service.retry_dead_letter_message("attendance_update:moved-chat-dead-letter-test")

        self.assertIn("retried successfully", result.lower())
        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(telegram.messages[0][0], "5570554999")

    def test_manual_dead_letter_retry_respects_paused_notifications_and_restores_dead_letter(self) -> None:
        student_id = self._add_student(label="Paused Dead Letter Student", timezone="Asia/Kolkata")
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        claimed = self.db.claim_outbound_message(
            idempotency_key="attendance_update:paused-dead-letter-test",
            student_id=student_id,
            channel="telegram",
            recipient="5570554766",
            category="attendance_update",
            message_kind="attendance",
            title="Attendance Update",
            body="Attendance Update\n\nStatus: Present",
        )
        self.assertTrue(claimed)
        self.db.mark_outbound_message_failed(
            "attendance_update:paused-dead-letter-test",
            "temporary failure",
            retry_limit=1,
            retry_backoff_seconds=1,
        )
        self.db.update_student_controls(
            student_id=student_id,
            enabled=True,
            notification_channel_mode="paused",
            disabled_actions_json="[]",
        )

        with self.assertRaises(NotificationDeliveryError):
            service.retry_dead_letter_message("attendance_update:paused-dead-letter-test")

        updated = self.db.get_outbound_message("attendance_update:paused-dead-letter-test")
        assert updated is not None
        self.assertEqual(updated.status, "dead_letter")
        self.assertIn("paused", updated.delivery_error_message or "")
        self.assertEqual(telegram.messages, [])

    def test_manual_dead_letter_retry_respects_disabled_feature_and_restores_dead_letter(self) -> None:
        student_id = self._add_student(label="Blocked Dead Letter Student", timezone="Asia/Kolkata")
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        claimed = self.db.claim_outbound_message(
            idempotency_key="attendance_update:blocked-dead-letter-test",
            student_id=student_id,
            channel="telegram",
            recipient="5570554766",
            category="attendance_update",
            message_kind="attendance",
            title="Attendance Update",
            body="Attendance Update\n\nStatus: Present",
        )
        self.assertTrue(claimed)
        self.db.mark_outbound_message_failed(
            "attendance_update:blocked-dead-letter-test",
            "temporary failure",
            retry_limit=1,
            retry_backoff_seconds=1,
        )
        self.db.update_student_controls(
            student_id=student_id,
            enabled=True,
            notification_channel_mode="telegram_only",
            disabled_actions_json='["send_attendance_summary"]',
        )

        with self.assertRaises(NotificationDeliveryError):
            service.retry_dead_letter_message("attendance_update:blocked-dead-letter-test")

        updated = self.db.get_outbound_message("attendance_update:blocked-dead-letter-test")
        assert updated is not None
        self.assertEqual(updated.status, "dead_letter")
        self.assertIn("disabled", updated.delivery_error_message or "")
        self.assertEqual(telegram.messages, [])

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
        telegram = ConfiguredTelegram()
        service = self._make_service(telegram=telegram)

        first = service.send_evening_report(student_id, target_date=target_date, force=True)
        second = service.send_evening_report(student_id, target_date=target_date, force=True)

        self.assertIn("End-of-Day Attendance Report", first)
        self.assertIn("End-of-Day Attendance Report", second)
        self.assertEqual(len(telegram.messages), 2)

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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)
        target_date = date(2026, 3, 15)

        first = service.send_morning_update(student_id, target_date=target_date)
        second = service.send_morning_update(student_id, target_date=target_date, force=True)

        self.assertIn("Morning Schedule Update", first)
        self.assertIn("Morning Schedule Update", second)
        self.assertEqual(len(telegram.messages), 2)

    def test_delete_student_cleans_outbound_messages(self) -> None:
        student_id = self._add_student(label="Delete Student", timezone="Asia/Kolkata")
        student = self.db.get_student(student_id)
        assert student is not None
        service = self._make_service()
        service._send_notification(
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        detected_at = datetime(2026, 3, 12, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_due_events(
            student,
            [event],
            detected_at,
            attendance=parse_attendance_summary(erp.get_attendance_summary(student)),
            snapshots=self.db.get_attendance_snapshots(student_id),
        )

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[0][2]
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)
        event = self.db.get_lecture_events_for_day(student_id, target_date)[0]
        detected_at = datetime(2026, 3, 11, 11, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_due_events(
            student,
            [event],
            detected_at,
            attendance=parse_attendance_summary(erp.get_attendance_summary(student)),
            snapshots=self.db.get_attendance_snapshots(student_id),
        )

        self.assertEqual(len(telegram.messages), 1)
        body = telegram.messages[0][2]
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
        telegram = ConfiguredTelegram()
        service = self._make_service(erp=erp, telegram=telegram)
        events = self.db.get_pending_lecture_events()
        detected_at = datetime(2026, 3, 12, 9, 30, tzinfo=ZoneInfo("Asia/Kolkata"))

        service._process_due_events(
            student,
            events,
            detected_at,
            attendance=parse_attendance_summary(erp.get_attendance_summary(student)),
            snapshots=self.db.get_attendance_snapshots(student_id),
        )

        self.assertEqual(len(telegram.messages), 2)
        self.assertTrue(all("Final status: Present" in message[2] for message in telegram.messages))
        self.assertTrue(all("ERP updated 2 pending lecture(s)" in message[2] for message in telegram.messages))
        refreshed = self.db.get_lecture_events_between(student_id, date(2026, 3, 10), date(2026, 3, 11))
        self.assertTrue(all(event.status == "notified_present" for event in refreshed))


if __name__ == "__main__":
    unittest.main()




