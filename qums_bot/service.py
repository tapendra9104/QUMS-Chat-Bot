from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from io import StringIO
import json
import logging
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database, utcnow_iso
from .emailer import EmailSender
from .erp_client import AuthenticationRequired, ERPClient, ERPClientError
from .models import ApplicationRequest, LectureEvent, LectureSlot, PendingLogin, Student, Substitution, TelegramAdminSession
from .parsers import (
    html_to_lines,
    match_attendance_record,
    normalize_subject_key,
    parse_attendance_summary,
    parse_time_range,
    parse_student_detail_response,
    parse_substitutions,
    parse_timetable_slots,
)
from .security import encrypt_text
from .telegram import TelegramError, TelegramSender
from .whatsapp import WhatsAppChannelStatus, WhatsAppError, WhatsAppSender
from .errors import StudentValidationError
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)


class NotificationDeliveryError(Exception):
    pass


STUDENT_ACTION_LABELS: dict[str, str] = {
    "edit": "Edit",
    "start_login": "Start ERP Login",
    "open_captcha": "Open Captcha",
    "preview_today": "Preview Today",
    "send_attendance_summary": "Send Attendance Summary",
    "send_morning": "Send Morning Summary",
    "send_day_report": "Send Day Report",
    "send_shortage_report": "Send Shortage Report",
    "send_channel_test": "Send Channel Test",
}

STUDENT_ACTION_ORDER: tuple[str, ...] = tuple(STUDENT_ACTION_LABELS.keys())

NOTIFICATION_CHANNEL_MODE_LABELS: dict[str, str] = {
    "telegram_only": "Telegram Only",
    "paused": "Paused",
}

ERP_SESSION_MONITOR_COOLDOWN_MINUTES = 15


class BotService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        erp_client: ERPClient,
        whatsapp: WhatsAppSender,
        telegram: TelegramSender,
        emailer: EmailSender,
    ) -> None:
        self.settings = settings
        self.db = db
        self.erp = erp_client
        self.whatsapp = whatsapp
        self.telegram = telegram
        self.emailer = emailer
        self.timezone = ZoneInfo(settings.local_timezone)

    def list_students(self) -> list[Student]:
        return self.db.list_students()

    def list_message_history(self, limit: int | None = 50):
        return self.db.get_recent_message_history(limit)

    def list_admin_audit_log(self, limit: int | None = 50):
        return self.db.get_recent_admin_audit_log(limit)

    def list_application_requests(self, limit: int = 20) -> list[ApplicationRequest]:
        return self.db.list_application_requests(limit)

    def get_application_request(self, application_id: int) -> ApplicationRequest | None:
        return self.db.get_application_request(application_id)

    def get_message_history_page(
        self,
        *,
        query: str = "",
        channel: str = "",
        category: str = "",
        student_id: int | None = None,
        limit: int,
        offset: int,
    ):
        return self.db.get_message_history_page(
            query=query,
            channel=channel,
            category=category,
            student_id=student_id,
            limit=limit,
            offset=offset,
        )

    def count_message_history(
        self,
        *,
        query: str = "",
        channel: str = "",
        category: str = "",
        student_id: int | None = None,
    ) -> int:
        return self.db.count_message_history(query=query, channel=channel, category=category, student_id=student_id)

    def get_message_history_filter_options(self) -> dict[str, list[str]]:
        return self.db.get_message_history_filter_options()

    def get_admin_audit_log_page(
        self,
        *,
        query: str = "",
        action: str = "",
        limit: int,
        offset: int,
    ):
        return self.db.get_admin_audit_log_page(query=query, action=action, limit=limit, offset=offset)

    def count_admin_audit_log(
        self,
        *,
        query: str = "",
        action: str = "",
    ) -> int:
        return self.db.count_admin_audit_log(query=query, action=action)

    def get_admin_audit_filter_options(self) -> dict[str, list[str]]:
        return self.db.get_admin_audit_filter_options()

    def get_outbound_queue_summary(self) -> dict[str, int]:
        return self.db.get_outbound_queue_summary()

    def get_outbound_queue_summary_for_student(self, student_id: int) -> dict[str, int]:
        return self.db.get_outbound_queue_summary_for_student(student_id)

    def get_dead_letter_messages(self, limit: int = 20):
        return self.db.get_dead_letter_messages(limit)

    def get_dead_letter_messages_for_student(self, student_id: int, limit: int = 20):
        return self.db.get_dead_letter_messages_for_student(student_id, limit)

    def retry_dead_letter_message(self, idempotency_key: str) -> str:
        message = self.db.get_outbound_message(idempotency_key)
        if not message:
            raise ERPClientError("Dead-letter message not found.")
        if message.status != "dead_letter":
            raise ERPClientError("This outbound message is not currently in the dead-letter queue.")
        if not self.db.try_claim_dead_letter_outbound_message(idempotency_key):
            raise ERPClientError("This dead-letter message is already being retried or has already changed state.")
        student = self._require_student(message.student_id)
        self._deliver_retry_message(student, message)
        updated = self.db.get_outbound_message(idempotency_key)
        if not updated:
            raise ERPClientError("Dead-letter message state could not be read after retry.")
        if updated.status == "sent":
            return f"Dead-letter message retried successfully for {updated.category}."
        error_text = updated.delivery_error_message or "Retry failed and the message returned to the dead-letter queue."
        raise NotificationDeliveryError(error_text)

    def log_admin_action(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        details: str,
    ) -> None:
        self.db.insert_admin_audit_log(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )

    def get_student_automation_status(
        self,
        student: Student,
        *,
        now: datetime | None = None,
    ) -> dict[str, str | None]:
        student_now = self._now_for_student(student, now or self._local_now())
        today_events = self.db.get_lecture_events_for_day(student.id, student_now.date())
        next_pending = self.db.get_next_pending_lecture_event(student.id)
        latest_recorded = self.db.get_latest_recorded_lecture_event(student.id)
        timetable_context = self._automation_timetable_context(student_now, today_events)

        next_scan = self._estimate_next_attendance_scan(student, student_now)
        next_scan_iso = None
        next_scan_label = None
        if next_scan is not None:
            next_scan_iso = next_scan.isoformat()
            next_scan_label = self._format_datetime(next_scan)

        next_pending_iso = None
        next_pending_label = None
        next_pending_subject = None
        if next_pending and next_pending.check_after:
            next_pending_dt = self._normalize_event_datetime(next_pending.check_after, student_now.tzinfo)
            next_pending_iso = next_pending_dt.isoformat()
            next_pending_label = self._format_datetime(next_pending_dt)
            next_pending_subject = next_pending.subject_name

        latest_recorded_iso = None
        latest_recorded_label = None
        latest_recorded_subject = None
        latest_recorded_status = None
        if latest_recorded and latest_recorded.status_recorded_at:
            recorded_dt = self._normalize_event_datetime(latest_recorded.status_recorded_at, student_now.tzinfo)
            latest_recorded_iso = recorded_dt.isoformat()
            latest_recorded_label = self._format_datetime(recorded_dt)
            latest_recorded_subject = latest_recorded.subject_name
            latest_recorded_status = self._attendance_state_for_event(latest_recorded)[0]

        return {
            "timezone": student.timezone,
            "current_time_iso": student_now.isoformat(),
            "current_time_label": self._format_datetime(student_now),
            "next_attendance_scan_iso": next_scan_iso,
            "next_attendance_scan_label": next_scan_label,
            "attendance_poll_interval_minutes": str(self.settings.attendance_poll_interval_minutes),
            "next_due_attendance_check_iso": next_pending_iso,
            "next_due_attendance_check_label": next_pending_label,
            "next_attendance_subject": next_pending_subject,
            "timetable_lecture_label": (
                str(timetable_context["label"])
                if timetable_context is not None and timetable_context.get("label")
                else None
            ),
            "timetable_lecture_subject": (
                str(timetable_context["subject_name"])
                if timetable_context is not None and timetable_context.get("subject_name")
                else None
            ),
            "timetable_lecture_time_label": (
                str(timetable_context["time_label"])
                if timetable_context is not None and timetable_context.get("time_label")
                else None
            ),
            "latest_marked_at_iso": latest_recorded_iso,
            "latest_marked_at_label": latest_recorded_label,
            "latest_marked_subject": latest_recorded_subject,
            "latest_marked_status": latest_recorded_status,
        }

    def get_student(self, student_id: int) -> Student | None:
        return self.db.get_student(student_id)

    def get_student_by_site_login_username(self, login_username: str) -> Student | None:
        normalized = self._normalize_site_login_username(login_username)
        return self.db.get_student_by_site_login_username(normalized)

    def get_student_disabled_actions(self, student: Student) -> set[str]:
        raw_value = student.disabled_actions_json or "[]"
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed = []
        if not isinstance(parsed, list):
            parsed = []
        return self._normalize_disabled_student_actions(parsed)

    def get_student_notification_channel_mode(self, student: Student) -> str:
        return self._normalize_notification_channel_mode(student.notification_channel_mode)

    def get_student_notification_channel_label(self, student: Student) -> str:
        return NOTIFICATION_CHANNEL_MODE_LABELS[self.get_student_notification_channel_mode(student)]

    def is_student_action_disabled(self, student: Student, action_key: str) -> bool:
        normalized_action = (action_key or "").strip().lower()
        if normalized_action not in STUDENT_ACTION_LABELS:
            return False
        return normalized_action in self.get_student_disabled_actions(student)

    def notifications_paused(self, student: Student) -> bool:
        return self.get_student_notification_channel_mode(student) == "paused"

    def assert_student_action_allowed(self, student: Student, action_key: str) -> None:
        if not student.enabled:
            raise ERPClientError("This student profile is blocked. Unblock it first to use bot features.")
        normalized_action = (action_key or "").strip().lower()
        if normalized_action and self.is_student_action_disabled(student, normalized_action):
            action_label = STUDENT_ACTION_LABELS.get(normalized_action, normalized_action.replace("_", " ").title())
            raise ERPClientError(f"{action_label} is disabled for this student profile.")

    def assert_student_notifications_available(self, student: Student) -> None:
        self.assert_student_action_allowed(student, "")
        if self.notifications_paused(student):
            raise ERPClientError("Notifications are paused for this student profile.")
        if not self._delivery_targets(student):
            raise ERPClientError("Telegram delivery is selected, but no reachable Telegram chat id is configured.")

    def delete_student(self, student_id: int) -> bool:
        return self.db.delete_student(student_id)

    def get_whatsapp_status(self, student: Student):
        return self.whatsapp.get_channel_status(student.whatsapp_number)

    def save_student(
        self,
        *,
        student_id: int | None,
        student_label: str,
        user_name: str,
        password: str,
        site_login_username: str = "",
        site_login_password: str = "",
        whatsapp_number: str,
        telegram_chat_id: str,
        email_address: str,
        enabled: bool,
        timezone: str,
        notification_channel_mode: str | None = None,
        disabled_actions: Iterable[str] | None = None,
    ) -> int:
        current_student = self.db.get_student(student_id) if student_id else None
        cleaned_label = student_label.strip()
        cleaned_user_name = user_name.strip()
        cleaned_timezone = timezone.strip() or self.settings.local_timezone
        normalized_site_login_username = self._normalize_site_login_username(site_login_username)
        canonical_whatsapp = self._canonical_whatsapp_number(whatsapp_number)
        canonical_telegram = self._normalize_telegram_chat_id(telegram_chat_id)
        canonical_email = self._normalize_email_address(email_address)
        resolved_notification_mode = self._normalize_notification_channel_mode(
            notification_channel_mode if notification_channel_mode is not None else (
                current_student.notification_channel_mode if current_student else "telegram_only"
            )
        )
        resolved_disabled_actions = self._normalize_disabled_student_actions(
            disabled_actions
            if disabled_actions is not None
            else (self.get_student_disabled_actions(current_student) if current_student else [])
        )
        site_password_hash = self._resolve_site_password_hash(
            student_id=student_id,
            site_login_username=normalized_site_login_username,
            site_login_password=site_login_password,
        )
        self._validate_student_input(
            student_id=student_id,
            student_label=cleaned_label,
            user_name=cleaned_user_name,
            site_login_username=normalized_site_login_username,
            whatsapp_number=canonical_whatsapp,
            telegram_chat_id=canonical_telegram,
            email_address=canonical_email,
            timezone_name=cleaned_timezone,
        )
        encrypted = encrypt_text(self.settings.app_secret, password) if password else None
        return self.db.upsert_student(
            student_id=student_id,
            student_label=cleaned_label,
            user_name=cleaned_user_name,
            password_encrypted=encrypted,
            site_login_username=normalized_site_login_username,
            site_password_hash=site_password_hash,
            whatsapp_number=canonical_whatsapp,
            telegram_chat_id=canonical_telegram,
            email_address=canonical_email,
            enabled=enabled,
            notification_channel_mode=resolved_notification_mode,
            disabled_actions_json=json.dumps(sorted(resolved_disabled_actions)),
            timezone=cleaned_timezone,
        )

    def update_student_controls(
        self,
        *,
        student_id: int,
        enabled: bool,
        notification_channel_mode: str,
        disabled_actions: Iterable[str],
    ) -> Student:
        student = self._require_student(student_id)
        normalized_mode = self._normalize_notification_channel_mode(notification_channel_mode)
        normalized_disabled_actions = self._normalize_disabled_student_actions(disabled_actions)
        self.db.update_student_controls(
            student_id=student_id,
            enabled=enabled,
            notification_channel_mode=normalized_mode,
            disabled_actions_json=json.dumps(sorted(normalized_disabled_actions)),
        )
        updated_student = self._require_student(student_id)
        blocked_state = "blocked" if not enabled else "active"
        disabled_summary = ", ".join(
            STUDENT_ACTION_LABELS[action_key]
            for action_key in STUDENT_ACTION_ORDER
            if action_key in normalized_disabled_actions
        ) or "none"
        self.db.update_student_bot_activity(
            student_id,
            "Admin updated student controls: "
            f"profile {blocked_state}, delivery {NOTIFICATION_CHANNEL_MODE_LABELS[normalized_mode]}, "
            f"disabled actions {disabled_summary}.",
        )
        return updated_student

    def update_student_site_password(self, *, student_id: int, new_password: str) -> None:
        password_hash = generate_password_hash(new_password)
        self.db.update_student_site_credentials(student_id=student_id, site_password_hash=password_hash)

    def submit_application_request(
        self,
        *,
        applicant_name: str,
        student_label: str,
        user_name: str,
        password: str,
        whatsapp_number: str,
        telegram_chat_id: str,
        timezone: str,
        reg_id: str,
        note: str,
        created_from_ip: str | None,
    ) -> dict[str, object]:
        cleaned_applicant_name = " ".join((applicant_name or "").strip().split())
        cleaned_label = " ".join((student_label or "").strip().split())
        cleaned_user_name = (user_name or "").strip()
        cleaned_password = password or ""
        cleaned_timezone = (timezone or "").strip() or self.settings.local_timezone
        cleaned_reg_id = " ".join((reg_id or "").strip().split()) or None
        cleaned_note = (note or "").strip() or None
        canonical_whatsapp = self._canonical_whatsapp_number(whatsapp_number)
        canonical_telegram = self._normalize_telegram_chat_id(telegram_chat_id)

        if len(cleaned_applicant_name) < 3:
            raise StudentValidationError("Applicant name is required.")
        if not cleaned_label:
            raise StudentValidationError("Preferred student label is required.")
        if not cleaned_user_name:
            raise StudentValidationError("ERP user id is required.")
        if len(cleaned_password) < 4:
            raise StudentValidationError("ERP password is required.")
        if cleaned_note and len(cleaned_note) > 1500:
            raise StudentValidationError("Application note must be 1500 characters or fewer.")
        try:
            ZoneInfo(cleaned_timezone)
        except Exception as exc:
            raise StudentValidationError(f"Invalid timezone: {cleaned_timezone}") from exc

        request_id = self.db.insert_application_request(
            applicant_name=cleaned_applicant_name,
            student_label=cleaned_label,
            user_name=cleaned_user_name,
            password_encrypted=encrypt_text(self.settings.app_secret, cleaned_password),
            reg_id=cleaned_reg_id,
            whatsapp_number=canonical_whatsapp,
            telegram_chat_id=canonical_telegram,
            timezone=cleaned_timezone,
            note=cleaned_note,
            created_from_ip=(created_from_ip or "").strip() or None,
        )

        notification_sent = False
        notification_error = None
        if self.telegram.configured and self.settings.telegram_admin_chat_ids:
            message = self._build_application_request_notification(
                request_id=request_id,
                applicant_name=cleaned_applicant_name,
                student_label=cleaned_label,
                user_name=cleaned_user_name,
                reg_id=cleaned_reg_id,
                telegram_chat_id=canonical_telegram,
                timezone=cleaned_timezone,
                note=cleaned_note,
            )
            try:
                for chat_id in self.settings.telegram_admin_chat_ids:
                    self.telegram.send_text(chat_id, message, message_kind="application_request")
                notification_sent = True
            except TelegramError as exc:
                notification_error = str(exc)

        return {
            "id": request_id,
            "notification_sent": notification_sent,
            "notification_error": notification_error,
        }

    def start_login(self, student_id: int) -> PendingLogin:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "start_login")
        pending = self.erp.start_manual_login(student)
        self.db.save_pending_login(pending)
        self.db.update_student_erp_status(student.id, "Waiting for manual captcha entry.")
        return pending

    def refresh_login(self, student_id: int) -> PendingLogin:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "open_captcha")
        pending = self.db.get_pending_login(student_id)
        if not pending:
            raise ERPClientError("No pending login session. Start login first.")
        refreshed = self.erp.refresh_captcha(pending)
        self.db.save_pending_login(refreshed)
        return refreshed

    def complete_login(self, student_id: int, captcha: str) -> str:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "open_captcha")
        pending = self.db.get_pending_login(student_id)
        if not pending:
            raise ERPClientError("No pending login session. Start login first.")
        result = self.erp.complete_manual_login(student, pending, captcha)
        self.db.update_student_session(
            student_id=student.id,
            cookies_json=result.cookies_json,
            last_login_status="ERP session active.",
            reg_id=result.reg_id,
            student_name=result.student_name,
        )
        self.db.clear_pending_login(student.id)
        return "Login successful. The bot can now sync the ERP."

    def preview_today(self, student_id: int, target_date: date | None = None) -> str:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "preview_today")
        target = target_date or self._now_for_student(student).date()
        summary = self._collect_daily_summary(student, target, send_risk_alerts=False)
        return summary["message"]

    def send_morning_update(
        self,
        student_id: int,
        target_date: date | None = None,
        *,
        force: bool = False,
    ) -> str:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "send_morning")
        current = self._now_for_student(student)
        target = target_date or current.date()
        summary = self._collect_daily_summary(student, target, send_risk_alerts=True)
        self._send_whatsapp(
            student,
            summary["message"],
            message_kind="morning",
            history_category="morning_summary",
            idempotency_key=self._report_idempotency_key(
                base_key=f"morning_summary:{student.id}:{target.isoformat()}",
                force=force,
                now=current,
            ),
        )
        self.db.mark_student_erp_sync(student.id)
        self.db.upsert_notification_event(
            student_id=student.id,
            category="morning_digest",
            notification_key=target.isoformat(),
            message_text=summary["message"],
        )
        self.db.update_student_status(student.id, f"Morning summary sent for {target.isoformat()}.")
        return summary["message"]

    def send_evening_report(
        self,
        student_id: int,
        target_date: date | None = None,
        *,
        force: bool = False,
    ) -> str:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "send_day_report")
        now = self._now_for_student(student)
        target = target_date or now.date()
        message = self._send_evening_report_if_due(student, target, now=now, force=force)
        if not message:
            raise ERPClientError(
                "The end-of-day attendance report is not ready yet. "
                "It is sent after the final lecture check window closes."
            )
        return message

    def send_shortage_report(
        self,
        student_id: int,
        target_date: date | None = None,
        *,
        force: bool = False,
    ) -> str:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "send_shortage_report")
        current = self._now_for_student(student)
        report_date = target_date or current.date()
        self._collect_daily_summary(student, report_date, send_risk_alerts=False)
        attendance = parse_attendance_summary(self.erp.get_attendance_summary(student))
        teacher_events = self._risk_teacher_reference_events(student, target_date=report_date)
        subject_references = self._build_weekly_subject_reference_map(student, report_date)
        message = self._build_shortage_report(
            student_name=student.student_name or student.student_label,
            attendance=attendance,
            lecture_events=teacher_events,
            subject_references=subject_references,
            generated_at=current,
        )
        self._send_whatsapp(
            student,
            message,
            message_kind="attendance",
            history_category="attendance_shortage_report",
            idempotency_key=self._report_idempotency_key(
                base_key=f"attendance_shortage_report:{student.id}:{report_date.isoformat()}",
                force=force,
                now=current,
            ),
        )
        self.db.mark_student_erp_sync(student.id)
        self.db.update_student_status(student.id, f"Attendance shortage report sent for {report_date.isoformat()}.")
        return message

    def send_attendance_summary_report(
        self,
        student_id: int,
        target_date: date | None = None,
        *,
        force: bool = False,
    ) -> str:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "send_attendance_summary")
        current = self._now_for_student(student)
        report_date = target_date or current.date()
        attendance = parse_attendance_summary(self.erp.get_attendance_summary(student))
        teacher_events = self._risk_teacher_reference_events(student, target_date=report_date)
        subject_references = self._build_weekly_subject_reference_map(student, report_date)
        message = self._build_attendance_summary_report(
            student_name=student.student_name or student.student_label,
            attendance=attendance,
            lecture_events=teacher_events,
            subject_references=subject_references,
            generated_at=current,
        )
        self._send_whatsapp(
            student,
            message,
            message_kind="attendance",
            history_category="attendance_summary_report",
            idempotency_key=self._report_idempotency_key(
                base_key=f"attendance_summary_report:{student.id}:{report_date.isoformat()}",
                force=force,
                now=current,
            ),
        )
        self.db.mark_student_erp_sync(student.id)
        self.db.update_student_status(student.id, f"Attendance summary report sent for {report_date.isoformat()}.")
        return message

    def send_test_message(self, student_id: int) -> str:
        student = self._require_student(student_id)
        self.assert_student_action_allowed(student, "send_channel_test")
        body = (
            "QUMS bot test message.\n"
            "If you received this, the configured delivery channels are working."
        )
        self._send_whatsapp(student, body, message_kind="generic", history_category="test_message")
        return body

    def run_telegram_inbound_sweep(self) -> None:
        if not self.telegram.configured:
            return
        self._ensure_telegram_bot_commands_registered()
        offset_value = self.db.get_runtime_state("telegram_update_offset")
        offset = None
        if offset_value:
            try:
                offset = int(offset_value)
            except ValueError:
                offset = None
        try:
            updates = self.telegram.get_updates(
                offset=offset,
                timeout_seconds=0,
                allowed_updates=["message", "callback_query"],
            )
        except TelegramError as exc:
            logger.warning("Telegram getUpdates failed: %s", exc)
            return
        for update in updates:
            update_id = int(update.get("update_id", 0))
            try:
                self._handle_telegram_update(update)
            except Exception:
                logger.exception("Telegram update handling failed for update_id=%s", update_id or "unknown")
            finally:
                if update_id:
                    self.db.upsert_runtime_state(
                        state_key="telegram_update_offset",
                        state_value=str(update_id + 1),
                    )

    def run_telegram_admin_refresh_sweep(self, *, now: datetime | None = None) -> None:
        if not self.telegram.configured or not self.settings.telegram_admin_chat_ids:
            return
        if self.settings.dashboard_auto_refresh_seconds <= 0:
            return
        current = now or self._local_now()
        for chat in self.db.list_telegram_admin_chats(auto_refresh_enabled=True):
            if not self._is_telegram_admin_chat(chat.chat_id):
                continue
            try:
                self._push_telegram_dashboard_message(chat.chat_id, now=current, live_mode=True)
            except TelegramError:
                continue

    def _handle_telegram_update(self, update: dict) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            self._handle_telegram_callback(callback_query)
            return
        message = update.get("message")
        if isinstance(message, dict):
            self._handle_telegram_message(message)

    def _handle_telegram_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        text = str(message.get("text") or "").strip()
        if not chat_id or not text:
            return
        session = self.db.get_telegram_admin_session(chat_id)
        if not self._is_telegram_admin_chat(chat_id):
            student = self._find_student_by_telegram_chat_id(chat_id)
            if student is not None:
                self._handle_student_telegram_message(chat_id, text, session, student)
                return
            self._handle_public_telegram_message(chat_id, text, session)
            return
        self._ensure_telegram_admin_chat(chat_id)
        normalized_text = " ".join(text.split())
        if normalized_text.startswith("/"):
            try:
                if self._handle_telegram_command(chat_id, normalized_text, session=session):
                    return
            except AuthenticationRequired:
                try:
                    self._send_telegram_text(
                        chat_id,
                        "ERP session expired. Open the captcha page again and complete login from the website.",
                    )
                except TelegramError:
                    pass
                return
            except (ERPClientError, NotificationDeliveryError, TelegramError, ValueError) as exc:
                try:
                    self._send_telegram_text(chat_id, f"Telegram admin command failed: {exc}")
                except TelegramError:
                    pass
                return
        if session and not normalized_text.startswith("/"):
            self._handle_telegram_session_input(chat_id, text, session)
            return

        command = normalized_text.lower()
        if command in {"/start", "/menu", "menu"}:
            self._push_telegram_dashboard_message(chat_id, live_mode=False)
            return
        if command in {"/help", "help"}:
            self._send_telegram_text(
                chat_id,
                self._render_telegram_admin_help(chat_id),
                reply_markup=self._telegram_root_markup(chat_id),
            )
            return
        if command in {"/dashboard", "dashboard"}:
            self._push_telegram_dashboard_message(chat_id, live_mode=False)
            return
        if command in {"/students", "students"}:
            self._send_telegram_text(
                chat_id,
                self._build_telegram_students_text(),
                reply_markup=self._telegram_students_markup(),
            )
            return
        if command in {"/runchecks", "/run_checks", "run checks", "run live checks", "/exports", "exports"}:
            self._send_telegram_text(
                chat_id,
                "This Telegram admin control has been removed. Use /menu for the dashboard or /students for student actions.",
            )
            return
        self._send_telegram_text(
            chat_id,
            "Unknown Telegram admin command. Send /menu to open the control panel.",
            reply_markup=self._telegram_root_markup(chat_id),
        )

    def _handle_student_telegram_message(
        self,
        chat_id: str,
        text: str,
        session: TelegramAdminSession | None,
        student: Student,
    ) -> None:
        if not student.enabled:
            self._send_telegram_text(
                chat_id,
                "This student profile is currently blocked. Contact the admin to restore access.",
            )
            return
        normalized_text = " ".join(text.split())
        if normalized_text.startswith("/"):
            try:
                if self._handle_student_telegram_command(chat_id, normalized_text, session=session, student=student):
                    return
            except AuthenticationRequired:
                try:
                    self._send_telegram_text(
                        chat_id,
                        "ERP session expired. Open the captcha page again and complete login from the website.",
                    )
                except TelegramError:
                    pass
                return
            except (ERPClientError, NotificationDeliveryError, TelegramError, ValueError) as exc:
                try:
                    self._send_telegram_text(chat_id, f"Telegram student command failed: {exc}")
                except TelegramError:
                    pass
                return

        command = normalized_text.lower()
        if command in {"/start", "/menu", "menu", "/dashboard", "dashboard", "/students", "students", "/student", "student"}:
            self._send_telegram_text(
                chat_id,
                self._build_telegram_student_menu_text(student),
                reply_markup=self._telegram_self_actions_markup(),
            )
            return
        if command in {"/help", "help"}:
            self._send_telegram_text(
                chat_id,
                self._render_student_telegram_help(student),
                reply_markup=self._telegram_self_actions_markup(),
            )
            return
        self._send_telegram_text(
            chat_id,
            "This Telegram chat is linked only to your own student profile. Send /menu to open your panel.",
            reply_markup=self._telegram_self_actions_markup(),
        )

    def _handle_student_telegram_command(
        self,
        chat_id: str,
        text: str,
        *,
        session: TelegramAdminSession | None,
        student: Student,
    ) -> bool:
        parts = text.split()
        if not parts:
            return False
        command = parts[0].split("@", 1)[0].lower()

        if command in {"/start", "/menu", "/dashboard", "/students", "/student"}:
            self._send_telegram_text(
                chat_id,
                self._build_telegram_student_menu_text(student),
                reply_markup=self._telegram_self_actions_markup(),
            )
            return True
        if command == "/help":
            self._send_telegram_text(
                chat_id,
                self._render_student_telegram_help(student),
                reply_markup=self._telegram_self_actions_markup(),
            )
            return True
        if command == "/cancel":
            if session:
                self.db.clear_telegram_admin_session(chat_id)
                self._send_telegram_text(
                    chat_id,
                    "No active student form is available in Telegram right now.",
                    reply_markup=self._telegram_self_actions_markup(),
                )
            else:
                self._send_telegram_text(chat_id, "No Telegram form is active right now.")
            return True
        if command == "/preview":
            self._send_telegram_text(chat_id, self.preview_today(student.id))
            return True
        if command == "/attendance":
            if not self._claim_telegram_action_window(chat_id, "send_attendance_summary_report", f"student:{student.id}"):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_attendance_summary_report(student.id, force=True)
            self._send_telegram_result_if_needed(chat_id, student.id, body)
            return True
        if command in {"/sendmorning", "/morning"}:
            if not self._claim_telegram_action_window(chat_id, "send_morning_update", f"student:{student.id}"):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_morning_update(student.id, force=True)
            self._send_telegram_result_if_needed(chat_id, student.id, body)
            return True
        if command in {"/senddayreport", "/dayreport", "/evening"}:
            if not self._claim_telegram_action_window(chat_id, "send_evening_report", f"student:{student.id}"):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_evening_report(student.id, force=True)
            self._send_telegram_result_if_needed(chat_id, student.id, body)
            return True
        if command in {"/sendshortagereport", "/shortage"}:
            if not self._claim_telegram_action_window(chat_id, "send_shortage_report", f"student:{student.id}"):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_shortage_report(student.id, force=True)
            self._send_telegram_result_if_needed(chat_id, student.id, body)
            return True
        if command in {"/sendtest", "/test"}:
            if not self._claim_telegram_action_window(chat_id, "send_test_message", f"student:{student.id}"):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_test_message(student.id)
            self._send_telegram_result_if_needed(chat_id, student.id, body)
            return True
        if command in {"/startlogin", "/login"}:
            if not self._claim_telegram_action_window(chat_id, "start_login", f"student:{student.id}"):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            pending = self.db.get_pending_login(student.id)
            if pending:
                self.refresh_login(student.id)
            else:
                self.start_login(student.id)
            self._send_telegram_text(chat_id, self._build_telegram_login_message(student.id))
            return True
        if command in {
            "/addstudent",
            "/editstudent",
            "/applications",
            "/application",
            "/deleteprofile",
            "/delete",
        }:
            self._send_telegram_text(
                chat_id,
                "This Telegram chat can access only your own student profile. Admin-only student management is not available here.",
                reply_markup=self._telegram_self_actions_markup(),
            )
            return True
        return False

    def _handle_public_telegram_message(
        self,
        chat_id: str,
        text: str,
        session: TelegramAdminSession | None,
    ) -> None:
        normalized_text = " ".join(text.split())
        if normalized_text.startswith("/"):
            try:
                if self._handle_public_telegram_command(chat_id, normalized_text, session=session):
                    return
            except (StudentValidationError, TelegramError, ValueError) as exc:
                try:
                    self._send_telegram_text(chat_id, f"Application request failed: {exc}")
                except TelegramError:
                    pass
                return
        if session and session.mode == "application_request" and not normalized_text.startswith("/"):
            self._handle_public_telegram_session_input(chat_id, text, session)
            return

        command = normalized_text.lower()
        if command in {"/start", "/menu", "/help", "start", "menu", "help"}:
            self._send_telegram_text(
                chat_id,
                self._render_public_telegram_help(),
                reply_markup=self._telegram_public_markup(),
            )
            return
        if command in {"/apply", "apply"}:
            self._start_public_application_session(chat_id)
            return
        self._send_telegram_text(
            chat_id,
            "Send /apply to submit a new student application, or /start to see the available options.",
            reply_markup=self._telegram_public_markup(),
        )

    def _handle_public_telegram_command(
        self,
        chat_id: str,
        text: str,
        *,
        session: TelegramAdminSession | None,
    ) -> bool:
        parts = text.split()
        if not parts:
            return False
        command = parts[0].split("@", 1)[0].lower()
        if command in {"/start", "/menu", "/help"}:
            self._send_telegram_text(
                chat_id,
                self._render_public_telegram_help(),
                reply_markup=self._telegram_public_markup(),
            )
            return True
        if command == "/apply":
            self._start_public_application_session(chat_id)
            return True
        if command == "/cancel":
            if session and session.mode == "application_request":
                self.db.clear_telegram_admin_session(chat_id)
                self._send_telegram_text(
                    chat_id,
                    "Application request cancelled.",
                    reply_markup=self._telegram_public_markup(),
                )
            else:
                self._send_telegram_text(chat_id, "No application request is active right now.")
            return True
        return False

    def _handle_telegram_command(
        self,
        chat_id: str,
        text: str,
        *,
        session: TelegramAdminSession | None,
    ) -> bool:
        parts = text.split()
        if not parts:
            return False
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        if command in {"/start", "/menu"}:
            self._push_telegram_dashboard_message(chat_id, live_mode=False)
            return True
        if command == "/help":
            self._send_telegram_text(
                chat_id,
                self._render_telegram_admin_help(chat_id),
                reply_markup=self._telegram_root_markup(chat_id),
            )
            return True
        if command == "/cancel":
            if session:
                self.db.clear_telegram_admin_session(chat_id)
                self._send_telegram_text(
                    chat_id,
                    "Telegram student form cancelled.",
                    reply_markup=self._telegram_root_markup(chat_id),
                )
            else:
                self._send_telegram_text(chat_id, "No Telegram form is active right now.")
            return True
        if command == "/dashboard":
            self._push_telegram_dashboard_message(chat_id, live_mode=False)
            return True
        if command == "/students":
            self._send_telegram_text(
                chat_id,
                self._build_telegram_students_text(),
                reply_markup=self._telegram_students_markup(),
            )
            return True
        if command == "/applications":
            self._send_telegram_text(chat_id, self._build_telegram_applications_text())
            return True
        if command == "/application":
            application_id = self._parse_telegram_student_id_arg(args, command_name="/application")
            application = self.get_application_request(application_id)
            if not application:
                raise ValueError("Application request not found.")
            self._send_telegram_text(chat_id, self._build_telegram_application_detail_text(application))
            return True
        if command in {
            "/runchecks",
            "/run_checks",
            "/runlivechecks",
            "/exports",
            "/exportmessages",
            "/exportaudit",
            "/autorefresh",
        }:
            self._send_telegram_text(
                chat_id,
                "This Telegram admin control has been removed. Use /menu for the dashboard or /students for student actions.",
            )
            return True
        if command == "/addstudent":
            self._start_telegram_student_session(chat_id, mode="student_add")
            return True
        if command == "/editstudent":
            student_id = self._parse_telegram_student_id_arg(args, command_name="/editstudent")
            self._start_telegram_student_session(chat_id, mode="student_edit", student_id=student_id)
            return True
        if command == "/student":
            student_id = self._parse_telegram_student_id_arg(args, command_name="/student")
            student = self._require_student(student_id)
            self._send_telegram_text(
                chat_id,
                self._build_telegram_student_menu_text(student),
                reply_markup=self._telegram_student_actions_markup(student.id),
            )
            return True
        if command == "/preview":
            student_id = self._parse_telegram_student_id_arg(args, command_name="/preview")
            self._send_telegram_text(chat_id, self.preview_today(student_id))
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="preview_today",
                target_type="student",
                target_id=str(student_id),
                details="Morning preview generated from Telegram command.",
            )
            return True
        if command == "/attendance":
            student_id = self._parse_telegram_student_id_arg(args, command_name="/attendance")
            if not self._claim_telegram_action_window(chat_id, "send_attendance_summary_report", str(student_id)):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_attendance_summary_report(student_id, force=True)
            self._send_telegram_result_if_needed(chat_id, student_id, body)
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="send_attendance_summary_report",
                target_type="student",
                target_id=str(student_id),
                details="Attendance summary report sent from Telegram command.",
            )
            return True
        if command in {"/sendmorning", "/morning"}:
            student_id = self._parse_telegram_student_id_arg(args, command_name=command)
            if not self._claim_telegram_action_window(chat_id, "send_morning_update", str(student_id)):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_morning_update(student_id, force=True)
            self._send_telegram_result_if_needed(chat_id, student_id, body)
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="send_morning_update",
                target_type="student",
                target_id=str(student_id),
                details="Morning summary sent from Telegram command.",
            )
            return True
        if command in {"/senddayreport", "/dayreport", "/evening"}:
            student_id = self._parse_telegram_student_id_arg(args, command_name=command)
            if not self._claim_telegram_action_window(chat_id, "send_evening_report", str(student_id)):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_evening_report(student_id, force=True)
            self._send_telegram_result_if_needed(chat_id, student_id, body)
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="send_evening_report",
                target_type="student",
                target_id=str(student_id),
                details="End-of-day report sent from Telegram command.",
            )
            return True
        if command in {"/sendshortagereport", "/shortage"}:
            student_id = self._parse_telegram_student_id_arg(args, command_name=command)
            if not self._claim_telegram_action_window(chat_id, "send_shortage_report", str(student_id)):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_shortage_report(student_id, force=True)
            self._send_telegram_result_if_needed(chat_id, student_id, body)
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="send_shortage_report",
                target_type="student",
                target_id=str(student_id),
                details="Shortage report sent from Telegram command.",
            )
            return True
        if command in {"/sendtest", "/test"}:
            student_id = self._parse_telegram_student_id_arg(args, command_name=command)
            if not self._claim_telegram_action_window(chat_id, "send_test_message", str(student_id)):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            body = self.send_test_message(student_id)
            self._send_telegram_result_if_needed(chat_id, student_id, body)
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="send_test_message",
                target_type="student",
                target_id=str(student_id),
                details="Channel test sent from Telegram command.",
            )
            return True
        if command in {"/startlogin", "/login"}:
            student_id = self._parse_telegram_student_id_arg(args, command_name=command)
            if not self._claim_telegram_action_window(chat_id, "start_login", str(student_id)):
                self._send_telegram_text(chat_id, "This action was already sent recently. Wait a few seconds before retrying.")
                return True
            pending = self.db.get_pending_login(student_id)
            if pending:
                self.refresh_login(student_id)
            else:
                self.start_login(student_id)
            self._send_telegram_text(chat_id, self._build_telegram_login_message(student_id))
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="start_login",
                target_type="student",
                target_id=str(student_id),
                details="ERP login started or refreshed from Telegram command.",
            )
            return True
        if command in {"/deleteprofile", "/delete"}:
            student_id = self._parse_telegram_student_id_arg(args, command_name=command)
            student = self._require_student(student_id)
            self._send_telegram_text(
                chat_id,
                f"Delete profile for {student.student_label}?\nThis cannot be undone.",
                reply_markup=self._telegram_inline_markup(
                    [[
                        {"text": "Delete Profile", "callback_data": f"tg:student:{student.id}:delete"},
                        {"text": "Cancel", "callback_data": f"tg:student:{student.id}:menu"},
                    ]]
                ),
            )
            return True
        return False

    def _parse_telegram_student_id_arg(self, args: list[str], *, command_name: str) -> int:
        if not args:
            raise ValueError(f"{command_name} requires a student id.")
        try:
            return int(str(args[0]).strip())
        except ValueError as exc:
            raise ValueError(f"{command_name} requires a numeric student id.") from exc

    def _handle_telegram_callback(self, callback_query: dict) -> None:
        callback_id = str(callback_query.get("id") or "").strip()
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        data = str(callback_query.get("data") or "").strip()
        if not chat_id or not data:
            return
        if not self._is_telegram_admin_chat(chat_id):
            student = self._find_student_by_telegram_chat_id(chat_id)
            if student is not None:
                if data.startswith("tgs:"):
                    self._handle_student_telegram_callback(chat_id, callback_id, data, student)
                    return
                self._answer_telegram_callback(
                    callback_id,
                    "This Telegram chat can access only its own student profile.",
                    show_alert=True,
                )
                return
            if data.startswith("tgpub:"):
                self._handle_public_telegram_callback(chat_id, callback_id, data)
                return
            self._answer_telegram_callback(
                callback_id,
                "This Telegram chat is not authorized.",
                show_alert=True,
            )
            return
        self._ensure_telegram_admin_chat(chat_id)
        try:
            if data == "tg:dashboard":
                self._push_telegram_dashboard_message(chat_id, live_mode=False)
                self._answer_telegram_callback(callback_id, "Dashboard updated.")
                return
            if data in {"tg:runchecks", "tg:export:messages", "tg:export:audit", "tg:autorefresh:toggle"}:
                self._answer_telegram_callback(
                    callback_id,
                    "This Telegram control has been removed.",
                    show_alert=True,
                )
                return
            if data == "tg:students":
                self._send_telegram_text(
                    chat_id,
                    self._build_telegram_students_text(),
                    reply_markup=self._telegram_students_markup(),
                )
                self._answer_telegram_callback(callback_id)
                return
            if data == "tg:student:add":
                self._start_telegram_student_session(chat_id, mode="student_add")
                self._answer_telegram_callback(callback_id, "Send the student details.")
                return
            if data.startswith("tg:student:") and data.endswith(":menu"):
                student_id = self._extract_telegram_student_id(data, suffix=":menu")
                student = self.get_student(student_id)
                if not student:
                    self._answer_telegram_callback(callback_id, "Student profile not found.", show_alert=True)
                    return
                self._send_telegram_text(
                    chat_id,
                    self._build_telegram_student_menu_text(student),
                    reply_markup=self._telegram_student_actions_markup(student.id),
                )
                self._answer_telegram_callback(callback_id)
                return
            if data.startswith("tg:student:") and data.endswith(":edit"):
                student_id = self._extract_telegram_student_id(data, suffix=":edit")
                self._start_telegram_student_session(chat_id, mode="student_edit", student_id=student_id)
                self._answer_telegram_callback(callback_id, "Edit flow started.")
                return
            if data.startswith("tg:student:") and data.endswith(":deleteconfirm"):
                student_id = self._extract_telegram_student_id(data, suffix=":deleteconfirm")
                student = self.get_student(student_id)
                if not student:
                    self._answer_telegram_callback(callback_id, "Student profile not found.", show_alert=True)
                    return
                self._send_telegram_text(
                    chat_id,
                    f"Delete profile for {student.student_label}?\nThis cannot be undone.",
                    reply_markup=self._telegram_inline_markup(
                        [
                            [
                                {"text": "Delete Profile", "callback_data": f"tg:student:{student.id}:delete"},
                                {"text": "Cancel", "callback_data": f"tg:student:{student.id}:menu"},
                            ]
                        ]
                    ),
                )
                self._answer_telegram_callback(callback_id)
                return
            if data.startswith("tg:student:") and data.endswith(":delete"):
                student_id = self._extract_telegram_student_id(data, suffix=":delete")
                student = self.get_student(student_id)
                if not student or not self.delete_student(student_id):
                    self._answer_telegram_callback(callback_id, "Student profile not found.", show_alert=True)
                    return
                self._log_admin_action_safe(
                    actor=f"telegram:{chat_id}",
                    action="delete_student",
                    target_type="student",
                    target_id=str(student_id),
                    details="Student profile deleted from Telegram.",
                )
                self._send_telegram_text(chat_id, f"Student profile deleted: {student.student_label}")
                self._answer_telegram_callback(callback_id, "Profile deleted.")
                return
            if data.startswith("tg:student:"):
                self._handle_telegram_student_action(chat_id, callback_id, data)
                return
            if data == "tg:session:save":
                self._finalize_telegram_student_session(chat_id)
                self._answer_telegram_callback(callback_id, "Student profile saved.")
                return
            if data == "tg:session:cancel":
                self.db.clear_telegram_admin_session(chat_id)
                self._send_telegram_text(
                    chat_id,
                    "Telegram student form cancelled.",
                    reply_markup=self._telegram_root_markup(chat_id),
                )
                self._answer_telegram_callback(callback_id, "Cancelled.")
                return
        except AuthenticationRequired:
            self._send_telegram_text(
                chat_id,
                "ERP session expired. Open the captcha page again and complete login from the website.",
            )
            self._answer_telegram_callback(callback_id, "ERP login required.", show_alert=True)
            return
        except (ERPClientError, NotificationDeliveryError, TelegramError, ValueError) as exc:
            self._send_telegram_text(chat_id, f"Telegram admin action failed: {exc}")
            self._answer_telegram_callback(callback_id, "Action failed.", show_alert=True)
            return

        self._answer_telegram_callback(callback_id)

    def _handle_student_telegram_callback(
        self,
        chat_id: str,
        callback_id: str,
        data: str,
        student: Student,
    ) -> None:
        try:
            if data == "tgs:menu":
                self._send_telegram_text(
                    chat_id,
                    self._build_telegram_student_menu_text(student),
                    reply_markup=self._telegram_self_actions_markup(),
                )
                self._answer_telegram_callback(callback_id)
                return
            if data == "tgs:help":
                self._send_telegram_text(
                    chat_id,
                    self._render_student_telegram_help(student),
                    reply_markup=self._telegram_self_actions_markup(),
                )
                self._answer_telegram_callback(callback_id)
                return

            action_map = {
                "tgs:preview": ("preview_today", "Preview sent.", lambda: self.preview_today(student.id), True),
                "tgs:attendance": ("send_attendance_summary_report", "Attendance summary sent.", lambda: self.send_attendance_summary_report(student.id, force=True), False),
                "tgs:morning": ("send_morning_update", "Morning summary sent.", lambda: self.send_morning_update(student.id, force=True), False),
                "tgs:evening": ("send_evening_report", "Day report sent.", lambda: self.send_evening_report(student.id, force=True), False),
                "tgs:shortage": ("send_shortage_report", "Shortage report sent.", lambda: self.send_shortage_report(student.id, force=True), False),
                "tgs:test": ("send_test_message", "Channel test sent.", lambda: self.send_test_message(student.id), False),
            }
            for callback_key, (action_name, callback_text, handler, always_echo) in action_map.items():
                if data != callback_key:
                    continue
                if not self._claim_telegram_action_window(chat_id, action_name, f"student:{student.id}"):
                    self._answer_telegram_callback(
                        callback_id,
                        "This action was already sent recently.",
                        show_alert=True,
                    )
                    return
                self._answer_telegram_callback(callback_id, "Processing request.")
                body = handler()
                if always_echo:
                    self._send_telegram_text(chat_id, body)
                else:
                    self._send_telegram_result_if_needed(chat_id, student.id, body)
                return

            if data == "tgs:login":
                if not self._claim_telegram_action_window(chat_id, "start_login", f"student:{student.id}"):
                    self._answer_telegram_callback(
                        callback_id,
                        "This action was already sent recently.",
                        show_alert=True,
                    )
                    return
                self._answer_telegram_callback(callback_id, "Preparing login handoff.")
                pending = self.db.get_pending_login(student.id)
                if pending:
                    self.refresh_login(student.id)
                else:
                    self.start_login(student.id)
                self._send_telegram_text(chat_id, self._build_telegram_login_message(student.id))
                return
        except AuthenticationRequired:
            self._send_telegram_text(
                chat_id,
                "ERP session expired. Open the captcha page again and complete login from the website.",
            )
            self._answer_telegram_callback(callback_id, "ERP login required.", show_alert=True)
            return
        except (ERPClientError, NotificationDeliveryError, TelegramError, ValueError) as exc:
            self._send_telegram_text(chat_id, f"Telegram student action failed: {exc}")
            self._answer_telegram_callback(callback_id, "Action failed.", show_alert=True)
            return

        self._answer_telegram_callback(callback_id, "Unknown action.", show_alert=True)

    def _handle_telegram_student_action(self, chat_id: str, callback_id: str, data: str) -> None:
        action_map = {
            ":preview": (
                "preview_today",
                "Preview sent.",
                lambda student_id: self.preview_today(student_id),
                "Morning preview generated from Telegram.",
            ),
            ":attendance": (
                "send_attendance_summary_report",
                "Attendance summary sent.",
                lambda student_id: self.send_attendance_summary_report(student_id, force=True),
                "Attendance summary report sent from Telegram.",
            ),
            ":morning": (
                "send_morning_update",
                "Morning summary sent.",
                lambda student_id: self.send_morning_update(student_id, force=True),
                "Morning summary sent from Telegram.",
            ),
            ":evening": (
                "send_evening_report",
                "Day report sent.",
                lambda student_id: self.send_evening_report(student_id, force=True),
                "End-of-day report sent from Telegram.",
            ),
            ":shortage": (
                "send_shortage_report",
                "Shortage report sent.",
                lambda student_id: self.send_shortage_report(student_id, force=True),
                "Shortage report sent from Telegram.",
            ),
            ":test": (
                "send_test_message",
                "Channel test sent.",
                lambda student_id: self.send_test_message(student_id),
                "Channel test sent from Telegram.",
            ),
        }
        for suffix, (action_name, callback_text, handler, log_details) in action_map.items():
            if not data.endswith(suffix):
                continue
            student_id = self._extract_telegram_student_id(data, suffix=suffix)
            if not self._claim_telegram_action_window(chat_id, action_name, str(student_id)):
                self._answer_telegram_callback(
                    callback_id,
                    "This action was already sent recently.",
                    show_alert=True,
                )
                return
            self._answer_telegram_callback(callback_id, "Processing request.")
            body = handler(student_id)
            self._send_telegram_result_if_needed(chat_id, student_id, body)
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action=action_name,
                target_type="student",
                target_id=str(student_id),
                details=log_details,
            )
            return
        if data.endswith(":login"):
            student_id = self._extract_telegram_student_id(data, suffix=":login")
            if not self._claim_telegram_action_window(chat_id, "start_login", str(student_id)):
                self._answer_telegram_callback(
                    callback_id,
                    "This action was already sent recently.",
                    show_alert=True,
                )
                return
            self._answer_telegram_callback(callback_id, "Preparing login handoff.")
            pending = self.db.get_pending_login(student_id)
            if pending:
                self.refresh_login(student_id)
            else:
                self.start_login(student_id)
            self._send_telegram_text(chat_id, self._build_telegram_login_message(student_id))
            self._log_admin_action_safe(
                actor=f"telegram:{chat_id}",
                action="start_login",
                target_type="student",
                target_id=str(student_id),
                details="ERP login started or refreshed from Telegram.",
            )
            return
        raise ValueError("Unsupported Telegram student action.")

    def _handle_public_telegram_callback(self, chat_id: str, callback_id: str, data: str) -> None:
        try:
            if data == "tgpub:apply":
                self._start_public_application_session(chat_id)
                self._answer_telegram_callback(callback_id, "Application form started.")
                return
            if data == "tgpub:session:save":
                self._finalize_public_application_session(chat_id)
                self._answer_telegram_callback(callback_id, "Application submitted.")
                return
            if data == "tgpub:session:cancel":
                self.db.clear_telegram_admin_session(chat_id)
                self._send_telegram_text(
                    chat_id,
                    "Application request cancelled.",
                    reply_markup=self._telegram_public_markup(),
                )
                self._answer_telegram_callback(callback_id, "Cancelled.")
                return
            if data == "tgpub:help":
                self._send_telegram_text(
                    chat_id,
                    self._render_public_telegram_help(),
                    reply_markup=self._telegram_public_markup(),
                )
                self._answer_telegram_callback(callback_id)
                return
        except (StudentValidationError, TelegramError, ValueError) as exc:
            self._send_telegram_text(chat_id, f"Application request failed: {exc}")
            self._answer_telegram_callback(callback_id, "Action failed.", show_alert=True)
            return
        self._answer_telegram_callback(callback_id, "Unknown action.", show_alert=True)

    def _handle_telegram_session_input(
        self,
        chat_id: str,
        text: str,
        session: TelegramAdminSession,
    ) -> None:
        command = text.strip()
        if command.lower() in {"cancel", "/cancel"}:
            self.db.clear_telegram_admin_session(chat_id)
            self._send_telegram_text(
                chat_id,
                "Telegram student form cancelled.",
                reply_markup=self._telegram_root_markup(chat_id),
            )
            return

        try:
            payload = json.loads(session.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}

        is_edit = session.mode == "student_edit"
        step = session.step
        normalized = command.strip()
        lower_value = normalized.lower()

        if step == "student_label":
            if not (is_edit and lower_value == "skip"):
                if not normalized:
                    self._send_telegram_text(chat_id, "Student label is required. Send the student label or /cancel.")
                    return
                payload["student_label"] = normalized
        elif step == "user_name":
            if not (is_edit and lower_value == "skip"):
                if not normalized:
                    self._send_telegram_text(chat_id, "ERP user id is required. Send the ERP user id or /cancel.")
                    return
                payload["user_name"] = normalized
        elif step == "site_login_username":
            if lower_value == "skip":
                payload["site_login_username"] = ""
                payload["site_login_password"] = ""
            else:
                payload["site_login_username"] = normalized
        elif step == "site_login_password":
            if lower_value == "skip":
                payload["site_login_password"] = ""
            elif normalized and len(normalized) < 8:
                self._send_telegram_text(chat_id, "Site login password must be at least 8 characters long.")
                return
            else:
                payload["site_login_password"] = normalized
        elif step == "password":
            if is_edit and lower_value == "skip":
                payload["password"] = ""
            elif not normalized and not is_edit:
                self._send_telegram_text(chat_id, "ERP password is required for a new student profile.")
                return
            else:
                payload["password"] = normalized
        elif step == "telegram_chat_id":
            if lower_value == "self":
                payload["telegram_chat_id"] = chat_id
            elif lower_value == "skip":
                if not is_edit:
                    payload.setdefault("telegram_chat_id", "")
            else:
                payload["telegram_chat_id"] = normalized
        elif step == "timezone":
            if lower_value == "skip":
                if not is_edit:
                    payload.setdefault("timezone", self.settings.local_timezone)
            else:
                payload["timezone"] = normalized or self.settings.local_timezone
        elif step == "enabled":
            if lower_value in {"yes", "y", "on", "1", "true"}:
                payload["enabled"] = True
            elif lower_value in {"no", "n", "off", "0", "false"}:
                payload["enabled"] = False
            elif is_edit and lower_value == "skip":
                payload.setdefault("enabled", True)
            else:
                self._send_telegram_text(chat_id, "Send yes or no for the enabled state, or /cancel.")
                return
        else:
            self.db.clear_telegram_admin_session(chat_id)
            self._send_telegram_text(chat_id, "Telegram form state was reset. Open /students and try again.")
            return

        step_order = [
            "student_label",
            "user_name",
            "site_login_username",
            "site_login_password",
            "password",
            "telegram_chat_id",
            "timezone",
            "enabled",
        ]
        current_index = step_order.index(step)
        if current_index == len(step_order) - 1:
            self.db.save_telegram_admin_session(
                chat_id=chat_id,
                mode=session.mode,
                step="confirm",
                student_id=session.student_id,
                payload_json=json.dumps(payload),
            )
            self._send_telegram_text(
                chat_id,
                self._build_telegram_student_confirmation_text(payload, is_edit=is_edit),
                reply_markup=self._telegram_inline_markup(
                    [[
                        {"text": "Save Student", "callback_data": "tg:session:save"},
                        {"text": "Cancel", "callback_data": "tg:session:cancel"},
                    ]]
                ),
            )
            return

        next_step = step_order[current_index + 1]
        self.db.save_telegram_admin_session(
            chat_id=chat_id,
            mode=session.mode,
            step=next_step,
            student_id=session.student_id,
            payload_json=json.dumps(payload),
        )
        self._send_telegram_text(chat_id, self._telegram_student_step_prompt(next_step, payload, is_edit=is_edit))

    def _handle_public_telegram_session_input(
        self,
        chat_id: str,
        text: str,
        session: TelegramAdminSession,
    ) -> None:
        command = text.strip()
        if command.lower() in {"cancel", "/cancel"}:
            self.db.clear_telegram_admin_session(chat_id)
            self._send_telegram_text(
                chat_id,
                "Application request cancelled.",
                reply_markup=self._telegram_public_markup(),
            )
            return

        try:
            payload = json.loads(session.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}

        step = session.step
        normalized = command.strip()
        lower_value = normalized.lower()

        if step == "applicant_name":
            if not normalized:
                self._send_telegram_text(chat_id, "Full name is required. Send your full name or /cancel.")
                return
            payload["applicant_name"] = normalized
        elif step == "student_label":
            if not normalized:
                self._send_telegram_text(chat_id, "Preferred dashboard label is required.")
                return
            payload["student_label"] = normalized
        elif step == "user_name":
            if not normalized:
                self._send_telegram_text(chat_id, "ERP user id is required.")
                return
            payload["user_name"] = normalized
        elif step == "password":
            if not normalized:
                self._send_telegram_text(chat_id, "ERP password is required.")
                return
            payload["password"] = normalized
        elif step == "reg_id":
            payload["reg_id"] = "" if lower_value == "skip" else normalized
        elif step == "telegram_chat_id":
            if lower_value in {"self", "skip"}:
                payload["telegram_chat_id"] = chat_id if lower_value == "self" else ""
            else:
                payload["telegram_chat_id"] = normalized
        elif step == "timezone":
            payload["timezone"] = normalized or self.settings.local_timezone
        elif step == "note":
            payload["note"] = "" if lower_value == "skip" else normalized
        else:
            self.db.clear_telegram_admin_session(chat_id)
            self._send_telegram_text(chat_id, "Application form state was reset. Send /apply to start again.")
            return

        step_order = [
            "applicant_name",
            "student_label",
            "user_name",
            "password",
            "reg_id",
            "telegram_chat_id",
            "timezone",
            "note",
        ]
        current_index = step_order.index(step)
        if current_index == len(step_order) - 1:
            self.db.save_telegram_admin_session(
                chat_id=chat_id,
                mode="application_request",
                step="confirm",
                student_id=None,
                payload_json=json.dumps(payload),
            )
            self._send_telegram_text(
                chat_id,
                self._build_public_application_confirmation_text(payload),
                reply_markup=self._telegram_inline_markup(
                    [[
                        {"text": "Submit Application", "callback_data": "tgpub:session:save"},
                        {"text": "Cancel", "callback_data": "tgpub:session:cancel"},
                    ]]
                ),
            )
            return

        next_step = step_order[current_index + 1]
        self.db.save_telegram_admin_session(
            chat_id=chat_id,
            mode="application_request",
            step=next_step,
            student_id=None,
            payload_json=json.dumps(payload),
        )
        self._send_telegram_text(chat_id, self._public_application_step_prompt(next_step, payload))

    def _start_telegram_student_session(
        self,
        chat_id: str,
        *,
        mode: str,
        student_id: int | None = None,
    ) -> None:
        payload: dict[str, object]
        if mode == "student_edit":
            if student_id is None:
                raise ValueError("Student id is required for edit mode.")
            student = self._require_student(student_id)
            payload = {
                "student_label": student.student_label,
                "user_name": student.user_name,
                "site_login_username": student.site_login_username,
                "site_login_password": "",
                "password": "",
                "whatsapp_number": "",
                "telegram_chat_id": student.telegram_chat_id,
                "timezone": student.timezone,
                "enabled": bool(student.enabled),
            }
        else:
            payload = {
                "student_label": "",
                "user_name": "",
                "site_login_username": "",
                "site_login_password": "",
                "password": "",
                "whatsapp_number": "",
                "telegram_chat_id": "",
                "timezone": self.settings.local_timezone,
                "enabled": True,
            }
        self.db.save_telegram_admin_session(
            chat_id=chat_id,
            mode=mode,
            step="student_label",
            student_id=student_id,
            payload_json=json.dumps(payload),
        )
        self._send_telegram_text(
            chat_id,
            self._telegram_student_step_prompt("student_label", payload, is_edit=(mode == "student_edit")),
        )

    def _start_public_application_session(self, chat_id: str) -> None:
        payload = {
            "applicant_name": "",
            "student_label": "",
            "user_name": "",
            "password": "",
            "reg_id": "",
            "telegram_chat_id": chat_id,
            "timezone": self.settings.local_timezone,
            "note": "",
        }
        self.db.save_telegram_admin_session(
            chat_id=chat_id,
            mode="application_request",
            step="applicant_name",
            student_id=None,
            payload_json=json.dumps(payload),
        )
        self._send_telegram_text(
            chat_id,
            self._public_application_step_prompt("applicant_name", payload),
            reply_markup=self._telegram_public_markup(),
        )

    def _finalize_telegram_student_session(self, chat_id: str) -> None:
        session = self.db.get_telegram_admin_session(chat_id)
        if not session or session.step != "confirm":
            raise ValueError("No student form is ready to save.")
        payload = json.loads(session.payload_json or "{}")
        saved_id = self.save_student(
            student_id=session.student_id if session.mode == "student_edit" else None,
            student_label=str(payload.get("student_label") or ""),
            user_name=str(payload.get("user_name") or ""),
            password=str(payload.get("password") or ""),
            site_login_username=str(payload.get("site_login_username") or ""),
            site_login_password=str(payload.get("site_login_password") or ""),
            whatsapp_number="",
            telegram_chat_id=str(payload.get("telegram_chat_id") or ""),
            email_address="",
            enabled=bool(payload.get("enabled")),
            timezone=str(payload.get("timezone") or self.settings.local_timezone),
        )
        self.db.clear_telegram_admin_session(chat_id)
        self._log_admin_action_safe(
            actor=f"telegram:{chat_id}",
            action="save_student",
            target_type="student",
            target_id=str(saved_id),
            details="Student profile saved from Telegram.",
        )
        student = self._require_student(saved_id)
        self._send_telegram_text(
            chat_id,
            f"Student profile saved: {student.student_label}",
            reply_markup=self._telegram_student_actions_markup(student.id),
        )

    def _finalize_public_application_session(self, chat_id: str) -> None:
        session = self.db.get_telegram_admin_session(chat_id)
        if not session or session.mode != "application_request" or session.step != "confirm":
            raise ValueError("No application request is ready to submit.")
        payload = json.loads(session.payload_json or "{}")
        result = self.submit_application_request(
            applicant_name=str(payload.get("applicant_name") or ""),
            student_label=str(payload.get("student_label") or ""),
            user_name=str(payload.get("user_name") or ""),
            password=str(payload.get("password") or ""),
            whatsapp_number="",
            telegram_chat_id=str(payload.get("telegram_chat_id") or ""),
            timezone=str(payload.get("timezone") or self.settings.local_timezone),
            reg_id=str(payload.get("reg_id") or ""),
            note=str(payload.get("note") or ""),
            created_from_ip=f"telegram:{chat_id}",
        )
        self.db.clear_telegram_admin_session(chat_id)
        confirmation_lines = [
            "Application submitted successfully.",
            "",
            f"Request id: {result['id']}",
            "The admin team has been notified on Telegram.",
        ]
        if not result["notification_sent"]:
            confirmation_lines[-1] = "The request was saved, but admin Telegram notification could not be delivered right now."
        self._send_telegram_text(
            chat_id,
            "\n".join(confirmation_lines),
            reply_markup=self._telegram_public_markup(),
        )

    def _run_live_checks_from_admin(self, *, actor: str) -> str:
        self.run_scheduled_dispatch()
        self.run_due_checks()
        self.run_substitution_sweep()
        self.run_monitor_sweep()
        self.run_retry_sweep()
        self._log_admin_action_safe(
            actor=actor,
            action="run_live_checks",
            target_type="system",
            target_id="telegram",
            details="Manual live checks executed from Telegram.",
        )
        return "\n".join(
            [
                "Live checks executed.",
                "",
                "Included:",
                "- morning dispatch scan",
                "- attendance scan",
                "- substitution scan",
                "- monitoring scan",
                "- delivery retry scan",
            ]
        )

    def _push_telegram_dashboard_message(
        self,
        chat_id: str,
        *,
        now: datetime | None = None,
        live_mode: bool,
    ) -> None:
        self._ensure_telegram_admin_chat(chat_id)
        current = now or self._local_now()
        snapshot = self._build_telegram_dashboard_snapshot(chat_id)
        dashboard_hash = self._telegram_dashboard_hash(snapshot)
        body = self._build_telegram_dashboard_text(snapshot, current)
        markup = self._telegram_root_markup(chat_id)
        chat_state = self.db.get_telegram_admin_chat(chat_id)
        message_id = chat_state.dashboard_message_id if chat_state else None
        if live_mode and chat_state and chat_state.last_dashboard_hash == dashboard_hash and message_id:
            return
        if message_id:
            try:
                self.telegram.edit_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    body=body,
                    reply_markup=markup,
                )
            except TelegramError as exc:
                if "message is not modified" not in str(exc).lower():
                    message_id = self.telegram.send_text(chat_id, body, reply_markup=markup)
                else:
                    message_id = chat_state.dashboard_message_id
        else:
            message_id = self.telegram.send_text(chat_id, body, reply_markup=markup)
        self.db.upsert_telegram_admin_chat(
            chat_id=chat_id,
            dashboard_message_id=message_id,
            last_dashboard_sent_at=utcnow_iso(),
            last_dashboard_hash=dashboard_hash,
        )

    def _build_telegram_dashboard_snapshot(self, chat_id: str) -> dict[str, object]:
        students = self.list_students()
        outbound = self.get_outbound_queue_summary()
        return {
            "scheduler": "Running" if self.settings.run_scheduler else "Disabled",
            "queue": {
                "claimed": int(outbound.get("claimed", 0)),
                "sent": int(outbound.get("sent", 0)),
                "failed": int(outbound.get("failed", 0)),
                "dead_letter": int(outbound.get("dead_letter", 0)),
            },
            "students": [
                {
                    "id": student.id,
                    "label": student.student_label,
                    "user_name": student.user_name,
                    "telegram": student.telegram_chat_id or "Not set",
                    "reg_id": student.reg_id or "Not synced yet",
                    "session_updated_at": student.session_updated_at or "Not logged in yet",
                    "erp_status": self._student_erp_status_text(student),
                    "recent_activity": self._student_bot_activity_text(student),
                }
                for student in students[:10]
            ],
            "student_count": len(students),
        }

    def _build_telegram_dashboard_text(self, snapshot: dict[str, object], current: datetime) -> str:
        queue = snapshot.get("queue", {})
        students = snapshot.get("students", [])
        student_count = int(snapshot.get("student_count", 0))
        lines = [
            "QUMS Admin Control",
            "",
            f"Last change: {self._format_datetime(current)}",
            f"Scheduler: {snapshot.get('scheduler', 'Unknown')}",
            "",
            "Queue",
            f"- Claimed: {queue.get('claimed', 0)}",
            f"- Sent: {queue.get('sent', 0)}",
            f"- Waiting retry: {queue.get('failed', 0)}",
            f"- Dead letter: {queue.get('dead_letter', 0)}",
            "",
            "Students",
        ]
        if not students:
            lines.append("- No student profiles saved.")
        for student in students:
            lines.extend(
                [
                    f"- {student['label']}",
                    f"  ERP user id: {student['user_name']}",
                    f"  Telegram: {student['telegram']}",
                    f"  RegID: {student['reg_id']}",
                    f"  Session updated: {student['session_updated_at']}",
                    f"  ERP session: {student['erp_status']}",
                ]
            )
            if student.get("recent_activity"):
                lines.append(f"  Recent bot activity: {student['recent_activity']}")
        if student_count > len(students):
            lines.append(f"- plus {student_count - len(students)} more student profiles")
        return "\n".join(lines)

    def _telegram_dashboard_hash(self, snapshot: dict[str, object]) -> str:
        serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _build_telegram_students_text(self) -> str:
        students = self.list_students()
        lines = ["Student Profiles", ""]
        if not students:
            lines.append("No student profiles are saved.")
            return "\n".join(lines)
        for student in students:
            lines.extend(
                [
                    f"{student.id}. {student.student_label}",
                    f"  ERP user id: {student.user_name}",
                    f"  Telegram: {student.telegram_chat_id or 'Not set'}",
                    f"  RegID: {student.reg_id or 'Not synced yet'}",
                    f"  Session updated: {student.session_updated_at or 'Not logged in yet'}",
                    f"  ERP session: {self._student_erp_status_text(student)}",
                    f"  Recent bot activity: {self._student_bot_activity_text(student)}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    def _build_telegram_student_menu_text(self, student: Student) -> str:
        disabled_actions = [
            STUDENT_ACTION_LABELS[action_key]
            for action_key in STUDENT_ACTION_ORDER
            if action_key in self.get_student_disabled_actions(student)
        ]
        return "\n".join(
            [
                f"Student: {student.student_label}",
                "",
                f"ERP user id: {student.user_name}",
                f"Telegram: {student.telegram_chat_id or 'Not set'}",
                f"RegID: {student.reg_id or 'Not synced yet'}",
                f"Session updated: {student.session_updated_at or 'Not logged in yet'}",
                f"Timezone: {student.timezone}",
                f"Enabled: {'Yes' if student.enabled else 'No'}",
                f"Profile status: {'Active' if student.enabled else 'Blocked'}",
                f"Delivery: {self.get_student_notification_channel_label(student)}",
                f"Disabled features: {', '.join(disabled_actions) if disabled_actions else 'None'}",
                f"ERP session: {self._student_erp_status_text(student)}",
                f"Recent bot activity: {self._student_bot_activity_text(student)}",
            ]
        )

    def _build_telegram_applications_text(self) -> str:
        applications = self.list_application_requests(10)
        lines = ["Application Requests", ""]
        if not applications:
            lines.append("No application requests have been submitted yet.")
            return "\n".join(lines)
        for item in applications:
            lines.extend(
                [
                    f"{item.id}. {item.applicant_name} -> {item.student_label}",
                    f"  ERP user id: {item.user_name}",
                    f"  Telegram: {item.telegram_chat_id or 'Not provided'}",
                    f"  Status: {item.status.title()}",
                    f"  Submitted: {item.created_at}",
                    "",
                ]
            )
        lines.append("Use /application <id> to view the full request.")
        return "\n".join(lines).rstrip()

    def _build_telegram_application_detail_text(self, application: ApplicationRequest) -> str:
        lines = [
            f"Application Request: {application.id}",
            "",
            f"Applicant: {application.applicant_name}",
            f"Preferred label: {application.student_label}",
            f"ERP user id: {application.user_name}",
            "ERP password: Submitted securely and hidden from admin views.",
            f"RegID: {application.reg_id or 'Not provided'}",
            f"Telegram: {application.telegram_chat_id or 'Not provided'}",
            f"Timezone: {application.timezone}",
            f"Status: {application.status.title()}",
            f"Created at: {application.created_at}",
            f"Updated at: {application.updated_at}",
            f"Source: {application.created_from_ip or 'Not available'}",
        ]
        if application.note:
            lines.extend(["", "Note:", application.note])
        return "\n".join(lines)

    def _build_telegram_login_message(self, student_id: int) -> str:
        student = self._require_student(student_id)
        login_url = self._dashboard_login_url(student_id)
        lines = [
            f"ERP login handoff ready for {student.student_label}.",
            "",
            "Captcha login still needs the website.",
        ]
        if login_url:
            lines.append(f"Open this page to enter the captcha: {login_url}")
        else:
            lines.append("Open the local dashboard on the bot host and complete the captcha there.")
        return "\n".join(lines)

    def _build_message_history_csv(self) -> str:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "sent_at",
                "student_label",
                "channel",
                "recipient",
                "category",
                "message_kind",
                "provider_sid",
                "delivery_status",
                "delivery_error_code",
                "title",
                "idempotency_key",
            ]
        )
        for item in self.list_message_history(500):
            writer.writerow(
                [
                    item.sent_at,
                    item.student_label,
                    item.channel,
                    item.recipient,
                    item.category,
                    item.message_kind,
                    item.provider_sid,
                    item.delivery_status or "",
                    item.delivery_error_code or "",
                    item.title,
                    item.idempotency_key or "",
                ]
            )
        return output.getvalue()

    def _build_admin_audit_csv(self) -> str:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["created_at", "actor", "action", "target_type", "target_id", "details"])
        for item in self.list_admin_audit_log(500):
            writer.writerow([item.created_at, item.actor, item.action, item.target_type, item.target_id, item.details])
        return output.getvalue()

    def _render_telegram_admin_help(self, chat_id: str) -> str:
        return "\n".join(
            [
                "QUMS Telegram Admin",
                "",
                "Manual commands",
                "- /menu",
                "- /dashboard",
                "- /students",
                "- /student <id>",
                "- /addstudent",
                "- /editstudent <id>",
                "- /applications",
                "- /application <id>",
                "- /preview <id>",
                "- /attendance <id>",
                "- /morning <id>",
                "- /dayreport <id>",
                "- /shortage <id>",
                "- /test <id>",
                "- /startlogin <id>",
                "- /deleteprofile <id>",
                "- /cancel",
                "",
                "QUMS Admin Control in Telegram is dashboard-only.",
                "Background dashboard refresh runs silently and only sends an update when something changes.",
                "All automatic alerts and scheduled checks continue to run in the background.",
            ]
        )

    def _render_public_telegram_help(self) -> str:
        lines = [
            "QUMS Telegram Bot",
            "",
            "Public commands",
            "- /start",
            "- /apply",
            "- /cancel",
            "",
            "Use /apply to submit a new student application.",
            "The admin will receive a Telegram notification when you submit it.",
        ]
        if self.settings.public_base_url:
            lines.append(f"Website dashboard: {self.settings.public_base_url}/")
        return "\n".join(lines)

    def _render_student_telegram_help(self, student: Student) -> str:
        return "\n".join(
            [
                "QUMS Student Panel",
                "",
                f"Student: {student.student_label}",
                "",
                "Available commands",
                "- /menu",
                "- /dashboard",
                "- /help",
                "- /preview",
                "- /attendance",
                "- /morning",
                "- /dayreport",
                "- /shortage",
                "- /test",
                "- /startlogin",
                "",
                "This Telegram chat is linked only to your own student profile.",
                "Other student profiles and admin controls are not available here.",
            ]
        )

    def _telegram_root_markup(self, chat_id: str) -> dict | None:
        return None

    def _telegram_public_markup(self) -> dict:
        return self._telegram_inline_markup(
            [
                [{"text": "Create Application", "callback_data": "tgpub:apply"}],
                [{"text": "Help", "callback_data": "tgpub:help"}],
            ]
        )

    def _telegram_self_actions_markup(self) -> dict:
        return self._telegram_inline_markup(
            [
                [
                    {"text": "Preview Today", "callback_data": "tgs:preview"},
                    {"text": "Send Attendance", "callback_data": "tgs:attendance"},
                ],
                [
                    {"text": "Send Morning", "callback_data": "tgs:morning"},
                    {"text": "Send Day Report", "callback_data": "tgs:evening"},
                ],
                [
                    {"text": "Send Shortage", "callback_data": "tgs:shortage"},
                    {"text": "Channel Test", "callback_data": "tgs:test"},
                ],
                [
                    {"text": "Start Login", "callback_data": "tgs:login"},
                    {"text": "Help", "callback_data": "tgs:help"},
                ],
                [
                    {"text": "My Profile", "callback_data": "tgs:menu"},
                ],
            ]
        )

    def _telegram_students_markup(self) -> dict:
        rows = []
        for student in self.list_students():
            rows.append([{"text": student.student_label, "callback_data": f"tg:student:{student.id}:menu"}])
        rows.append([{"text": "Add Student", "callback_data": "tg:student:add"}])
        rows.append([{"text": "Dashboard", "callback_data": "tg:dashboard"}])
        return self._telegram_inline_markup(rows)

    def _telegram_student_actions_markup(self, student_id: int) -> dict:
        return self._telegram_inline_markup(
            [
                [
                    {"text": "Preview Today", "callback_data": f"tg:student:{student_id}:preview"},
                    {"text": "Send Attendance", "callback_data": f"tg:student:{student_id}:attendance"},
                ],
                [
                    {"text": "Send Morning", "callback_data": f"tg:student:{student_id}:morning"},
                    {"text": "Send Day Report", "callback_data": f"tg:student:{student_id}:evening"},
                ],
                [
                    {"text": "Send Shortage", "callback_data": f"tg:student:{student_id}:shortage"},
                    {"text": "Channel Test", "callback_data": f"tg:student:{student_id}:test"},
                ],
                [
                    {"text": "Start Login", "callback_data": f"tg:student:{student_id}:login"},
                    {"text": "Edit Profile", "callback_data": f"tg:student:{student_id}:edit"},
                ],
                [
                    {"text": "Delete Profile", "callback_data": f"tg:student:{student_id}:deleteconfirm"},
                    {"text": "Students", "callback_data": "tg:students"},
                ],
                [
                    {"text": "Dashboard", "callback_data": "tg:dashboard"},
                ],
            ]
        )

    def _telegram_inline_markup(self, rows: list[list[dict[str, str]]]) -> dict:
        return {"inline_keyboard": rows}

    def _public_application_step_prompt(self, step: str, payload: dict) -> str:
        if step == "applicant_name":
            return "Full name\nSend your full name for the application."
        if step == "student_label":
            return "Preferred dashboard label\nSend the name the admin should use for your student profile."
        if step == "user_name":
            return "ERP user id\nSend your ERP user id."
        if step == "password":
            return "ERP password\nSend your ERP password so the admin can add your profile."
        if step == "reg_id":
            return "Registration id\nSend your RegID, or send skip."
        if step == "telegram_chat_id":
            return "Telegram chat id\nSend self to use this chat, send a numeric chat id, or send skip."
        if step == "timezone":
            return f"Timezone\nSend your timezone or send {self.settings.local_timezone}."
        if step == "note":
            return "Application note\nSend any extra note, or send skip."
        return "Send the next value."

    def _build_public_application_confirmation_text(self, payload: dict) -> str:
        return "\n".join(
            [
                "Application Summary",
                "",
                f"Full name: {payload.get('applicant_name') or 'Not set'}",
                f"Preferred label: {payload.get('student_label') or 'Not set'}",
                f"ERP user id: {payload.get('user_name') or 'Not set'}",
                f"ERP password: {'Provided' if payload.get('password') else 'Not set'}",
                f"RegID: {payload.get('reg_id') or 'Not provided'}",
                f"Telegram: {payload.get('telegram_chat_id') or 'Not provided'}",
                f"Timezone: {payload.get('timezone') or self.settings.local_timezone}",
                f"Note: {payload.get('note') or 'Not provided'}",
                "",
                "Use Submit Application to send this request to the admin.",
            ]
        )

    def _telegram_student_step_prompt(self, step: str, payload: dict, *, is_edit: bool) -> str:
        suffix = " Send skip to keep the current value." if is_edit else ""
        if step == "student_label":
            current = f"Current: {payload.get('student_label')}" if is_edit else ""
            return f"Student label{suffix}\n{current}".strip()
        if step == "user_name":
            current = f"Current ERP user id: {payload.get('user_name')}" if is_edit else ""
            return f"ERP user id{suffix}\n{current}".strip()
        if step == "site_login_username":
            current = f"Current site login username: {payload.get('site_login_username') or 'Not set'}" if is_edit else ""
            lines = ["Site login username", "Send the login username for this student's website access, or send skip to disable it."]
            if current:
                lines.append(current)
            return "\n".join(lines)
        if step == "site_login_password":
            if is_edit:
                return "Site login password\nSend a new site login password, or send skip to keep the current one."
            return "Site login password\nSend the website login password for this student, or send skip to leave website login disabled."
        if step == "password":
            if is_edit:
                return "ERP password\nSend the new password, or send skip to keep the existing password."
            return "ERP password\nSend the ERP password for the new student profile."
        if step == "telegram_chat_id":
            current = f"Current Telegram: {payload.get('telegram_chat_id') or 'Not set'}" if is_edit else ""
            lines = [
                "Telegram chat id",
                "Send a numeric chat id, self, or skip.",
            ]
            if current:
                lines.append(current)
            return "\n".join(lines)
        if step == "timezone":
            current = (
                f"Current timezone: {payload.get('timezone')}"
                if is_edit
                else f"Default timezone: {self.settings.local_timezone}"
            )
            return f"Timezone{suffix}\n{current}".strip()
        if step == "enabled":
            current = ""
            if is_edit:
                current = f"Current enabled state: {'Yes' if payload.get('enabled') else 'No'}"
            lines = ["Enable this student profile?", "Send yes or no."]
            if current:
                lines.append(current)
            return "\n".join(lines)
        return "Send the next value."

    def _build_telegram_student_confirmation_text(self, payload: dict, *, is_edit: bool) -> str:
        return "\n".join(
            [
                f"{'Edit' if is_edit else 'Add'} Student Summary",
                "",
                f"Student label: {payload.get('student_label') or 'Not set'}",
                f"ERP user id: {payload.get('user_name') or 'Not set'}",
                f"Site login username: {payload.get('site_login_username') or 'Disabled'}",
                f"Site login password: {'Updated' if payload.get('site_login_password') else 'Keep existing / disabled'}",
                f"Password: {'Updated' if payload.get('password') else 'Keep existing / not set'}",
                f"Telegram: {payload.get('telegram_chat_id') or 'Not set'}",
                f"Timezone: {payload.get('timezone') or self.settings.local_timezone}",
                f"Enabled: {'Yes' if payload.get('enabled') else 'No'}",
                "",
                "Use Save Student to apply these changes.",
            ]
        )

    def _send_telegram_text(
        self,
        chat_id: str,
        body: str,
        *,
        reply_markup: dict | None = None,
    ) -> str:
        return self.telegram.send_text(chat_id, body, message_kind="generic", reply_markup=reply_markup)

    def _send_telegram_document(
        self,
        *,
        chat_id: str,
        filename: str,
        content_text: str,
        caption: str,
    ) -> str:
        return self.telegram.send_document(
            chat_id=chat_id,
            filename=filename,
            content_bytes=content_text.encode("utf-8"),
            caption=caption,
        )

    def _answer_telegram_callback(self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False) -> None:
        if not callback_query_id:
            return
        try:
            self.telegram.answer_callback_query(
                callback_query_id=callback_query_id,
                text=text,
                show_alert=show_alert,
            )
        except TelegramError:
            return

    def _ensure_telegram_admin_chat(self, chat_id: str) -> None:
        chat_state = self.db.get_telegram_admin_chat(chat_id)
        if chat_state:
            if not chat_state.auto_refresh_enabled:
                self.db.upsert_telegram_admin_chat(chat_id=chat_id, auto_refresh_enabled=True)
            return
        self.db.upsert_telegram_admin_chat(chat_id=chat_id, auto_refresh_enabled=True)

    def _is_telegram_admin_chat(self, chat_id: str) -> bool:
        return str(chat_id).strip() in {
            value.strip()
            for value in self.settings.telegram_admin_chat_ids
            if value.strip()
        }

    def _extract_telegram_student_id(self, callback_data: str, *, suffix: str) -> int:
        prefix = "tg:student:"
        if not callback_data.startswith(prefix) or not callback_data.endswith(suffix):
            raise ValueError("Invalid Telegram student callback.")
        raw_id = callback_data[len(prefix):-len(suffix)]
        return int(raw_id)

    def _dashboard_login_url(self, student_id: int) -> str:
        base = self.settings.public_base_url or f"http://127.0.0.1:{self.settings.flask_port}"
        return f"{base}/students/{student_id}/login"

    def _send_telegram_result_if_needed(self, chat_id: str, student_id: int, body: str) -> None:
        student = self._require_student(student_id)
        if self._telegram_delivery_targets_chat(student, chat_id):
            return
        self._send_telegram_text(chat_id, body)

    def _telegram_delivery_targets_chat(self, student: Student, chat_id: str) -> bool:
        if not self.telegram.configured:
            return False
        target = str(student.telegram_chat_id or "").strip()
        return bool(target and target == str(chat_id).strip())

    def _telegram_action_state_key(self, chat_id: str, action_name: str, target_id: str) -> str:
        return f"telegram_action_guard:{chat_id}:{action_name}:{target_id}"

    def _claim_telegram_action_window(
        self,
        chat_id: str,
        action_name: str,
        target_id: str,
        *,
        cooldown_seconds: int = 20,
    ) -> bool:
        state_key = self._telegram_action_state_key(chat_id, action_name, target_id)
        current = self._local_now()
        last_value = self.db.get_runtime_state(state_key)
        last_seen = self._parse_datetime(last_value)
        if last_seen is not None and (current - last_seen.astimezone(current.tzinfo)).total_seconds() < cooldown_seconds:
            return False
        self.db.upsert_runtime_state(state_key=state_key, state_value=current.isoformat())
        return True

    def _log_admin_action_safe(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        details: str,
    ) -> None:
        try:
            self.log_admin_action(
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        except Exception:
            return

    def _ensure_telegram_bot_commands_registered(self) -> None:
        expected_state = self._telegram_bot_commands_state_value()
        if self.db.get_runtime_state("telegram_bot_commands_registered") == expected_state:
            return
        try:
            self.telegram.set_commands(self._telegram_bot_commands())
        except TelegramError:
            return
        self.db.upsert_runtime_state(
            state_key="telegram_bot_commands_registered",
            state_value=expected_state,
        )

    def _telegram_bot_commands_state_value(self) -> str:
        serialized = json.dumps(self._telegram_bot_commands(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _telegram_bot_commands(self) -> list[dict[str, str]]:
        return [
            {"command": "menu", "description": "Open the admin control panel"},
            {"command": "dashboard", "description": "Show the live admin dashboard"},
            {"command": "apply", "description": "Submit a public student application"},
            {"command": "students", "description": "List student profiles"},
            {"command": "applications", "description": "List public application requests"},
            {"command": "application", "description": "Show one application request by id"},
            {"command": "student", "description": "Open a student action menu by id"},
            {"command": "addstudent", "description": "Start the add-student form"},
            {"command": "editstudent", "description": "Start the edit form by id"},
            {"command": "preview", "description": "Preview today's schedule by student id"},
            {"command": "attendance", "description": "Send attendance summary by student id"},
            {"command": "morning", "description": "Send morning summary by student id"},
            {"command": "dayreport", "description": "Send day report by student id"},
            {"command": "shortage", "description": "Send shortage report by student id"},
            {"command": "test", "description": "Send a channel test by student id"},
            {"command": "startlogin", "description": "Open the ERP captcha login handoff"},
            {"command": "deleteprofile", "description": "Delete a student profile by id"},
            {"command": "cancel", "description": "Cancel the active Telegram form"},
        ]

    def run_morning_sweep(self) -> None:
        self.run_scheduled_dispatch()

    def run_scheduled_dispatch(self, *, now: datetime | None = None) -> None:
        current = now or self._local_now()
        for student in self.db.list_students():
            if not student.enabled:
                continue
            if self.is_student_action_disabled(student, "send_morning"):
                continue
            student_now = self._now_for_student(student, current)
            if not self._is_morning_dispatch_due(student, student_now):
                continue
            try:
                self.send_morning_update(student.id, target_date=student_now.date())
            except AuthenticationRequired:
                self._handle_authentication_required(student, detected_at=student_now)
            except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
                self.db.update_student_status(student.id, f"Morning sync failed: {exc}")

    def run_substitution_sweep(self, *, now: datetime | None = None) -> None:
        current = now or self._local_now()
        for student in self.db.list_students():
            if not student.enabled:
                continue
            student_now = self._now_for_student(student, current)
            target_date = student_now.date()
            try:
                payload = self.erp.get_substitutions(student)
                substitutions = parse_substitutions(payload, target_date)
                lecture_events = self.db.get_lecture_events_for_day(student.id, target_date)
                self._sync_substitutions(
                    student=student,
                    target_date=target_date,
                    substitutions=substitutions,
                    lecture_events=lecture_events,
                    source="live_alert",
                    send_alerts=True,
                    detected_at=student_now,
                )
            except AuthenticationRequired:
                self._handle_authentication_required(student, detected_at=student_now)
            except ERPClientError as exc:
                self.db.update_student_status(student.id, f"Substitution check failed: {exc}")
            except (WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
                self.db.update_student_status(student.id, f"Delivery failed: {exc}")

    def run_monitor_sweep(self, *, now: datetime | None = None) -> None:
        current = now or self._local_now()
        for student in self.db.list_students():
            if not student.enabled:
                continue
            student_now = self._now_for_student(student, current)

            try:
                self._check_erp_session(student, None, student_now)
            except (WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
                self.db.update_student_status(student.id, f"Delivery failed: {exc}")

    def run_retry_sweep(self, *, now: datetime | None = None) -> None:
        current = now or self._local_now()
        due_messages = self.db.get_retryable_outbound_messages(now=current)
        for message in due_messages:
            if not self.db.try_claim_retry_outbound_message(message.idempotency_key):
                continue
            student = self.db.get_student(message.student_id)
            if not student or not student.enabled:
                self.db.mark_outbound_message_failed(
                    message.idempotency_key,
                    "Student record is missing or disabled.",
                    retry_limit=self.settings.delivery_retry_limit,
                    retry_backoff_seconds=self.settings.delivery_retry_backoff_seconds,
                )
                continue
            try:
                self._deliver_retry_message(student, message)
            except Exception as exc:
                self.db.update_student_status(
                    student.id,
                    f"Retry delivery encountered an internal failure for {message.category}: {exc}",
                )

    def run_due_checks(self) -> None:
        now = self._local_now()
        pending_events = self.db.get_pending_lecture_events()
        events_by_student: dict[int, list[LectureEvent]] = defaultdict(list)
        for event in pending_events:
            events_by_student[event.student_id].append(event)

        lookback_start = now.date() - timedelta(days=self.settings.attendance_correction_lookback_days)
        for student in self.db.list_students():
            if not student.enabled:
                continue
            student_now = self._now_for_student(student, now)
            try:
                self._ensure_attendance_tracking_for_date(student, student_now.date(), detected_at=student_now)
            except AuthenticationRequired:
                self._handle_authentication_required(student, detected_at=student_now)
                continue
            except ERPClientError as exc:
                self.db.update_student_status(student.id, f"Attendance routine sync failed: {exc}")
                continue

            student_events = list(events_by_student.get(student.id, []))
            tracked_today_events = self.db.get_lecture_events_for_day(student.id, student_now.date())
            tracked_today_pending = [
                event
                for event in tracked_today_events
                if event.check_after and event.status in {"scheduled", "notified_unmarked"}
            ]
            known_ids = {event.id for event in student_events}
            student_events.extend(
                event
                for event in tracked_today_pending
                if event.id not in known_ids
            )
            due_events = [
                event
                for event in student_events
                if event.check_after and event.check_after <= student_now.replace(tzinfo=None)
            ]
            has_recent_history = self.db.has_lecture_events_since(student.id, lookback_start)
            if not due_events and not has_recent_history:
                continue
            try:
                self._process_attendance_scan(student, due_events, student_now)
            except AuthenticationRequired:
                self._handle_authentication_required(student, detected_at=student_now)
            except ERPClientError as exc:
                self.db.update_student_status(student.id, f"Attendance check failed: {exc}")
            except (WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
                self.db.update_student_status(student.id, f"Delivery failed: {exc}")

        self.run_evening_sweep(now=now)

    def _ensure_attendance_tracking_for_date(
        self,
        student: Student,
        target_date: date,
        *,
        detected_at: datetime,
    ) -> None:
        existing_events = self.db.get_lecture_events_for_day(student.id, target_date)
        if existing_events:
            return

        timetable_payload = self.erp.get_timetable(student)
        substitutions_payload = self.erp.get_substitutions(student)
        substitutions = parse_substitutions(substitutions_payload, target_date)
        slots = parse_timetable_slots(timetable_payload, target_date)
        if not slots:
            slots = parse_timetable_slots(substitutions_payload, target_date)
        if not slots:
            slots = self._default_non_lecture_slots(target_date, substitutions)

        self.db.replace_lecture_events(
            student_id=student.id,
            event_date=target_date,
            slots=slots,
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        lecture_events = self.db.get_lecture_events_for_day(student.id, target_date)
        self._sync_substitutions(
            student=student,
            target_date=target_date,
            substitutions=substitutions,
            lecture_events=lecture_events,
            source="attendance_bootstrap",
            send_alerts=False,
            detected_at=detected_at,
        )
        self.db.mark_student_erp_sync(student.id)
        self.db.update_student_bot_activity(
            student.id,
            f"Background attendance tracking synced today's lecture routine for {target_date.isoformat()}.",
        )

    def run_evening_sweep(self, *, now: datetime | None = None) -> None:
        current = now or self._local_now()
        for student in self.db.list_students():
            if not student.enabled:
                continue
            if self.is_student_action_disabled(student, "send_day_report"):
                continue
            student_now = self._now_for_student(student, current)
            target_date = student_now.date()
            try:
                self._send_evening_report_if_due(student, target_date, now=student_now)
            except AuthenticationRequired:
                self._handle_authentication_required(student, detected_at=student_now)
            except ERPClientError as exc:
                self.db.update_student_status(student.id, f"Evening report failed: {exc}")
            except (WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
                self.db.update_student_status(student.id, f"Delivery failed: {exc}")

    def _collect_daily_summary(
        self,
        student: Student,
        target_date: date,
        *,
        send_risk_alerts: bool = True,
    ) -> dict[str, object]:
        timetable_payload = self.erp.get_timetable(student)
        substitutions_payload = self.erp.get_substitutions(student)
        attendance_payload = self.erp.get_attendance_summary(student)
        detail_payload = self.erp.get_student_detail(student)

        detail = parse_student_detail_response(detail_payload) or {}
        substitutions = parse_substitutions(substitutions_payload, target_date)
        slots = parse_timetable_slots(timetable_payload, target_date)
        if not slots:
            slots = parse_timetable_slots(substitutions_payload, target_date)
        if not slots:
            slots = self._default_non_lecture_slots(target_date, substitutions)
        attendance = parse_attendance_summary(attendance_payload)

        self.db.replace_lecture_events(
            student_id=student.id,
            event_date=target_date,
            slots=slots,
            grace_minutes=self.settings.lecture_grace_minutes,
        )
        lecture_events = self.db.get_lecture_events_for_day(student.id, target_date)
        self._sync_substitutions(
            student=student,
            target_date=target_date,
            substitutions=substitutions,
            lecture_events=lecture_events,
            source="morning_sync",
            send_alerts=False,
            detected_at=None,
        )
        lecture_events = self.db.get_lecture_events_for_day(student.id, target_date)
        for record in attendance:
            self.db.upsert_attendance_snapshot(
                student_id=student.id,
                subject_key=record.subject_key,
                subject_name=record.subject_name,
                subject_code=record.subject_code,
                teacher_name=record.teacher_name,
                total_lecture=record.total_lecture,
                total_present=record.total_present,
                percentage=record.percentage,
            )
        if send_risk_alerts:
            self._evaluate_attendance_risk(
                student,
                attendance,
                self._now_for_student(student),
                lecture_events=lecture_events,
            )

        message = self._render_morning_message(
            student_name=str(detail.get("StudentName") or student.student_name or student.student_label).strip(),
            target_date=target_date,
            substitutions=substitutions,
            lecture_events=lecture_events,
        )
        self.db.mark_student_erp_sync(student.id)
        self.db.update_student_bot_activity(student.id, f"ERP sync completed for {target_date.isoformat()}.")
        return {"message": message, "slots": slots, "substitutions": substitutions}

    def _default_non_lecture_slots(
        self,
        target_date: date,
        substitutions: list[Substitution],
    ) -> list[LectureSlot]:
        if substitutions:
            return []
        if target_date.weekday() == 6:
            return [
                LectureSlot(
                    slot_label="All day",
                    subject_key=normalize_subject_key("off day"),
                    subject_name="Off Day",
                    teacher_name="",
                    raw_cell="Sunday off day",
                    start_time=None,
                    end_time=None,
                    is_break=True,
                    note="Sunday off day",
                )
            ]
        return []

    def _check_sandbox_expiry(
        self,
        student: Student,
        channel_status: WhatsAppChannelStatus,
        now: datetime,
    ) -> None:
        return

    def _check_erp_session(
        self,
        student: Student,
        channel_status: WhatsAppChannelStatus | None,
        now: datetime,
    ) -> None:
        if not student.session_cookies:
            return
        if not self._should_probe_erp_session(student, now):
            return

        try:
            self.erp.ensure_authenticated(student)
        except AuthenticationRequired:
            self._handle_authentication_required(student, detected_at=now, channel_status=channel_status)
        except ERPClientError as exc:
            self.db.update_student_bot_activity(student.id, f"ERP session check failed: {exc}")

    def _should_probe_erp_session(self, student: Student, now: datetime) -> bool:
        threshold = timedelta(
            minutes=max(
                self.settings.monitor_poll_interval_minutes,
                ERP_SESSION_MONITOR_COOLDOWN_MINUTES,
            )
        )
        latest_activity: datetime | None = None
        for raw_value in (student.last_erp_sync_at, student.session_updated_at):
            if not raw_value:
                continue
            try:
                parsed = self._parse_datetime(raw_value)
            except ValueError:
                continue
            if parsed is None:
                continue
            normalized = parsed.astimezone(now.tzinfo or self.timezone)
            if latest_activity is None or normalized > latest_activity:
                latest_activity = normalized
        if latest_activity is None:
            return True
        return (now - latest_activity) >= threshold

    def _handle_authentication_required(
        self,
        student: Student,
        *,
        detected_at: datetime,
        channel_status: WhatsAppChannelStatus | None = None,
    ) -> None:
        status_text = "ERP session expired. Open the dashboard and complete login again with a fresh captcha."
        self.db.update_student_erp_status(student.id, status_text)

        if not student.session_updated_at:
            return
        if self.db.has_notification_event(student.id, "erp_session_expired", student.session_updated_at):
            return

        targets = self._delivery_targets(student)
        if not targets:
            return

        body = self._render_erp_session_expired_alert(student, detected_at=detected_at)
        self._send_whatsapp(
            student,
            body,
            message_kind="attendance",
            history_category="erp_session_expired",
            idempotency_key=f"erp_session_expired:{student.id}:{student.session_updated_at}",
        )
        self.db.upsert_notification_event(
            student_id=student.id,
            category="erp_session_expired",
            notification_key=student.session_updated_at,
            message_text=body,
        )

    def _sync_substitutions(
        self,
        *,
        student: Student,
        target_date: date,
        substitutions: list[Substitution],
        lecture_events: list[LectureEvent],
        source: str,
        send_alerts: bool,
        detected_at: datetime | None,
    ) -> None:
        known_keys = self.db.get_substitution_alert_keys(student.id, target_date)
        for item in substitutions:
            context = self._build_substitution_context(item, lecture_events)
            if context["event"] is not None:
                self._apply_substitution_to_event(context["event"], context)

            alert_key = self._build_substitution_alert_key(target_date, context)
            if alert_key in known_keys:
                continue

            notified_at = None
            if send_alerts:
                alert_time = detected_at or self._local_now()
                body = self._render_substitution_alert(target_date, context, detected_at=alert_time)
                self._send_whatsapp(
                    student,
                    body,
                    message_kind="attendance",
                    history_category="substitution_alert",
                    idempotency_key=f"substitution_alert:{student.id}:{target_date.isoformat()}:{alert_key}",
                )
                notified_at = alert_time.replace(microsecond=0).isoformat()

            self.db.upsert_substitution_alert(
                student_id=student.id,
                event_date=target_date,
                alert_key=alert_key,
                period=context["period_text"],
                time_text=context["lecture_time_text"],
                subject_name=context["subject_name"],
                teacher_name=context["assigned_teacher"],
                end_time_text=context["end_time_text"],
                source=source,
                notified_at=notified_at,
            )
            known_keys.add(alert_key)

    def _build_substitution_context(
        self,
        item: Substitution,
        lecture_events: list[LectureEvent],
    ) -> dict[str, LectureEvent | str | None]:
        matched_event = self._match_substitution_event(item, lecture_events)
        subject_name = (
            item.substitute_subject
            or item.original_subject
            or (matched_event.subject_name if matched_event else "")
            or "Updated class"
        )
        assigned_teacher = (
            item.substitute_teacher
            or item.original_teacher
            or (matched_event.teacher_name if matched_event else "")
            or "Not available"
        )
        lecture_time_text, end_time_text = self._resolve_substitution_time_window(item, matched_event)
        period_text = item.period or (matched_event.slot_label if matched_event else "") or "Scheduled lecture"
        location_text = self._event_class_location(matched_event) if matched_event else ""
        return {
            "event": matched_event,
            "period_text": period_text,
            "lecture_time_text": lecture_time_text,
            "end_time_text": end_time_text,
            "location_text": location_text,
            "subject_name": subject_name,
            "original_subject": item.original_subject or subject_name,
            "assigned_teacher": assigned_teacher,
            "original_teacher": item.original_teacher or "",
        }

    def _match_substitution_event(
        self,
        item: Substitution,
        lecture_events: list[LectureEvent],
    ) -> LectureEvent | None:
        period_text = self._normalize_text(item.period)
        item_time_text = self._normalize_text(item.time_text)
        item_start, item_end = parse_time_range(item.time_text, item.period)
        subject_tokens = {
            normalize_subject_key(value)
            for value in (item.substitute_subject, item.original_subject)
            if value
        }

        best_event: LectureEvent | None = None
        best_score = 0
        for event in lecture_events:
            if event.is_break:
                continue

            score = 0
            event_slot_text = self._normalize_text(event.slot_label)
            event_time_text = self._normalize_text(self._format_event_time(event))
            event_subject = normalize_subject_key(event.subject_name)

            if period_text and period_text in event_slot_text:
                score += 3
            if item_time_text and item_time_text in event_time_text:
                score += 4
            if item_start and event.start_time and item_start == event.start_time:
                score += 4
            if item_end and event.end_time and item_end == event.end_time:
                score += 4
            if subject_tokens and event_subject in subject_tokens:
                score += 2

            if score > best_score:
                best_event = event
                best_score = score

        return best_event if best_score > 0 else None

    def _resolve_substitution_time_window(
        self,
        item: Substitution,
        matched_event: LectureEvent | None,
    ) -> tuple[str, str]:
        start_time, end_time = parse_time_range(item.time_text, item.period)
        if start_time and end_time:
            return (
                f"{start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')}",
                end_time.strftime("%H:%M"),
            )
        if matched_event and matched_event.start_time and matched_event.end_time:
            return (
                self._format_event_time(matched_event),
                matched_event.end_time.strftime("%H:%M"),
            )
        if item.time_text:
            return item.time_text, end_time.strftime("%H:%M") if end_time else "Not available"
        if matched_event:
            return matched_event.slot_label, "Not available"
        return item.period or "Scheduled lecture", "Not available"

    def _apply_substitution_to_event(
        self,
        event: LectureEvent,
        context: dict[str, LectureEvent | str | None],
    ) -> None:
        subject_name = str(context["subject_name"] or event.subject_name).strip()
        teacher_name = str(context["assigned_teacher"] or event.teacher_name).strip()
        lecture_time_text = str(context["lecture_time_text"] or self._format_event_time(event)).strip()
        end_time_text = str(context["end_time_text"] or "Not available").strip()
        original_teacher = str(context["original_teacher"] or "").strip()

        note_parts = ["Substitute lecture assigned"]
        if original_teacher and original_teacher != teacher_name:
            note_parts.append(f"Original faculty: {original_teacher}")
        if end_time_text and end_time_text != "Not available":
            note_parts.append(f"Ends at: {end_time_text}")
        location_text = self._extract_class_location(event.raw_cell)
        if location_text:
            note_parts.append(f"Class: {location_text}")
        note = " | ".join(note_parts)
        raw_cell = "\n".join(part for part in [subject_name, teacher_name, lecture_time_text] if part)
        subject_key = event.subject_key or normalize_subject_key(subject_name)

        self.db.update_lecture_event_assignment(
            event.id,
            subject_key=subject_key,
            subject_name=subject_name,
            teacher_name=teacher_name,
            raw_cell=raw_cell,
            note=note,
        )
        event.subject_key = subject_key
        event.subject_name = subject_name
        event.teacher_name = teacher_name
        event.raw_cell = raw_cell
        event.note = note

    def _build_substitution_alert_key(
        self,
        target_date: date,
        context: dict[str, LectureEvent | str | None],
    ) -> str:
        parts = [
            target_date.isoformat(),
            self._normalize_text(str(context["period_text"] or "")),
            self._normalize_text(str(context["lecture_time_text"] or "")),
            self._normalize_text(str(context["end_time_text"] or "")),
            normalize_subject_key(str(context["subject_name"] or "")),
            normalize_subject_key(str(context["assigned_teacher"] or "")),
        ]
        return "|".join(parts)

    def _process_attendance_scan(self, student: Student, due_events: list[LectureEvent], now: datetime) -> None:
        payload = self.erp.get_attendance_summary(student)
        attendance = parse_attendance_summary(payload)
        self.db.mark_student_erp_sync(student.id)
        snapshots = self.db.get_attendance_snapshots(student.id)
        baseline_snapshots = {key: dict(value) for key, value in snapshots.items()}
        lecture_update_sent = False
        live_candidate_events = self._live_attendance_candidate_events(student, now, due_events)
        if live_candidate_events:
            lecture_update_sent = self._process_live_attendance_candidates(
                student,
                live_candidate_events,
                now,
                attendance=attendance,
                snapshots=snapshots,
            )
        if due_events:
            lecture_update_sent = self._process_due_events(
                student,
                due_events,
                now,
                attendance=attendance,
                snapshots=snapshots,
            ) or lecture_update_sent
        self._emit_lecture_finish_notifications(
            student,
            self.db.get_lecture_events_for_day(student.id, now.date()),
            now,
            attendance,
        )
        self._detect_attendance_corrections(student, attendance, snapshots, now)
        self._detect_attendance_summary_changes(
            student,
            attendance,
            baseline_snapshots,
            now,
            lecture_events=due_events,
            suppress_notification=lecture_update_sent,
        )
        self._sync_attendance_snapshots(student, attendance)
        self._evaluate_attendance_risk(student, attendance, now, lecture_events=due_events)

    def _live_attendance_candidate_events(
        self,
        student: Student,
        now: datetime,
        due_events: list[LectureEvent],
    ) -> list[LectureEvent]:
        due_event_ids = {event.id for event in due_events}
        now_naive = now.replace(tzinfo=None)
        candidates: list[LectureEvent] = []
        for event in self.db.get_lecture_events_for_day(student.id, now.date()):
            if event.is_break or event.id in due_event_ids or event.status != "scheduled":
                continue
            if self._event_has_started_by(event, now_naive):
                candidates.append(event)
        return candidates

    def _process_live_attendance_candidates(
        self,
        student: Student,
        events: list[LectureEvent],
        now: datetime,
        *,
        attendance,
        snapshots: dict[str, dict],
    ) -> bool:
        if not events:
            return False
        next_check = now.replace(tzinfo=None) + timedelta(
            minutes=self.settings.attendance_poll_interval_minutes
        )
        now_naive = now.replace(tzinfo=None)
        grouped_events: dict[str, dict[str, object]] = {}
        unmatched_events: list[LectureEvent] = []
        sent_attendance_update = False

        for event in sorted(
            events,
            key=lambda item: (
                item.event_date.isoformat(),
                item.start_time.isoformat() if item.start_time else "99:99",
                item.id,
            ),
        ):
            record = match_attendance_record(attendance, event.subject_key, event.subject_name)
            if not record:
                unmatched_events.append(event)
                continue
            key = record.subject_key or event.subject_key
            bucket = grouped_events.setdefault(key, {"record": record, "events": []})
            bucket["events"].append(event)

        for event in unmatched_events:
            if not self._event_has_finished_by(event, now_naive):
                continue
            self._mark_event_pending_update(student, event, now=now, next_check=next_check)

        for bucket in grouped_events.values():
            record = bucket["record"]
            subject_events = list(bucket["events"])
            snapshot = snapshots.get(record.subject_key)
            previous_lecture = int(snapshot["total_lecture"]) if snapshot else 0
            previous_present = int(snapshot["total_present"]) if snapshot else 0
            lecture_delta = max(0, record.total_lecture - previous_lecture)
            present_delta = max(0, record.total_present - previous_present)

            if lecture_delta > 0:
                resolved_count = min(lecture_delta, len(subject_events))
                resolved_present_count = min(present_delta, resolved_count)
                inferred_from_batch = resolved_count > 1
                for index, event in enumerate(subject_events):
                    if index < resolved_count:
                        was_present = index < resolved_present_count
                        final_status = "notified_present" if was_present else "notified_absent"
                        body = self._render_attendance_message(
                            event,
                            was_present,
                            record,
                            detected_at=now,
                            inferred_from_batch=inferred_from_batch,
                            batch_size=resolved_count,
                        )
                        self._send_whatsapp(
                            student,
                            body,
                            message_kind="attendance",
                            history_category="attendance_update",
                            idempotency_key=f"attendance_update:{event.id}:{final_status}",
                        )
                        self.db.mark_event_status(event.id, final_status, status_recorded_at=now)
                        self.db.update_student_status(
                            student.id,
                            (
                                f"Attendance marked {'present' if was_present else 'absent'} for "
                                f"{record.subject_name} at {self._format_datetime(now)}."
                            ),
                        )
                        sent_attendance_update = True
                        continue

                    if not self._event_has_finished_by(event, now_naive):
                        continue
                    self._mark_event_pending_update(student, event, now=now, next_check=next_check)

                self.db.upsert_attendance_snapshot(
                    student_id=student.id,
                    subject_key=record.subject_key,
                    subject_name=record.subject_name,
                    subject_code=record.subject_code,
                    teacher_name=record.teacher_name,
                    total_lecture=record.total_lecture,
                    total_present=record.total_present,
                    percentage=record.percentage,
                )
                snapshots[record.subject_key] = {
                    "total_lecture": record.total_lecture,
                    "total_present": record.total_present,
                }
                continue

            for event in subject_events:
                if not self._event_has_finished_by(event, now_naive):
                    continue
                self._mark_event_pending_update(student, event, now=now, next_check=next_check)
        return sent_attendance_update

    def _mark_event_pending_update(
        self,
        student: Student,
        event: LectureEvent,
        *,
        now: datetime,
        next_check: datetime,
    ) -> None:
        body = self._render_not_marked_message(event)
        if event.status == "scheduled":
            self._send_whatsapp(
                student,
                body,
                message_kind="attendance",
                history_category="attendance_pending",
                idempotency_key=f"attendance_pending:{event.id}",
            )
        self.db.mark_event_status(event.id, "notified_unmarked", next_check_after=next_check)
        if self._event_has_finished_by(event, now.replace(tzinfo=None)):
            self._record_lecture_finish_notification(student, event, body)

    def _emit_lecture_finish_notifications(
        self,
        student: Student,
        events: list[LectureEvent],
        now: datetime,
        attendance,
    ) -> None:
        now_naive = now.replace(tzinfo=None)
        for event in sorted(
            events,
            key=lambda item: (
                item.event_date.isoformat(),
                item.end_time.isoformat() if item.end_time else item.start_time.isoformat() if item.start_time else "99:99",
                item.id,
            ),
        ):
            if event.is_break or not self._event_has_finished_by(event, now_naive):
                continue
            if self._has_lecture_finish_notification(student, event):
                continue
            if self._status_was_recorded_in_current_scan(event, now):
                continue

            if event.status in {"scheduled", "notified_unmarked"}:
                body = self._render_not_marked_message(event)
            else:
                record = match_attendance_record(attendance, event.subject_key, event.subject_name)
                body = self._render_lecture_finished_status_message(
                    event,
                    record=record,
                    detected_at=now,
                )

            self._send_whatsapp(
                student,
                body,
                message_kind="attendance",
                history_category="lecture_finished_status",
                idempotency_key=f"lecture_finished_status:{event.id}",
            )
            self._record_lecture_finish_notification(student, event, body)

    def _has_lecture_finish_notification(self, student: Student, event: LectureEvent) -> bool:
        return self.db.has_notification_event(
            student.id,
            "lecture_finished_status",
            str(event.id),
        )

    def _status_was_recorded_in_current_scan(self, event: LectureEvent, now: datetime) -> bool:
        if not event.status_recorded_at:
            return False
        recorded_at = self._normalize_event_datetime(event.status_recorded_at, now.tzinfo)
        return recorded_at == now.replace(microsecond=0)

    def _record_lecture_finish_notification(self, student: Student, event: LectureEvent, body: str) -> None:
        self.db.upsert_notification_event(
            student_id=student.id,
            category="lecture_finished_status",
            notification_key=str(event.id),
            message_text=body,
        )

    def _sync_attendance_snapshots(self, student: Student, attendance) -> None:
        for record in attendance:
            self.db.upsert_attendance_snapshot(
                student_id=student.id,
                subject_key=record.subject_key,
                subject_name=record.subject_name,
                subject_code=record.subject_code,
                teacher_name=record.teacher_name,
                total_lecture=record.total_lecture,
                total_present=record.total_present,
                percentage=record.percentage,
            )

    def _detect_attendance_summary_changes(
        self,
        student: Student,
        attendance,
        previous_snapshots: dict[str, dict],
        now: datetime,
        *,
        lecture_events: list[LectureEvent] | None = None,
        suppress_notification: bool = False,
    ) -> None:
        if not attendance or not previous_snapshots:
            return

        current_keys = {record.subject_key for record in attendance if record.subject_key}
        if not current_keys or any(key not in previous_snapshots for key in current_keys):
            return

        changed_subjects: list[dict[str, object]] = []
        teacher_events = self._risk_teacher_reference_events(
            student,
            target_date=now.date(),
            lecture_events=lecture_events,
        )
        subject_references = self._build_weekly_subject_reference_map(student, now.date())

        for record in attendance:
            snapshot = previous_snapshots.get(record.subject_key)
            if not snapshot:
                continue
            previous_lecture = max(int(snapshot.get("total_lecture", 0) or 0), 0)
            previous_present = max(int(snapshot.get("total_present", 0) or 0), 0)
            current_lecture = max(int(record.total_lecture), 0)
            current_present = max(int(record.total_present), 0)
            previous_percentage = self._parse_percentage(
                str(snapshot.get("percentage") or ""),
                previous_present,
                previous_lecture,
            )
            current_percentage = self._parse_percentage(
                record.percentage,
                current_present,
                current_lecture,
            )

            if (
                previous_lecture == current_lecture
                and previous_present == current_present
                and abs(previous_percentage - current_percentage) < 0.01
            ):
                continue

            changed_subjects.append(
                {
                    "subject_name": self._format_subject_label(record.subject_name, record.subject_code),
                    "teacher_name": self._resolve_subject_faculty_name(
                        record,
                        subject_references=subject_references,
                        lecture_events=teacher_events,
                    ),
                    "previous_present": previous_present,
                    "previous_lecture": previous_lecture,
                    "previous_percentage": previous_percentage,
                    "current_present": current_present,
                    "current_lecture": current_lecture,
                    "current_percentage": current_percentage,
                }
            )

        previous_present_total = sum(
            max(int(previous_snapshots[key].get("total_present", 0) or 0), 0)
            for key in current_keys
            if key in previous_snapshots
        )
        previous_lecture_total = sum(
            max(int(previous_snapshots[key].get("total_lecture", 0) or 0), 0)
            for key in current_keys
            if key in previous_snapshots
        )
        current_present_total = sum(max(int(record.total_present), 0) for record in attendance)
        current_lecture_total = sum(max(int(record.total_lecture), 0) for record in attendance)

        if (
            not changed_subjects
            and previous_present_total == current_present_total
            and previous_lecture_total == current_lecture_total
        ):
            return

        state_payload = {
            "overall": {
                "previous_present": previous_present_total,
                "previous_lecture": previous_lecture_total,
                "current_present": current_present_total,
                "current_lecture": current_lecture_total,
            },
            "subjects": [
                {
                    "subject_name": str(item["subject_name"]),
                    "previous_present": int(item["previous_present"]),
                    "previous_lecture": int(item["previous_lecture"]),
                    "current_present": int(item["current_present"]),
                    "current_lecture": int(item["current_lecture"]),
                }
                for item in changed_subjects
            ],
        }
        state_key = hashlib.sha256(
            json.dumps(state_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.db.has_notification_event(student.id, "attendance_summary_change", state_key):
            return

        body = self._render_attendance_summary_change_message(
            student=student,
            changed_subjects=changed_subjects,
            detected_at=now,
            previous_present_total=previous_present_total,
            previous_lecture_total=previous_lecture_total,
            current_present_total=current_present_total,
            current_lecture_total=current_lecture_total,
        )
        if suppress_notification:
            self.db.upsert_notification_event(
                student_id=student.id,
                category="attendance_summary_change",
                notification_key=state_key,
                message_text=body,
            )
            return
        self._send_whatsapp(
            student,
            body,
            message_kind="attendance",
            history_category="attendance_summary_change",
            idempotency_key=f"attendance_summary_change:{student.id}:{state_key}",
        )
        self.db.upsert_notification_event(
            student_id=student.id,
            category="attendance_summary_change",
            notification_key=state_key,
            message_text=body,
        )

    def _resolve_attendance_change_teacher_name(
        self,
        record,
        snapshot: dict[str, object],
        lecture_events: list[LectureEvent],
    ) -> str:
        teacher_name = self._resolve_teacher_name_from_events(record, lecture_events)
        if teacher_name and teacher_name != "Not available":
            return teacher_name
        return "Not available"

    def _detect_attendance_corrections(
        self,
        student: Student,
        attendance,
        snapshots: dict[str, dict],
        now: datetime,
    ) -> None:
        lookback_start = now.date() - timedelta(days=self.settings.attendance_correction_lookback_days)
        for record in attendance:
            snapshot = snapshots.get(record.subject_key)
            if not snapshot:
                continue
            previous_lecture = int(snapshot["total_lecture"])
            previous_present = int(snapshot["total_present"])
            current_lecture = record.total_lecture
            current_present = record.total_present

            if current_lecture == previous_lecture and current_present == previous_present:
                continue
            if current_lecture > previous_lecture:
                continue

            matched_event = self.db.get_latest_marked_event_for_subject(
                student_id=student.id,
                subject_key=record.subject_key,
                subject_name=record.subject_name,
                since_date=lookback_start,
            )
            if not matched_event:
                self.db.upsert_attendance_snapshot(
                    student_id=student.id,
                    subject_key=record.subject_key,
                    subject_name=record.subject_name,
                    subject_code=record.subject_code,
                    teacher_name=record.teacher_name,
                    total_lecture=current_lecture,
                    total_present=current_present,
                    percentage=record.percentage,
                )
                continue

            if current_lecture == previous_lecture:
                if current_present > previous_present:
                    corrected_present = True
                elif current_present < previous_present:
                    corrected_present = False
                else:
                    corrected_present = matched_event.status == "notified_present"
            else:
                corrected_present = matched_event.status == "notified_present"

            new_status = "notified_present" if corrected_present else "notified_absent"
            if matched_event.status == new_status and current_lecture == previous_lecture and current_present == previous_present:
                continue

            notification_key = (
                f"{matched_event.id}:{previous_lecture}:{previous_present}:{current_lecture}:{current_present}"
            )
            if self.db.has_notification_event(student.id, "attendance_correction", notification_key):
                continue

            self.db.mark_event_status(
                matched_event.id,
                new_status,
                status_recorded_at=now,
            )
            body = self._render_attendance_correction_message(
                matched_event,
                record,
                detected_at=now,
                previous_lecture=previous_lecture,
                previous_present=previous_present,
                corrected_present=corrected_present,
            )
            self._send_whatsapp(
                student,
                body,
                message_kind="attendance",
                history_category="attendance_correction",
                idempotency_key=f"attendance_correction:{matched_event.id}:{notification_key}",
            )
            self.db.upsert_notification_event(
                student_id=student.id,
                category="attendance_correction",
                notification_key=notification_key,
                message_text=body,
            )
            self.db.upsert_attendance_snapshot(
                student_id=student.id,
                subject_key=record.subject_key,
                subject_name=record.subject_name,
                subject_code=record.subject_code,
                teacher_name=record.teacher_name,
                total_lecture=current_lecture,
                total_present=current_present,
                percentage=record.percentage,
            )
            snapshots[record.subject_key] = {
                "total_lecture": current_lecture,
                "total_present": current_present,
            }

    def _evaluate_attendance_risk(
        self,
        student: Student,
        attendance,
        now: datetime,
        *,
        lecture_events: list[LectureEvent] | None = None,
    ) -> None:
        if not attendance:
            return
        teacher_events = self._risk_teacher_reference_events(
            student,
            target_date=now.date(),
            lecture_events=lecture_events,
        )
        totals_present = sum(record.total_present for record in attendance)
        totals_lecture = sum(record.total_lecture for record in attendance)
        overall_percentage = self._safe_percentage(totals_present, totals_lecture)
        shortage_threshold = self._attendance_shortage_warning_threshold()
        subject_references = self._build_weekly_subject_reference_map(student, now.date())
        shortage_items = self._attendance_shortage_items(
            attendance,
            lecture_events=teacher_events,
            subject_references=subject_references,
        )
        for threshold in self.settings.low_attendance_thresholds:
            if threshold == shortage_threshold:
                continue
            if totals_lecture and overall_percentage < threshold:
                notification_key = f"overall:{threshold}:{totals_present}:{totals_lecture}"
                if not self.db.has_notification_event(student.id, "low_attendance_overall", notification_key):
                    body = self._render_low_attendance_message(
                        subject_name="Overall attendance",
                        teacher_name="All subjects",
                        present=totals_present,
                        total=totals_lecture,
                        percentage=overall_percentage,
                        threshold=threshold,
                        detected_at=now,
                    )
                    self._send_whatsapp(
                        student,
                        body,
                        message_kind="attendance",
                        history_category="low_attendance_alert",
                        idempotency_key=f"low_attendance_overall:{student.id}:{threshold}:{totals_present}:{totals_lecture}",
                    )
                    self.db.upsert_notification_event(
                        student_id=student.id,
                        category="low_attendance_overall",
                        notification_key=notification_key,
                        message_text=body,
                    )

        for record in attendance:
            percentage = self._parse_percentage(record.percentage, record.total_present, record.total_lecture)
            teacher_name = self._resolve_risk_teacher_name(record, teacher_events)
            for threshold in self.settings.low_attendance_thresholds:
                if threshold == shortage_threshold:
                    continue
                if record.total_lecture and percentage < threshold:
                    notification_key = (
                        f"{record.subject_key}:{threshold}:{record.total_present}:{record.total_lecture}"
                    )
                    if not self.db.has_notification_event(student.id, "low_attendance_subject", notification_key):
                        body = self._render_low_attendance_message(
                            subject_name=self._format_subject_label(record.subject_name, record.subject_code),
                            teacher_name=teacher_name,
                            present=record.total_present,
                            total=record.total_lecture,
                            percentage=percentage,
                            threshold=threshold,
                            detected_at=now,
                        )
                        self._send_whatsapp(
                            student,
                            body,
                            message_kind="attendance",
                            history_category="low_attendance_alert",
                            idempotency_key=(
                                f"low_attendance_subject:{student.id}:{record.subject_key}:{threshold}:{record.total_present}:{record.total_lecture}"
                            ),
                        )
                        self.db.upsert_notification_event(
                            student_id=student.id,
                            category="low_attendance_subject",
                            notification_key=notification_key,
                            message_text=body,
                        )
        if not shortage_items:
            return
        notification_key = self._attendance_shortage_notification_key(
            shortage_items,
            threshold=shortage_threshold,
        )
        if self.db.has_notification_event(student.id, "attendance_shortage_report_auto", notification_key):
            return
        body = "\n".join(
            self._shortage_report_lines(
                student_name=student.student_name or student.student_label,
                shortage_items=shortage_items,
                generated_at=now,
                threshold=shortage_threshold,
            )
        )
        self._send_whatsapp(
            student,
            body,
            message_kind="attendance",
            history_category="attendance_shortage_report",
            idempotency_key=f"attendance_shortage_report_auto:{student.id}:{notification_key}",
        )
        self.db.upsert_notification_event(
            student_id=student.id,
            category="attendance_shortage_report_auto",
            notification_key=notification_key,
            message_text=body,
        )

    def _process_due_events(
        self,
        student: Student,
        events: list[LectureEvent],
        now: datetime,
        *,
        attendance=None,
        snapshots: dict[str, dict] | None = None,
    ) -> bool:
        if attendance is None:
            payload = self.erp.get_attendance_summary(student)
            attendance = parse_attendance_summary(payload)
        if snapshots is None:
            snapshots = self.db.get_attendance_snapshots(student.id)
        next_check = now.replace(tzinfo=None) + timedelta(
            minutes=self.settings.attendance_poll_interval_minutes
        )
        grouped_events: dict[str, dict[str, object]] = {}
        unmatched_events: list[LectureEvent] = []
        sent_attendance_update = False

        for event in sorted(
            events,
            key=lambda item: (
                item.event_date.isoformat(),
                item.start_time.isoformat() if item.start_time else "99:99",
                item.id,
            ),
        ):
            record = match_attendance_record(attendance, event.subject_key, event.subject_name)
            if not record:
                unmatched_events.append(event)
                continue
            key = record.subject_key or event.subject_key
            bucket = grouped_events.setdefault(key, {"record": record, "events": []})
            bucket["events"].append(event)

        for event in unmatched_events:
            self.db.mark_event_status(event.id, event.status, next_check_after=next_check)

        for bucket in grouped_events.values():
            record = bucket["record"]
            subject_events = list(bucket["events"])
            snapshot = snapshots.get(record.subject_key)
            previous_lecture = int(snapshot["total_lecture"]) if snapshot else 0
            previous_present = int(snapshot["total_present"]) if snapshot else 0
            lecture_delta = max(0, record.total_lecture - previous_lecture)
            present_delta = max(0, record.total_present - previous_present)

            if lecture_delta > 0:
                resolved_count = min(lecture_delta, len(subject_events))
                resolved_present_count = min(present_delta, resolved_count)
                inferred_from_batch = resolved_count > 1
                for index, event in enumerate(subject_events):
                    if index < resolved_count:
                        was_present = index < resolved_present_count
                        final_status = "notified_present" if was_present else "notified_absent"
                        body = self._render_attendance_message(
                            event,
                            was_present,
                            record,
                            detected_at=now,
                            inferred_from_batch=inferred_from_batch,
                            batch_size=resolved_count,
                        )
                        self._send_whatsapp(
                            student,
                            body,
                            message_kind="attendance",
                            history_category="attendance_update",
                            idempotency_key=f"attendance_update:{event.id}:{final_status}",
                        )
                        self.db.mark_event_status(event.id, final_status, status_recorded_at=now)
                        self.db.update_student_status(
                            student.id,
                            (
                                f"Attendance marked {'present' if was_present else 'absent'} for "
                                f"{record.subject_name} at {self._format_datetime(now)}."
                            ),
                        )
                        sent_attendance_update = True
                        continue

                    self._mark_event_pending_update(student, event, now=now, next_check=next_check)

                self.db.upsert_attendance_snapshot(
                    student_id=student.id,
                    subject_key=record.subject_key,
                    subject_name=record.subject_name,
                    subject_code=record.subject_code,
                    teacher_name=record.teacher_name,
                    total_lecture=record.total_lecture,
                    total_present=record.total_present,
                    percentage=record.percentage,
                )
                snapshots[record.subject_key] = {
                    "total_lecture": record.total_lecture,
                    "total_present": record.total_present,
                }
                continue

            for event in subject_events:
                self._mark_event_pending_update(student, event, now=now, next_check=next_check)
        return sent_attendance_update

    def _send_evening_report_if_due(
        self,
        student: Student,
        target_date: date,
        *,
        now: datetime,
        force: bool = False,
    ) -> str | None:
        if self.db.get_daily_attendance_report(student.id, target_date) and not force:
            return None

        events = self.db.get_lecture_events_for_day(student.id, target_date)
        lecture_events = [event for event in events if not event.is_break]
        if not lecture_events:
            if force:
                raise ERPClientError(
                    "No lecture routine was synced for this date. "
                    "Send the morning summary first so the bot can track lecture-wise attendance."
                )
            return None

        if not force and not self._is_evening_report_due(lecture_events, target_date, now):
            return None

        due_events = self._events_due_now(lecture_events, now)
        if due_events:
            self._process_due_events(student, due_events, now)
            events = self.db.get_lecture_events_for_day(student.id, target_date)
            lecture_events = [event for event in events if not event.is_break]

        report = self._build_evening_report(
            student_name=student.student_name or student.student_label,
            target_date=target_date,
            events=lecture_events,
            generated_at=now,
        )
        self._send_whatsapp(
            student,
            report["body"],
            message_kind="attendance",
            history_category="daily_report",
            idempotency_key=self._report_idempotency_key(
                base_key=f"daily_report:{student.id}:{target_date.isoformat()}",
                force=force,
                now=now,
            ),
        )
        self.db.upsert_daily_attendance_report(
            student_id=student.id,
            event_date=target_date,
            total_lectures=report["total_lectures"],
            marked_count=report["marked_count"],
            present_count=report["present_count"],
            absent_count=report["absent_count"],
            unmarked_count=report["unmarked_count"],
            report_body=report["body"],
        )
        self.db.update_student_status(student.id, f"End-of-day attendance report sent for {target_date.isoformat()}.")
        return report["body"]

    def _is_evening_report_due(
        self,
        events: list[LectureEvent],
        target_date: date,
        now: datetime,
    ) -> bool:
        now_naive = now.replace(tzinfo=None)
        check_times = [event.check_after for event in events if event.check_after]
        if check_times:
            return now_naive >= max(check_times)
        return now_naive >= datetime.combine(target_date, self._parse_clock(self.settings.evening_report_time))

    def _events_due_now(self, events: list[LectureEvent], now: datetime) -> list[LectureEvent]:
        now_naive = now.replace(tzinfo=None)
        return [
            event
            for event in events
            if event.check_after
            and event.check_after <= now_naive
            and event.status in {"scheduled", "notified_unmarked"}
        ]

    def _build_evening_report(
        self,
        *,
        student_name: str,
        target_date: date,
        events: list[LectureEvent],
        generated_at: datetime,
    ) -> dict[str, int | str]:
        present_count = 0
        absent_count = 0
        unmarked_count = 0
        for event in events:
            _, marked, is_present = self._attendance_state_for_event(event)
            if marked:
                if is_present:
                    present_count += 1
                else:
                    absent_count += 1
            else:
                unmarked_count += 1

        total_lectures = len(events)
        marked_count = present_count + absent_count
        lines = [
            "End-of-Day Attendance Report",
            "",
            f"Student: {student_name or 'student'}",
            f"Report date: {target_date.strftime('%A, %d %B %Y')}",
            f"Generated at: {self._format_datetime(generated_at)}",
            "",
            "Summary",
            f"Scheduled lectures: {total_lectures}",
            f"Attendance marked: {marked_count}",
            f"Present: {present_count}",
            f"Absent: {absent_count}",
            f"Not marked yet: {unmarked_count}",
            "",
            "Lecture-wise Status",
        ]

        for event in events:
            status_text, _, _ = self._attendance_state_for_event(event)
            marked_at_text = ""
            if event.status_recorded_at:
                marked_at_dt = self._normalize_event_datetime(event.status_recorded_at, generated_at.tzinfo)
                marked_at_text = f" | Marked at: {self._format_datetime(marked_at_dt)}"
            teacher_text = f" | Faculty: {event.teacher_name}" if event.teacher_name else ""
            location_text = self._event_class_location(event)
            class_text = f" | Class: {location_text}" if location_text else ""
            note_text = f" | Note: {event.note}" if event.note else ""
            lines.append(
                f"- {self._format_event_time(event)} | {event.subject_name} | {status_text}{marked_at_text}{teacher_text}{class_text}{note_text}"
            )

        if unmarked_count:
            lines.extend(
                [
                    "",
                    "Pending lecture entries will continue to be checked automatically.",
                    "If attendance is marked later, the bot will send a lecture-wise update with the original lecture date and time.",
                ]
            )

        return {
            "body": "\n".join(lines),
            "total_lectures": total_lectures,
            "marked_count": marked_count,
            "present_count": present_count,
            "absent_count": absent_count,
            "unmarked_count": unmarked_count,
        }

    def _parse_percentage(self, text: str, present: int, total: int) -> float:
        cleaned = (text or "").strip().replace("%", "")
        if cleaned:
            try:
                return float(cleaned)
            except ValueError:
                pass
        return self._safe_percentage(present, total)

    def _safe_percentage(self, numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round((numerator / denominator) * 100, 2)

    def _attendance_shortage_warning_threshold(self) -> int:
        thresholds = tuple(value for value in self.settings.low_attendance_thresholds if value > 0)
        if 75 in thresholds:
            return 75
        if thresholds:
            return max(thresholds)
        return 75

    def _risk_teacher_reference_events(
        self,
        student: Student,
        *,
        target_date: date,
        lecture_events: list[LectureEvent] | None = None,
    ) -> list[LectureEvent]:
        reference_events = [event for event in (lecture_events or []) if not event.is_break]
        recent_events = [
            event
            for event in self.db.get_lecture_events_between(
                student.id,
                target_date - timedelta(days=30),
                target_date,
            )
            if not event.is_break
        ]
        seen_ids = {event.id for event in reference_events}
        for event in recent_events:
            if event.id in seen_ids:
                continue
            reference_events.append(event)
        return reference_events

    def _build_weekly_subject_reference_map(
        self,
        student: Student,
        target_date: date,
    ) -> dict[str, dict[str, str]]:
        try:
            timetable_payload = self.erp.get_timetable(student)
        except ERPClientError:
            return {}

        references: dict[str, dict[str, str]] = {}
        week_start = target_date - timedelta(days=target_date.weekday())
        for offset in range(7):
            reference_date = week_start + timedelta(days=offset)
            for slot in parse_timetable_slots(timetable_payload, reference_date):
                if slot.is_break:
                    continue
                entries = self._parse_subject_reference_entries(
                    slot.raw_cell,
                    fallback_subject_name=slot.subject_name,
                    fallback_teacher_name=slot.teacher_name,
                )
                for entry in entries:
                    self._merge_subject_reference(references, entry)
        return references

    def _parse_subject_reference_entries(
        self,
        raw_cell: str,
        *,
        fallback_subject_name: str = "",
        fallback_teacher_name: str = "",
    ) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        lines = html_to_lines(raw_cell)
        if not lines and raw_cell:
            lines = [str(raw_cell).strip()]

        parts: list[str] = []
        for line in lines:
            parts.extend(
                part.strip()
                for part in re.split(r"\s*-\s*(?=[A-Za-z])", line)
                if part.strip()
            )

        if not parts and fallback_subject_name:
            parts = [fallback_subject_name]

        for part in parts:
            teacher_name = ""
            subject_text = part.strip()
            if "," in subject_text:
                subject_text, teacher_name = subject_text.rsplit(",", 1)
                teacher_name = teacher_name.strip()

            subject_code = self._extract_subject_code(subject_text)
            subject_name = self._clean_timetable_subject_text(subject_text)
            if not subject_name:
                continue
            entries.append(
                {
                    "subject_name": subject_name,
                    "subject_code": subject_code,
                    "teacher_name": teacher_name or fallback_teacher_name,
                    "location": self._extract_class_location(subject_text),
                }
            )

        if entries:
            return entries

        cleaned_subject = self._clean_timetable_subject_text(fallback_subject_name)
        if not cleaned_subject:
            return []
        return [
            {
                "subject_name": cleaned_subject,
                "subject_code": self._extract_subject_code(raw_cell or fallback_subject_name),
                "teacher_name": fallback_teacher_name,
                "location": self._extract_class_location(raw_cell),
            }
        ]

    def _merge_subject_reference(
        self,
        references: dict[str, dict[str, str]],
        entry: dict[str, str],
    ) -> None:
        keys = {normalize_subject_key(str(entry.get("subject_name") or ""))}
        subject_code = str(entry.get("subject_code") or "").strip()
        if subject_code:
            keys.add(normalize_subject_key(subject_code))

        teacher_name = str(entry.get("teacher_name") or "").strip()
        location = str(entry.get("location") or "").strip()
        subject_name = str(entry.get("subject_name") or "").strip()

        for key in keys:
            if not key or key == "unknown_subject":
                continue
            existing = references.get(key)
            if not existing:
                references[key] = {
                    "subject_name": subject_name,
                    "subject_code": subject_code,
                    "teacher_name": teacher_name,
                    "location": location,
                }
                continue

            if not existing.get("teacher_name") and teacher_name:
                existing["teacher_name"] = teacher_name
            if not existing.get("location") and location:
                existing["location"] = location
            if not existing.get("subject_code") and subject_code:
                existing["subject_code"] = subject_code
            if not existing.get("subject_name") and subject_name:
                existing["subject_name"] = subject_name

    def _extract_subject_code(self, value: str) -> str:
        match = re.search(r"\(([A-Z]{2,}\d+[A-Z0-9]*)\)", value or "")
        return match.group(1).strip() if match else ""

    def _clean_timetable_subject_text(self, value: str) -> str:
        subject = " ".join(str(value or "").split())
        subject_code = self._extract_subject_code(subject)
        if subject_code:
            subject = re.sub(rf"\s*\({re.escape(subject_code)}\)", "", subject, count=1).strip()

        while True:
            updated = re.sub(r"\s*\(([A-Za-z0-9_/-]+(?:-[A-Za-z0-9_/-]+)*)\)\s*$", "", subject).strip()
            if updated == subject:
                break
            subject = updated
        return subject

    def _extract_class_location(self, value: str) -> str:
        locations: list[str] = []
        for match in re.findall(r"\(([^()]+)\)", value or ""):
            candidate = " ".join(match.split()).strip()
            if not candidate:
                continue
            if re.fullmatch(r"[A-Z]{2,}\d+[A-Z0-9]*", candidate):
                continue
            if re.fullmatch(r"[A-Z]", candidate):
                continue
            if " " in candidate:
                continue
            if not any(char.isdigit() for char in candidate) and "_" not in candidate and "-" not in candidate:
                continue
            if candidate not in locations:
                locations.append(candidate)
        return " / ".join(locations)

    def _find_subject_reference(
        self,
        record,
        subject_references: dict[str, dict[str, str]] | None,
    ) -> dict[str, str] | None:
        if not subject_references:
            return None
        for candidate in (
            getattr(record, "subject_code", ""),
            getattr(record, "subject_key", ""),
            getattr(record, "subject_name", ""),
        ):
            key = normalize_subject_key(str(candidate or ""))
            if not key or key == "unknown_subject":
                continue
            reference = subject_references.get(key)
            if reference:
                return reference
        return None

    def _resolve_teacher_name_from_events(self, record, lecture_events: list[LectureEvent]) -> str:
        for event in reversed(lecture_events):
            if not event.teacher_name:
                continue
            if match_attendance_record([record], event.subject_key, event.subject_name):
                return event.teacher_name
        return "Not available"

    def _resolve_subject_faculty_name(
        self,
        record,
        *,
        subject_references: dict[str, dict[str, str]] | None,
        lecture_events: list[LectureEvent],
    ) -> str:
        reference = self._find_subject_reference(record, subject_references)
        if reference:
            teacher_name = str(reference.get("teacher_name") or "").strip()
            if teacher_name:
                return teacher_name

        event_teacher = self._resolve_teacher_name_from_events(record, lecture_events)
        if event_teacher and event_teacher != "Not available":
            return event_teacher
        return str(getattr(record, "teacher_name", "") or "").strip() or "Not available"

    def _resolve_subject_class_location(
        self,
        record,
        *,
        subject_references: dict[str, dict[str, str]] | None,
        lecture_events: list[LectureEvent],
    ) -> str:
        reference = self._find_subject_reference(record, subject_references)
        if reference:
            location = str(reference.get("location") or "").strip()
            if location:
                return location

        for event in reversed(lecture_events):
            if match_attendance_record([record], event.subject_key, event.subject_name):
                return self._event_class_location(event)
        return ""

    def _resolve_risk_teacher_name(self, record, lecture_events: list[LectureEvent]) -> str:
        event_teacher = self._resolve_teacher_name_from_events(record, lecture_events)
        if event_teacher and event_teacher != "Not available":
            return event_teacher
        return record.teacher_name or "Not available"

    def _build_shortage_report(
        self,
        *,
        student_name: str,
        attendance,
        lecture_events: list[LectureEvent],
        subject_references: dict[str, dict[str, str]] | None,
        generated_at: datetime,
    ) -> str:
        threshold = self._attendance_shortage_warning_threshold()
        shortage_items = self._attendance_shortage_items(
            attendance,
            lecture_events=lecture_events,
            subject_references=subject_references,
        )
        return "\n".join(
            self._shortage_report_lines(
                student_name=student_name,
                shortage_items=shortage_items,
                generated_at=generated_at,
                threshold=threshold,
            )
        )

    def _attendance_shortage_items(
        self,
        attendance,
        *,
        lecture_events: list[LectureEvent],
        subject_references: dict[str, dict[str, str]] | None,
    ) -> list[dict[str, object]]:
        threshold = self._attendance_shortage_warning_threshold()
        shortage_items: list[dict[str, object]] = []
        for record in attendance:
            percentage = self._parse_percentage(record.percentage, record.total_present, record.total_lecture)
            if percentage > threshold:
                continue
            shortage_items.append(
                {
                    "subject_key": getattr(record, "subject_key", ""),
                    "subject_name": self._format_subject_label(record.subject_name, record.subject_code),
                    "teacher_name": self._resolve_subject_faculty_name(
                        record,
                        subject_references=subject_references,
                        lecture_events=lecture_events,
                    ),
                    "present": record.total_present,
                    "total": record.total_lecture,
                    "percentage": percentage,
                    "remaining_absences": self._absences_remaining_before_threshold(
                        record.total_present,
                        record.total_lecture,
                        threshold,
                    ),
                }
            )
        shortage_items.sort(key=lambda item: (item["percentage"], str(item["subject_name"]).lower()))
        return shortage_items

    def _build_attendance_summary_report(
        self,
        *,
        student_name: str,
        attendance,
        lecture_events: list[LectureEvent],
        subject_references: dict[str, dict[str, str]] | None,
        generated_at: datetime,
    ) -> str:
        if not attendance:
            return "\n".join(
                [
                    "Attendance Summary Report",
                    "",
                    f"Student: {student_name or 'student'}",
                    f"Generated at: {self._format_datetime(generated_at)}",
                    "",
                    "No attendance records are available right now.",
                ]
            )

        total_present = sum(max(int(item.total_present), 0) for item in attendance)
        total_lecture = sum(max(int(item.total_lecture), 0) for item in attendance)
        total_absent = max(total_lecture - total_present, 0)
        shortage_threshold = self._attendance_shortage_warning_threshold()
        shortage_items = self._attendance_shortage_items(
            attendance,
            lecture_events=lecture_events,
            subject_references=subject_references,
        )
        lines = [
            "Attendance Summary Report",
            "",
            f"Student: {student_name or 'student'}",
            f"Generated at: {self._format_datetime(generated_at)}",
            f"Totals present: {total_present}",
            f"Total lectures: {total_lecture}",
            f"Total absent: {total_absent}",
            (
                f"Overall attendance: {total_present}/{total_lecture} "
                f"({self._safe_percentage(total_present, total_lecture):.2f}%)"
            ),
            "",
        ]
        lines.extend(
            self._shortage_report_lines(
                student_name=student_name,
                shortage_items=shortage_items,
                generated_at=generated_at,
                threshold=shortage_threshold,
            )
        )
        lines.extend(
            [
                "",
            "Subject-wise Attendance",
            ]
        )
        sorted_attendance = sorted(
            attendance,
            key=lambda item: (
                self._format_subject_label(item.subject_name, item.subject_code).lower(),
                item.subject_code.lower(),
            ),
        )
        for item in sorted_attendance:
            present = max(int(item.total_present), 0)
            total = max(int(item.total_lecture), 0)
            absent = max(total - present, 0)
            percentage = self._parse_percentage(item.percentage, present, total)
            teacher_name = self._resolve_subject_faculty_name(
                item,
                subject_references=subject_references,
                lecture_events=lecture_events,
            )
            lines.append(
                f"- {self._format_subject_label(item.subject_name, item.subject_code)} | "
                f"Faculty: {teacher_name} | "
                f"Percentage: {percentage:.2f}% | "
                f"Total lectures: {total} | "
                f"Present: {present} | "
                f"Absent: {absent}"
            )
        return "\n".join(lines)

    def _absences_remaining_before_threshold(self, present: int, total: int, threshold: int) -> int:
        if total <= 0 or threshold <= 0:
            return 0
        threshold_ratio = threshold / 100.0
        if threshold_ratio <= 0:
            return 0
        safe_absences = int((present / threshold_ratio) - total)
        return max(safe_absences, 0)

    def _attendance_shortage_notification_key(
        self,
        shortage_items: list[dict[str, object]],
        *,
        threshold: int,
    ) -> str:
        payload = {
            "threshold": threshold,
            "items": [
                {
                    "subject_key": str(item.get("subject_key") or ""),
                    "present": int(item["present"]),
                    "total": int(item["total"]),
                }
                for item in shortage_items
            ],
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _shortage_report_lines(
        self,
        *,
        student_name: str,
        shortage_items: list[dict[str, object]],
        generated_at: datetime,
        threshold: int,
    ) -> list[str]:
        totals_present = sum(max(int(item["present"]), 0) for item in shortage_items)
        totals_lecture = sum(max(int(item["total"]), 0) for item in shortage_items)
        total_absent = max(totals_lecture - totals_present, 0)
        lines = [
            "Attendance Shortage Report",
            "",
            f"Student: {student_name or 'student'}",
            f"Generated at: {self._format_datetime(generated_at)}",
            f"Threshold: {threshold}% per subject",
            "Scope: totals below include only subjects currently at risk.",
            f"Totals present: {totals_present}",
            f"Total lectures: {totals_lecture}",
            f"Total absent: {total_absent}",
            "",
        ]
        if not shortage_items:
            lines.extend(
                [
                    "Status: Clear",
                    "No subject is currently at or below the attendance shortage threshold.",
                ]
            )
            return lines

        lines.append("Subjects At Risk")
        for item in shortage_items:
            remaining_absences = int(item["remaining_absences"])
            risk_text = (
                "No more safe absences remain."
                if remaining_absences <= 0
                else f"{remaining_absences} safe absence(s) remain before further risk."
            )
            lines.append(
                f"- {item['subject_name']} | Faculty: {item['teacher_name']} | "
                f"Attendance: {int(item['present'])}/{int(item['total'])} ({float(item['percentage']):.2f}%) | "
                f"Risk: {risk_text}"
            )
        return lines

    def _report_idempotency_key(self, *, base_key: str, force: bool, now: datetime) -> str:
        if not force:
            return base_key
        return f"{base_key}:manual:{now.isoformat()}"

    def _render_morning_message(
        self,
        *,
        student_name: str,
        target_date: date,
        substitutions,
        lecture_events: list[LectureEvent],
    ) -> str:
        display_events = sorted(
            lecture_events,
            key=lambda event: (
                event.start_time.isoformat() if event.start_time else "99:99",
                event.slot_label,
            ),
        )
        if display_events and not substitutions and all(self._is_no_class_event(event) for event in display_events):
            return self._render_no_class_day_message(
                student_name=student_name,
                target_date=target_date,
                events=display_events,
            )

        lines = [
            "Morning Schedule Update",
            "",
            f"Student: {student_name or 'student'}",
            f"Date: {target_date.strftime('%A, %d %B %Y')}",
            "",
            "Today's Lectures",
        ]

        if not display_events:
            lines.append("- No timetable rows were found for today.")
        else:
            for event in display_events:
                time_text = self._format_event_time(event)
                if event.is_break:
                    if self._is_no_class_event(event):
                        note_text = f" | Note: {event.note}" if event.note and event.note != event.subject_name else ""
                        lines.append(f"- {time_text}: {event.subject_name}{note_text}")
                    else:
                        lines.append(f"- {time_text}: Break")
                    continue
                teacher_text = f" | Faculty: {event.teacher_name}" if event.teacher_name else ""
                location_text = self._event_class_location(event)
                class_text = f" | Class: {location_text}" if location_text else ""
                note_text = f" | Note: {event.note}" if event.note else ""
                lines.append(f"- {time_text}: {event.subject_name}{teacher_text}{class_text}{note_text}")

        lines.extend(["", "Substitute Lectures"])
        if not substitutions:
            lines.append("- No substitute lecture is assigned right now.")
        else:
            for item in substitutions:
                context = self._build_substitution_context(item, lecture_events)
                lines.append(self._render_substitution_summary_line(context))

        lines.extend(
            [
                "",
                "Attendance updates will be checked after each lecture.",
                "New substitute-teacher assignments will be checked automatically and sent immediately when detected.",
                "If the ERP session expires, open the dashboard and enter a fresh captcha.",
            ]
        )
        return "\n".join(lines)

    def _render_no_class_day_message(
        self,
        *,
        student_name: str,
        target_date: date,
        events: list[LectureEvent],
    ) -> str:
        primary_event = events[0] if events else None
        reason = primary_event.subject_name if primary_event else "No Class"
        warm_line = "Enjoy the day and take a proper break."
        if self._normalize_text(reason) == "off day" and target_date.weekday() == 6:
            warm_line = "Happy Sunday. Enjoy the day and take some proper rest."
        elif self._normalize_text(reason) == "holiday":
            warm_line = "Warm wishes for the holiday. Enjoy the day and take some rest."

        lines = [
            "No-Class Day Update",
            "",
            f"Student: {student_name or 'student'}",
            f"Date: {target_date.strftime('%A, %d %B %Y')}",
            f"Status: {reason}",
            "There are no scheduled classes for today.",
            warm_line,
        ]
        if primary_event and primary_event.note and primary_event.note != primary_event.subject_name:
            lines.append(f"Note: {primary_event.note}")
        return "\n".join(lines)

    def _render_substitution_summary_line(
        self,
        context: dict[str, LectureEvent | str | None],
    ) -> str:
        subject_text = self._format_subject_label(
            str(context["subject_name"] or "Updated class"),
            "",
        )
        faculty_text = str(context["assigned_teacher"] or "Not available")
        period_text = str(context["period_text"] or "Scheduled lecture")
        lecture_time_text = str(context["lecture_time_text"] or "Not available")
        end_time_text = str(context["end_time_text"] or "Not available")
        location_text = str(context.get("location_text") or "").strip()
        return (
            f"- {period_text} | Subject: {subject_text} | Faculty: {faculty_text} | "
            f"Time: {lecture_time_text} | Ends at: {end_time_text}"
            f"{' | Class: ' + location_text if location_text else ''}"
        )

    def _render_substitution_alert(
        self,
        target_date: date,
        context: dict[str, LectureEvent | str | None],
        *,
        detected_at: datetime,
    ) -> str:
        subject_text = self._format_subject_label(
            str(context["subject_name"] or "Updated class"),
            "",
        )
        assigned_teacher = str(context["assigned_teacher"] or "Not available")
        original_teacher = str(context["original_teacher"] or "").strip()
        lecture_time_text = str(context["lecture_time_text"] or "Not available")
        end_time_text = str(context["end_time_text"] or "Not available")
        period_text = str(context["period_text"] or "Scheduled lecture")
        location_text = str(context.get("location_text") or "").strip()

        lines = [
            "Substitute Lecture Alert",
            "",
            f"Subject: {subject_text}",
            f"Assigned faculty: {assigned_teacher}",
            f"Lecture date: {target_date.strftime('%A, %d %B %Y')}",
            f"Lecture slot: {period_text}",
            f"Lecture time: {lecture_time_text}",
            f"Lecture ends at: {end_time_text}",
            *([f"Class: {location_text}"] if location_text else []),
            f"Detected at: {self._format_datetime(detected_at)}",
        ]
        if original_teacher and original_teacher != assigned_teacher:
            lines.append(f"Original faculty: {original_teacher}")
        lines.extend(
            [
                "",
                "A substitute lecture has been assigned for this class.",
            ]
        )
        return "\n".join(lines)

    def _render_sandbox_expiry_reminder(
        self,
        *,
        student: Student,
        channel_status: WhatsAppChannelStatus,
        expires_at: datetime,
        detected_at: datetime,
        remaining: timedelta,
    ) -> str:
        join_text = channel_status.join_command or "join <code>"
        lines = [
            "Twilio Sandbox Expiry Reminder",
            "",
            f"Student: {student.student_name or student.student_label}",
            f"Recipient: {student.whatsapp_number}",
            f"Detected at: {self._format_datetime(detected_at)}",
            f"Sandbox expires at: {self._format_datetime(expires_at)}",
            f"Time remaining: {self._format_remaining_time(remaining)}",
            "",
            "Action required:",
            "The join message must be sent manually from the recipient's WhatsApp.",
            f"Current join command: {join_text}",
            "If the sandbox expires, send the current join command again to whatsapp:+14155238886.",
        ]
        if not channel_status.join_command:
            lines.append("Note: Set TWILIO_SANDBOX_JOIN_CODE in .env if you want the exact join command shown here.")
        return "\n".join(lines)

    def _render_erp_session_expired_alert(
        self,
        student: Student,
        *,
        detected_at: datetime,
    ) -> str:
        return "\n".join(
            [
                "ERP Session Alert",
                "",
                f"Student: {student.student_name or student.student_label}",
                f"ERP user id: {student.user_name}",
                f"Detected at: {self._format_datetime(detected_at)}",
                "Status: ERP session expired",
                "",
                "Impact:",
                "Lecture-end attendance scans and summary checks are paused until ERP login is restored.",
                "",
                "Action required:",
                "Open the dashboard and complete ERP login again with a fresh captcha.",
            ]
        )

    def _render_low_attendance_message(
        self,
        *,
        subject_name: str,
        teacher_name: str,
        present: int,
        total: int,
        percentage: float,
        threshold: int,
        detected_at: datetime,
    ) -> str:
        return "\n".join(
            [
                "Low Attendance Alert",
                "",
                f"Subject: {subject_name}",
                f"Faculty: {teacher_name}",
                f"Detected at: {self._format_datetime(detected_at)}",
                f"Current attendance: {present}/{total} ({percentage:.2f}%)",
                f"Threshold breached: {threshold}%",
                "",
                "Action: Review the attendance shortfall and plan the remaining lectures carefully.",
            ]
        )

    def _render_attendance_summary_change_message(
        self,
        *,
        student: Student,
        changed_subjects: list[dict[str, object]],
        detected_at: datetime,
        previous_present_total: int,
        previous_lecture_total: int,
        current_present_total: int,
        current_lecture_total: int,
    ) -> str:
        previous_absent_total = max(previous_lecture_total - previous_present_total, 0)
        current_absent_total = max(current_lecture_total - current_present_total, 0)
        previous_percentage = self._safe_percentage(previous_present_total, previous_lecture_total)
        current_percentage = self._safe_percentage(current_present_total, current_lecture_total)

        lines = [
            "Attendance Summary Change Update",
            "",
            f"Student: {student.student_name or student.student_label}",
            f"Detected at: {self._format_datetime(detected_at)}",
            "",
            "Overall attendance",
            (
                f"- Previous: {previous_present_total}/{previous_lecture_total} "
                f"({previous_percentage:.2f}%) | Absent: {previous_absent_total}"
            ),
            (
                f"- Current: {current_present_total}/{current_lecture_total} "
                f"({current_percentage:.2f}%) | Absent: {current_absent_total}"
            ),
            (
                f"- Change: present {current_present_total - previous_present_total:+d}, "
                f"lectures {current_lecture_total - previous_lecture_total:+d}, "
                f"absent {current_absent_total - previous_absent_total:+d}"
            ),
        ]

        if changed_subjects:
            lines.extend(["", "Subject changes"])
            for item in sorted(changed_subjects, key=lambda value: str(value["subject_name"]).lower()):
                previous_absent = max(int(item["previous_lecture"]) - int(item["previous_present"]), 0)
                current_absent = max(int(item["current_lecture"]) - int(item["current_present"]), 0)
                lines.extend(
                    [
                        f"- {item['subject_name']}",
                        f"  Faculty: {item['teacher_name']}",
                        (
                            f"  Previous: {int(item['previous_present'])}/{int(item['previous_lecture'])} "
                            f"({float(item['previous_percentage']):.2f}%) | Absent: {previous_absent}"
                        ),
                        (
                            f"  Current: {int(item['current_present'])}/{int(item['current_lecture'])} "
                            f"({float(item['current_percentage']):.2f}%) | Absent: {current_absent}"
                        ),
                        (
                            f"  Change: present {int(item['current_present']) - int(item['previous_present']):+d}, "
                            f"lectures {int(item['current_lecture']) - int(item['previous_lecture']):+d}, "
                            f"absent {current_absent - previous_absent:+d}"
                        ),
                    ]
                )

        return "\n".join(lines)

    def _render_shortage_warning_message(
        self,
        *,
        subject_name: str,
        teacher_name: str,
        present: int,
        total: int,
        percentage: float,
        threshold: int,
        remaining_absences: int,
        detected_at: datetime,
    ) -> str:
        buffer_text = "No more safe absences remain." if remaining_absences <= 0 else f"Only {remaining_absences} safe absence(s) remain."
        absent = max(total - present, 0)
        return "\n".join(
            [
                "Attendance Shortage Warning",
                "",
                f"Subject: {subject_name}",
                f"Faculty: {teacher_name}",
                f"Detected at: {self._format_datetime(detected_at)}",
                f"Current attendance: {present}/{total} ({percentage:.2f}%)",
                f"Total absent: {absent}",
                f"Threshold monitored: {threshold}%",
                f"Risk level: {buffer_text}",
                "",
                "Action: Another absence can push attendance below the required threshold.",
            ]
        )

    def _render_attendance_correction_message(
        self,
        event: LectureEvent,
        record,
        *,
        detected_at: datetime,
        previous_lecture: int,
        previous_present: int,
        corrected_present: bool,
    ) -> str:
        corrected_status = "Present" if corrected_present else "Absent"
        faculty_text = self._attendance_display_teacher_name(event, record)
        marking_teacher = self._attendance_marking_teacher_name(record)
        lines = [
            "Attendance Correction Alert",
            "",
            f"Subject: {self._format_subject_label(event.subject_name, getattr(record, 'subject_code', ''))}",
            f"Faculty: {faculty_text}",
            f"Lecture date: {event.event_date.strftime('%A, %d %B %Y')}",
            f"Lecture time: {self._format_event_time(event)}",
            f"Attendance marked time: {self._format_datetime(detected_at)}",
            f"Final status: {corrected_status}",
            (
                "Previous cumulative attendance: "
                f"{previous_present}/{previous_lecture}"
            ),
            f"Updated cumulative attendance: {record.total_present}/{record.total_lecture}",
        ]
        if marking_teacher:
            lines.append(f"Attendance marked by: {marking_teacher}")
        lines.extend(self._attendance_context_lines(event, detected_at=detected_at, record=record))
        lines.extend(
            [
                "",
                "The ERP revised this lecture after the original attendance alert.",
            ]
        )
        return "\n".join(lines)

    def _render_attendance_message(
        self,
        event: LectureEvent,
        was_present: bool,
        record,
        *,
        detected_at: datetime,
        inferred_from_batch: bool = False,
        batch_size: int = 1,
    ) -> str:
        status = "Present" if was_present else "Absent"
        percentage_text = f" ({record.percentage})" if record.percentage else ""
        faculty_text = self._attendance_display_teacher_name(event, record)
        marking_teacher = self._attendance_marking_teacher_name(record)
        subject_text = self._format_subject_label(event.subject_name, getattr(record, "subject_code", ""))
        update_type = "Delayed ERP update" if detected_at.date() != event.event_date else "Same-day ERP update"

        lines = [
            "Attendance Alert" if not was_present else "Attendance Update",
            "",
            f"Subject: {subject_text}",
            f"Faculty: {faculty_text}",
            f"Lecture date: {event.event_date.strftime('%A, %d %B %Y')}",
            f"Lecture time: {self._format_event_time(event)}",
            f"Attendance marked time: {self._format_datetime(detected_at)}",
            f"Update type: {update_type}",
            f"Final status: {status}",
            f"Cumulative attendance: {record.total_present}/{record.total_lecture}{percentage_text}",
        ]
        if marking_teacher:
            lines.append(f"Attendance marked by: {marking_teacher}")
        lines.extend(self._attendance_context_lines(event, detected_at=detected_at, record=record))

        if was_present:
            lines.extend(
                [
                    "",
                    "Attendance has been marked present for this lecture.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Attendance has been marked absent for this lecture.",
                    "Action: Review this absence in the ERP if you expected a present marking.",
                ]
            )

        if detected_at.date() != event.event_date:
            lines.extend(
                [
                    "",
                    (
                        "Late update: This lecture was marked "
                        f"{max((detected_at.date() - event.event_date).days, 1)} day(s) after the lecture date."
                    ),
                ]
            )
        if inferred_from_batch:
            lines.extend(
                [
                    "",
                    (
                        f"Note: The ERP updated {batch_size} pending lecture(s) for this subject together. "
                        "This lecture-level status is inferred from cumulative attendance totals."
                    ),
                ]
            )

        return "\n".join(lines)

    def _render_not_marked_message(self, event: LectureEvent) -> str:
        return "\n".join(
            [
                "Attendance Pending Update",
                "",
                f"Subject: {event.subject_name}",
                f"Faculty: {event.teacher_name or 'Not available'}",
                f"Lecture date: {event.event_date.strftime('%A, %d %B %Y')}",
                f"Lecture time: {self._format_event_time(event)}",
                "Current status: Not marked yet",
                "Next action: The bot will check again automatically.",
            ]
        )

    def _render_lecture_finished_status_message(
        self,
        event: LectureEvent,
        *,
        record,
        detected_at: datetime,
    ) -> str:
        status_text, marked, _ = self._attendance_state_for_event(event)
        subject_code = getattr(record, "subject_code", "") if record is not None else ""
        subject_text = self._format_subject_label(event.subject_name, subject_code)
        faculty_text = self._attendance_display_teacher_name(event, record)
        finish_reference = datetime.combine(
            event.event_date,
            event.end_time or event.start_time or time(0, 0),
            tzinfo=detected_at.tzinfo or self.timezone,
        )
        lines = [
            "Lecture Finished Attendance Status",
            "",
            f"Subject: {subject_text}",
            f"Faculty: {faculty_text}",
            f"Lecture date: {event.event_date.strftime('%A, %d %B %Y')}",
            f"Lecture time: {self._format_event_time(event)}",
            f"Lecture finished at: {self._format_datetime(finish_reference)}",
            f"Status checked at: {self._format_datetime(detected_at)}",
            f"{'Final status' if marked else 'Current status'}: {status_text}",
        ]
        if record is not None:
            percentage_text = f" ({record.percentage})" if record.percentage else ""
            lines.append(f"Cumulative attendance: {record.total_present}/{record.total_lecture}{percentage_text}")
        if marked:
            lines.extend(
                [
                    "",
                    "The lecture has finished and this is the latest attendance status currently visible in the ERP.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "The lecture has finished, but the ERP has not marked this lecture yet.",
                    "Next action: The bot will keep checking silently in the background and notify you if it changes.",
                ]
            )
        return "\n".join(lines)

    def _attendance_state_for_event(self, event: LectureEvent) -> tuple[str, bool, bool | None]:
        if event.status == "notified_present":
            return "Present", True, True
        if event.status == "notified_absent":
            return "Absent", True, False
        return "Not marked yet", False, None

    def _is_no_class_event(self, event: LectureEvent) -> bool:
        if not event.is_break:
            return False
        normalized_subject = self._normalize_text(event.subject_name)
        normalized_note = self._normalize_text(event.note)
        return normalized_subject in {"holiday", "off day", "no class"} or any(
            token in normalized_note
            for token in ("holiday", "off day", "no class", "not scheduled", "class cancelled", "class canceled")
        )

    def _attendance_context_lines(self, event: LectureEvent, *, detected_at: datetime, record=None) -> list[str]:
        lines: list[str] = []
        if event.note and "substitute lecture assigned" in event.note.lower():
            lines.append("Lecture type: Substitute lecture")
            lines.append(f"Substitute faculty: {self._attendance_display_teacher_name(event, record)}")
            original_faculty = self._extract_note_value(event.note, "Original faculty")
            if original_faculty:
                lines.append(f"Original faculty: {original_faculty}")
        end_time_text = self._extract_note_value(event.note, "Ends at")
        if end_time_text:
            lines.append(f"Scheduled end time: {end_time_text}")
        class_text = self._event_class_location(event)
        if class_text:
            lines.append(f"Class: {class_text}")
        if event.status_recorded_at and detected_at.date() == event.event_date:
            normalized = self._normalize_event_datetime(event.status_recorded_at, detected_at.tzinfo)
            lines.append(f"Recorded time reference: {self._format_datetime(normalized)}")
        return lines

    def _attendance_display_teacher_name(self, event: LectureEvent, record) -> str:
        event_teacher = (event.teacher_name or "").strip()
        record_teacher = self._attendance_marking_teacher_name(record)
        original_faculty = self._extract_note_value(event.note, "Original faculty")
        if event.note and "substitute lecture assigned" in event.note.lower():
            original_norm = self._normalize_text(original_faculty)
            for candidate in (record_teacher, event_teacher):
                if candidate and self._normalize_text(candidate) != original_norm:
                    return candidate
        return event_teacher or record_teacher or "Not available"

    def _attendance_marking_teacher_name(self, record) -> str:
        return str(getattr(record, "teacher_name", "") or "").strip()

    def _extract_note_value(self, note: str, label: str) -> str:
        prefix = f"{label}:"
        for part in note.split("|"):
            cleaned = part.strip()
            if cleaned.lower().startswith(prefix.lower()):
                return cleaned.split(":", 1)[1].strip()
        return ""

    def _event_class_location(self, event: LectureEvent | None) -> str:
        if event is None:
            return ""
        noted_location = self._extract_note_value(event.note, "Class")
        if noted_location:
            return noted_location
        return self._extract_class_location(event.raw_cell)

    def _format_event_time(self, event: LectureEvent) -> str:
        if event.start_time and event.end_time:
            return f"{event.start_time.strftime('%H:%M')} - {event.end_time.strftime('%H:%M')}"
        return event.slot_label

    def _event_has_started_by(self, event: LectureEvent, now_naive: datetime) -> bool:
        if event.start_time:
            return datetime.combine(event.event_date, event.start_time) <= now_naive
        return event.event_date <= now_naive.date()

    def _event_has_finished_by(self, event: LectureEvent, now_naive: datetime) -> bool:
        if event.end_time:
            return datetime.combine(event.event_date, event.end_time) <= now_naive
        if event.start_time:
            return datetime.combine(event.event_date, event.start_time) <= now_naive
        return event.event_date < now_naive.date()

    def _automation_timetable_context(
        self,
        student_now: datetime,
        lecture_events: list[LectureEvent],
    ) -> dict[str, str] | None:
        now_naive = student_now.replace(tzinfo=None)
        relevant_events = [
            event
            for event in lecture_events
            if not event.is_break and not self._is_no_class_event(event)
        ]
        if not relevant_events:
            return None

        current_events = [
            event
            for event in relevant_events
            if self._event_has_started_by(event, now_naive) and not self._event_has_finished_by(event, now_naive)
        ]
        if current_events:
            current_event = min(
                current_events,
                key=lambda event: event.start_time.isoformat() if event.start_time else "99:99",
            )
            return {
                "label": "Current timetable lecture",
                "subject_name": current_event.subject_name,
                "time_label": self._format_event_time(current_event),
            }

        upcoming_events = []
        for event in relevant_events:
            if event.start_time is None:
                continue
            if datetime.combine(event.event_date, event.start_time) > now_naive:
                upcoming_events.append(event)
        if upcoming_events:
            upcoming_event = min(
                upcoming_events,
                key=lambda event: event.start_time.isoformat() if event.start_time else "99:99",
            )
            return {
                "label": "Next timetable lecture",
                "subject_name": upcoming_event.subject_name,
                "time_label": self._format_event_time(upcoming_event),
            }

        latest_event = max(
            relevant_events,
            key=lambda event: (
                event.end_time.isoformat() if event.end_time else event.start_time.isoformat() if event.start_time else "",
                event.id,
            ),
        )
        return {
            "label": "Latest timetable lecture",
            "subject_name": latest_event.subject_name,
            "time_label": self._format_event_time(latest_event),
        }

    def _estimate_next_attendance_scan(
        self,
        student: Student,
        student_now: datetime,
    ) -> datetime | None:
        lookback_start = student_now.date() - timedelta(days=self.settings.attendance_correction_lookback_days)
        if not self.db.has_lecture_events_since(student.id, lookback_start):
            return None
        interval = timedelta(minutes=self.settings.attendance_poll_interval_minutes)
        last_sync = None
        if student.last_erp_sync_at:
            try:
                parsed = self._parse_datetime(student.last_erp_sync_at)
            except ValueError:
                parsed = None
            if parsed is not None:
                last_sync = parsed.astimezone(student_now.tzinfo or self.timezone)
        base_time = last_sync if last_sync and last_sync <= student_now else student_now
        next_scan = base_time + interval
        if next_scan <= student_now:
            next_scan = student_now + interval
        return next_scan

    def _normalize_event_datetime(
        self,
        value: datetime,
        tzinfo,
    ) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=tzinfo or self.timezone)
        return value.astimezone(tzinfo or self.timezone)

    def _parse_clock(self, value: str) -> time:
        try:
            return datetime.strptime(value.strip(), "%H:%M").time()
        except ValueError:
            return time(19, 0)

    def _format_datetime(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.timezone)
        return value.strftime("%A, %d %B %Y at %H:%M")

    def _format_subject_label(self, subject_name: str, subject_code: str) -> str:
        if subject_code:
            return f"{subject_name} ({subject_code})"
        return subject_name

    def _build_application_request_notification(
        self,
        *,
        request_id: int,
        applicant_name: str,
        student_label: str,
        user_name: str,
        reg_id: str | None,
        telegram_chat_id: str,
        timezone: str,
        note: str | None,
    ) -> str:
        lines = [
            "New student application request",
            "",
            f"Request id: {request_id}",
            f"Applicant: {applicant_name}",
            f"Preferred label: {student_label}",
            f"ERP user id: {user_name}",
            f"RegID: {reg_id or 'Not provided'}",
            f"Telegram: {telegram_chat_id or 'Not provided'}",
            f"Timezone: {timezone}",
            "ERP password: Submitted securely in the website application.",
        ]
        if note:
            lines.extend(["", "Note:", note])
        return "\n".join(lines)

    def _normalize_text(self, value: str) -> str:
        return " ".join((value or "").strip().lower().split())

    def _parse_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _format_remaining_time(self, value: timedelta) -> str:
        total_minutes = max(0, int(value.total_seconds() // 60))
        hours, minutes = divmod(total_minutes, 60)
        if hours and minutes:
            return f"{hours} hour(s) {minutes} minute(s)"
        if hours:
            return f"{hours} hour(s)"
        return f"{minutes} minute(s)"

    def _normalize_whatsapp_number(self, value: str) -> str:
        return value.strip().removeprefix("whatsapp:")

    def _canonical_whatsapp_number(self, value: str) -> str:
        raw = self._normalize_whatsapp_number(value)
        if not raw.strip():
            return ""
        normalized = re.sub(r"[^\d+]", "", raw)
        if normalized.startswith("00"):
            normalized = f"+{normalized[2:]}"
        if not normalized.startswith("+"):
            normalized = f"+{normalized}"
        if not re.fullmatch(r"\+[1-9]\d{7,14}", normalized):
            raise StudentValidationError("WhatsApp number must be in E.164 format, for example +91XXXXXXXXXX.")
        return normalized

    def _normalize_telegram_chat_id(self, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        if re.fullmatch(r"-?\d{5,20}", cleaned):
            return cleaned
        raise StudentValidationError(
            "Telegram chat id must be numeric, for example 123456789 or -1001234567890. "
            "Open the bot, send /start, then save that numeric chat id instead of an @username."
        )

    def _normalize_email_address(self, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned):
            raise StudentValidationError("Email address is not valid.")
        return cleaned.lower()

    def _normalize_site_login_username(self, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        if len(cleaned) < 3 or len(cleaned) > 64:
            raise StudentValidationError("Site login username must be between 3 and 64 characters.")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if any(ch not in allowed for ch in cleaned):
            raise StudentValidationError("Site login username can use letters, numbers, dots, underscores, and hyphens only.")
        return cleaned

    def _resolve_site_password_hash(
        self,
        *,
        student_id: int | None,
        site_login_username: str,
        site_login_password: str,
    ) -> str | None:
        password_value = site_login_password or ""
        if not site_login_username:
            return ""
        if len(password_value) >= 8:
            return generate_password_hash(password_value)
        if student_id is None:
            raise StudentValidationError("Site login password must be at least 8 characters long for a new student login.")
        current = self.db.get_student(student_id)
        if current and current.site_password_hash:
            return None
        raise StudentValidationError("Site login password must be at least 8 characters long.")

    def _validate_student_input(
        self,
        *,
        student_id: int | None,
        student_label: str,
        user_name: str,
        site_login_username: str,
        whatsapp_number: str,
        telegram_chat_id: str,
        email_address: str,
        timezone_name: str,
    ) -> None:
        if not student_label:
            raise StudentValidationError("Student label is required.")
        if not user_name:
            raise StudentValidationError("ERP user id is required.")
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise StudentValidationError(f"Invalid timezone: {timezone_name}") from exc

        user_name_key = user_name.casefold()
        site_login_key = site_login_username.casefold()
        whatsapp_key = whatsapp_number
        telegram_key = telegram_chat_id
        email_key = email_address.casefold()
        for student in self.db.list_students():
            if student_id and student.id == student_id:
                continue
            if student.user_name.casefold() == user_name_key:
                raise StudentValidationError("ERP user id already exists in another student profile.")
            if site_login_key and student.site_login_username.casefold() == site_login_key:
                raise StudentValidationError("Site login username already exists in another student profile.")
            if whatsapp_key:
                try:
                    student_whatsapp = self._canonical_whatsapp_number(student.whatsapp_number)
                except ValueError:
                    student_whatsapp = student.whatsapp_number.strip()
                if student_whatsapp == whatsapp_key:
                    raise StudentValidationError("WhatsApp number already exists in another student profile.")
            if telegram_key and (student.telegram_chat_id or "").strip() == telegram_key:
                raise StudentValidationError("Telegram chat id already exists in another student profile.")
            if email_key and (student.email_address or "").strip().casefold() == email_key:
                raise StudentValidationError("Email address already exists in another student profile.")

    def _find_student_by_whatsapp_number(self, value: str) -> Student | None:
        try:
            target = self._canonical_whatsapp_number(value)
        except ValueError:
            target = self._normalize_whatsapp_number(value).strip()
        for student in self.db.list_students():
            try:
                candidate = self._canonical_whatsapp_number(student.whatsapp_number)
            except ValueError:
                candidate = self._normalize_whatsapp_number(student.whatsapp_number).strip()
            if candidate == target:
                return student
        return None

    def _find_student_by_telegram_chat_id(self, value: str) -> Student | None:
        target = str(value or "").strip()
        if not target:
            return None
        for student in self.db.list_students():
            if (student.telegram_chat_id or "").strip() == target:
                return student
        return None

    def _render_inbound_help(self) -> str:
        return "\n".join(
            [
                "QUMS Bot Commands",
                "",
                "help - Show command list",
                "today - Send today's schedule",
                "next - Show the next lecture",
                "attendance - Show current attendance summary",
                "login status - Show ERP login state",
            ]
        )

    def _render_next_lecture(self, student: Student, current: datetime) -> str:
        summary = self._collect_daily_summary(student, current.date(), send_risk_alerts=False)
        lecture_events = self.db.get_lecture_events_for_day(student.id, current.date())
        for event in lecture_events:
            if event.is_break or not event.start_time:
                continue
            lecture_start = datetime.combine(current.date(), event.start_time, tzinfo=current.tzinfo)
            if lecture_start >= current:
                return "\n".join(
                    [
                        "Next Lecture",
                        "",
                        f"Subject: {event.subject_name}",
                        f"Faculty: {event.teacher_name or 'Not available'}",
                        *([f"Class: {self._event_class_location(event)}"] if self._event_class_location(event) else []),
                        f"Time: {self._format_event_time(event)}",
                        f"Date: {current.strftime('%A, %d %B %Y')}",
                        f"Note: {event.note or 'Regular lecture'}",
                    ]
                )
        return summary["message"]

    def _render_attendance_snapshot(self, student: Student) -> str:
        payload = self.erp.get_attendance_summary(student)
        attendance = parse_attendance_summary(payload)
        current = self._now_for_student(student)
        self.db.mark_student_erp_sync(student.id)
        teacher_events = self._risk_teacher_reference_events(student, target_date=current.date())
        subject_references = self._build_weekly_subject_reference_map(student, current.date())
        return self._build_attendance_summary_report(
            student_name=student.student_name or student.student_label,
            attendance=attendance,
            lecture_events=teacher_events,
            subject_references=subject_references,
            generated_at=current,
        )

    def _render_login_status(self, student: Student, current: datetime) -> str:
        return "\n".join(
            [
                "ERP Login Status",
                "",
                f"Student: {student.student_name or student.student_label}",
                f"ERP user id: {student.user_name}",
                f"Current time: {self._format_datetime(current)}",
                f"ERP session: {self._student_erp_status_text(student)}",
                f"Recent bot activity: {self._student_bot_activity_text(student)}",
                f"Session updated at: {student.session_updated_at or 'Not logged in yet'}",
            ]
        )

    def _student_erp_status_text(self, student: Student) -> str:
        raw_status = (student.erp_status_text or "").strip()
        if raw_status:
            return raw_status
        legacy_status = (student.last_login_status or "").strip()
        if legacy_status == "Waiting for manual captcha entry." or legacy_status.startswith("ERP session"):
            return legacy_status
        if student.session_cookies:
            return "ERP session saved."
        return "Not started"

    def _student_bot_activity_text(self, student: Student) -> str:
        return (student.last_bot_activity_text or "").strip() or "No recent bot activity recorded."

    def _send_whatsapp(
        self,
        student: Student,
        body: str,
        *,
        message_kind: str,
        history_category: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self._send_message(
            student,
            body,
            message_kind=message_kind,
            history_category=history_category,
            idempotency_key=idempotency_key,
        )

    def _send_message(
        self,
        student: Student,
        body: str,
        *,
        message_kind: str,
        history_category: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        title = body.splitlines()[0].strip() if body.strip() else message_kind
        category = history_category or message_kind
        if not student.enabled or self.notifications_paused(student):
            return
        targets = self._delivery_targets(student)
        if not targets:
            raise NotificationDeliveryError(
                "No delivery channel is configured for this student. Add a reachable Telegram chat id."
            )

        delivered_channels: list[str] = []
        delivery_errors: list[str] = []
        for channel, recipient in targets:
            channel_key = f"{idempotency_key}:{channel}" if idempotency_key else None
            if channel_key:
                claimed = self.db.claim_outbound_message(
                    idempotency_key=channel_key,
                    student_id=student.id,
                    channel=channel,
                    recipient=recipient,
                    category=category,
                    message_kind=message_kind,
                    title=title,
                    body=body,
                )
                if not claimed:
                    continue
            try:
                provider_sid = self._send_via_channel(
                    channel,
                    recipient,
                    body,
                    title=title,
                    message_kind=message_kind,
                )
            except Exception as exc:
                if channel_key:
                    try:
                        self.db.mark_outbound_message_failed(
                            channel_key,
                            str(exc),
                            retry_limit=self.settings.delivery_retry_limit,
                            retry_backoff_seconds=self.settings.delivery_retry_backoff_seconds,
                        )
                    except Exception:
                        pass
                delivery_errors.append(f"{channel}: {exc}")
                continue

            if channel_key:
                try:
                    self.db.mark_outbound_message_sent(channel_key, provider_sid)
                except Exception:
                    try:
                        self.db.update_student_status(
                            student.id,
                            f"{channel.title()} delivered but outbound state update failed: {category}",
                        )
                    except Exception:
                        pass
                    continue

            try:
                self.db.insert_message_history(
                    student_id=student.id,
                    channel=channel,
                    recipient=recipient,
                    category=category,
                    message_kind=message_kind,
                    provider_sid=provider_sid,
                    title=title,
                    body=body,
                    idempotency_key=channel_key,
                    delivery_status="accepted",
                )
            except Exception as exc:
                try:
                    self.db.update_student_status(student.id, f"{channel.title()} delivered but history logging failed: {exc}")
                except Exception:
                    pass
                continue
            delivered_channels.append(channel)

        if delivery_errors:
            try:
                self.db.update_student_status(student.id, f"Channel delivery issue: {'; '.join(delivery_errors)}")
            except Exception as exc:
                pass

        if delivered_channels or not delivery_errors:
            return
        raise NotificationDeliveryError("; ".join(delivery_errors))

    def _deliver_retry_message(self, student: Student, message) -> None:
        try:
            provider_sid = self._send_via_channel(
                message.channel,
                message.recipient,
                message.body,
                title=message.title,
                message_kind=message.message_kind,
            )
        except Exception as exc:
            self.db.mark_outbound_message_failed(
                message.idempotency_key,
                str(exc),
                retry_limit=self.settings.delivery_retry_limit,
                retry_backoff_seconds=self.settings.delivery_retry_backoff_seconds,
            )
            self.db.update_student_status(
                student.id,
                f"Retry delivery failed for {message.category}: {exc}",
            )
            return

        try:
            self.db.mark_outbound_message_sent(message.idempotency_key, provider_sid)
        except Exception as exc:
            self.db.update_student_status(
                student.id,
                f"Retry delivery succeeded but outbound state update failed for {message.category}: {exc}",
            )
            return

        try:
            self.db.insert_message_history(
                student_id=student.id,
                channel=message.channel,
                recipient=message.recipient,
                category=message.category,
                message_kind=message.message_kind,
                provider_sid=provider_sid,
                title=message.title,
                body=message.body,
                idempotency_key=message.idempotency_key,
                delivery_status="accepted",
            )
        except Exception as exc:
            self.db.update_student_status(
                student.id,
                f"Retry delivery succeeded but history logging failed for {message.category}: {exc}",
            )
            return
        self.db.update_student_status(student.id, f"Retry delivery succeeded for {message.category}.")

    def _delivery_targets(self, student: Student) -> list[tuple[str, str]]:
        targets: list[tuple[str, str]] = []
        if not student.enabled:
            return targets
        mode = self.get_student_notification_channel_mode(student)
        if mode == "paused":
            return targets
        if mode == "telegram_only" and bool(getattr(self.telegram, "configured", False)) and student.telegram_chat_id:
            targets.append(("telegram", student.telegram_chat_id))
        return targets

    def _normalize_notification_channel_mode(self, value: str | None) -> str:
        normalized = str(value or "telegram_only").strip().lower()
        if normalized in {"all", "whatsapp_only"}:
            return "telegram_only"
        if normalized not in NOTIFICATION_CHANNEL_MODE_LABELS:
            return "telegram_only"
        return normalized

    def _normalize_disabled_student_actions(self, values: Iterable[str]) -> set[str]:
        normalized: set[str] = set()
        for value in values:
            action_key = str(value or "").strip().lower()
            if action_key in STUDENT_ACTION_LABELS:
                normalized.add(action_key)
        return normalized

    def _send_via_channel(
        self,
        channel: str,
        recipient: str,
        body: str,
        *,
        title: str,
        message_kind: str,
    ) -> str:
        if channel == "telegram":
            return self.telegram.send_text(recipient, body, message_kind=message_kind)
        raise RuntimeError(f"Unsupported delivery channel: {channel}")

    def _require_student(self, student_id: int) -> Student:
        student = self.db.get_student(student_id)
        if not student:
            raise ERPClientError("Student profile not found.")
        return student

    def _student_timezone(self, student: Student) -> ZoneInfo:
        try:
            return ZoneInfo(student.timezone or self.settings.local_timezone)
        except Exception:
            return ZoneInfo(self.settings.local_timezone)

    def _now_for_student(self, student: Student, base_now: datetime | None = None) -> datetime:
        current = base_now or self._local_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=self.timezone)
        return current.astimezone(self._student_timezone(student))

    def _is_morning_dispatch_due(self, student: Student, student_now: datetime) -> bool:
        notification_key = student_now.date().isoformat()
        if self.db.has_notification_event(student.id, "morning_digest", notification_key):
            return False
        digest_time = self._parse_clock(self.settings.morning_digest_time)
        return student_now.time().replace(second=0, microsecond=0) >= digest_time

    def _local_now(self) -> datetime:
        return datetime.now(self.timezone)
