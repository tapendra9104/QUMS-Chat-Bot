from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .erp_client import AuthenticationRequired, ERPClient, ERPClientError, LoginFailed
from .models import LectureEvent, PendingLogin, Student
from .parsers import (
    format_slot_time,
    match_attendance_record,
    parse_attendance_summary,
    parse_student_detail_response,
    parse_substitutions,
    parse_timetable_slots,
)
from .security import encrypt_text
from .whatsapp import WhatsAppError, WhatsAppSender


class BotService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        erp_client: ERPClient,
        whatsapp: WhatsAppSender,
    ) -> None:
        self.settings = settings
        self.db = db
        self.erp = erp_client
        self.whatsapp = whatsapp
        self.timezone = ZoneInfo(settings.local_timezone)

    def list_students(self) -> list[Student]:
        return self.db.list_students()

    def get_student(self, student_id: int) -> Student | None:
        return self.db.get_student(student_id)

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
        whatsapp_number: str,
        enabled: bool,
        timezone: str,
    ) -> int:
        encrypted = encrypt_text(self.settings.app_secret, password) if password else None
        return self.db.upsert_student(
            student_id=student_id,
            student_label=student_label.strip(),
            user_name=user_name.strip(),
            password_encrypted=encrypted,
            whatsapp_number=whatsapp_number.strip(),
            enabled=enabled,
            timezone=timezone.strip() or self.settings.local_timezone,
        )

    def start_login(self, student_id: int) -> PendingLogin:
        student = self._require_student(student_id)
        pending = self.erp.start_manual_login(student)
        self.db.save_pending_login(pending)
        self.db.update_student_status(student.id, "Waiting for manual captcha entry.")
        return pending

    def refresh_login(self, student_id: int) -> PendingLogin:
        pending = self.db.get_pending_login(student_id)
        if not pending:
            raise ERPClientError("No pending login session. Start login first.")
        refreshed = self.erp.refresh_captcha(pending)
        self.db.save_pending_login(refreshed)
        return refreshed

    def complete_login(self, student_id: int, captcha: str) -> str:
        student = self._require_student(student_id)
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
        target = target_date or self._local_now().date()
        summary = self._collect_daily_summary(student, target)
        return summary["message"]

    def send_morning_update(self, student_id: int, target_date: date | None = None) -> str:
        student = self._require_student(student_id)
        target = target_date or self._local_now().date()
        summary = self._collect_daily_summary(student, target)
        self._send_whatsapp(student, summary["message"], message_kind="morning")
        self.db.update_student_status(student.id, f"Morning summary sent for {target.isoformat()}.")
        return summary["message"]

    def send_test_message(self, student_id: int) -> str:
        student = self._require_student(student_id)
        body = (
            "QUMS bot test message.\n"
            "If you received this, WhatsApp delivery is configured correctly."
        )
        self._send_whatsapp(student, body, message_kind="generic")
        return body

    def run_morning_sweep(self) -> None:
        for student in self.db.list_students():
            if not student.enabled:
                continue
            try:
                self.send_morning_update(student.id)
            except (ERPClientError, WhatsAppError) as exc:
                self.db.update_student_status(student.id, f"Morning sync failed: {exc}")

    def run_due_checks(self) -> None:
        now = self._local_now()
        due_events = self.db.get_due_lecture_events(now.replace(tzinfo=None))
        events_by_student: dict[int, list[LectureEvent]] = defaultdict(list)
        for event in due_events:
            events_by_student[event.student_id].append(event)

        for student_id, events in events_by_student.items():
            student = self.db.get_student(student_id)
            if not student or not student.enabled:
                continue
            try:
                payload = self.erp.get_attendance_summary(student)
                attendance = parse_attendance_summary(payload)
                snapshots = self.db.get_attendance_snapshots(student.id)
            except ERPClientError as exc:
                self.db.update_student_status(student.id, f"Attendance check failed: {exc}")
                continue

            for event in events:
                record = match_attendance_record(attendance, event.subject_key, event.subject_name)
                if not record:
                    next_check = now.replace(tzinfo=None) + timedelta(
                        minutes=self.settings.attendance_poll_interval_minutes
                    )
                    self.db.mark_event_status(event.id, event.status, next_check_after=next_check)
                    continue

                snapshot = snapshots.get(record.subject_key)
                previous_lecture = int(snapshot["total_lecture"]) if snapshot else 0
                previous_present = int(snapshot["total_present"]) if snapshot else 0

                if record.total_lecture > previous_lecture:
                    final_status = "notified_present" if record.total_present > previous_present else "notified_absent"
                    body = self._render_attendance_message(event, record.total_present > previous_present, record)
                    try:
                        self._send_whatsapp(student, body, message_kind="attendance")
                    except WhatsAppError as exc:
                        self.db.update_student_status(student.id, f"WhatsApp delivery failed: {exc}")
                        continue
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
                    self.db.mark_event_status(event.id, final_status)
                    self.db.update_student_status(student.id, f"Attendance updated for {record.subject_name}.")
                    continue

                next_check = now.replace(tzinfo=None) + timedelta(
                    minutes=self.settings.attendance_poll_interval_minutes
                )
                if event.status == "scheduled":
                    try:
                        self._send_whatsapp(
                            student,
                            self._render_not_marked_message(event),
                            message_kind="attendance",
                        )
                    except WhatsAppError as exc:
                        self.db.update_student_status(student.id, f"WhatsApp delivery failed: {exc}")
                        continue
                    self.db.mark_event_status(event.id, "notified_unmarked", next_check_after=next_check)
                else:
                    self.db.mark_event_status(event.id, "notified_unmarked", next_check_after=next_check)

    def _collect_daily_summary(self, student: Student, target_date: date) -> dict[str, object]:
        timetable_payload = self.erp.get_timetable(student)
        substitutions_payload = self.erp.get_substitutions(student)
        attendance_payload = self.erp.get_attendance_summary(student)
        detail_payload = self.erp.get_student_detail(student)

        detail = parse_student_detail_response(detail_payload) or {}
        slots = parse_timetable_slots(substitutions_payload, target_date)
        if not slots:
            slots = parse_timetable_slots(timetable_payload, target_date)
        substitutions = parse_substitutions(substitutions_payload, target_date)
        attendance = parse_attendance_summary(attendance_payload)

        self.db.replace_lecture_events(
            student_id=student.id,
            event_date=target_date,
            slots=slots,
            grace_minutes=self.settings.lecture_grace_minutes,
        )
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

        message = self._render_morning_message(
            student_name=str(detail.get("StudentName") or student.student_name or student.student_label).strip(),
            target_date=target_date,
            slots=slots,
            substitutions=substitutions,
        )
        self.db.update_student_status(student.id, f"ERP sync completed for {target_date.isoformat()}.")
        return {"message": message, "slots": slots, "substitutions": substitutions}

    def _render_morning_message(
        self,
        *,
        student_name: str,
        target_date: date,
        slots,
        substitutions,
    ) -> str:
        lines = [
            f"QUMS schedule for {student_name or 'student'}",
            target_date.strftime("%A, %d %B %Y"),
            "",
            "Today:",
        ]

        if not slots:
            lines.append("- No timetable rows were found for today.")
        else:
            for slot in slots:
                time_text = format_slot_time(slot)
                if slot.is_break:
                    lines.append(f"- {time_text}: Break")
                    continue
                teacher_text = f" | Teacher: {slot.teacher_name}" if slot.teacher_name else ""
                note_text = f" | Note: {slot.note}" if slot.note else ""
                lines.append(f"- {time_text}: {slot.subject_name}{teacher_text}{note_text}")

        lines.extend(["", "Substitutions:"])
        if not substitutions:
            lines.append("- No substitutions found.")
        else:
            for item in substitutions:
                period = item.period or item.time_text or "Slot"
                replacement = item.substitute_subject or item.original_subject or "Updated class"
                teacher = item.substitute_teacher or item.original_teacher
                teacher_text = f" | Teacher: {teacher}" if teacher else ""
                lines.append(f"- {period}: {replacement}{teacher_text}")

        lines.extend(
            [
                "",
                "Attendance updates will be checked after each lecture.",
                "If the ERP session expires, open the dashboard and enter a fresh captcha.",
            ]
        )
        return "\n".join(lines)

    def _render_attendance_message(self, event: LectureEvent, was_present: bool, record) -> str:
        status = "Present" if was_present else "Absent"
        percentage_text = f" | Attendance %: {record.percentage}" if record.percentage else ""
        return (
            f"Attendance update for {event.subject_name}\n"
            f"Time: {event.slot_label}\n"
            f"Teacher: {event.teacher_name or record.teacher_name or 'Not available'}\n"
            f"Status: {status}\n"
            f"Total lectures: {record.total_lecture} | Total present: {record.total_present}{percentage_text}"
        )

    def _render_not_marked_message(self, event: LectureEvent) -> str:
        return (
            f"Attendance not marked yet for {event.subject_name}\n"
            f"Time: {event.slot_label}\n"
            f"Teacher: {event.teacher_name or 'Not available'}\n"
            "The bot will check again automatically."
        )

    def _send_whatsapp(self, student: Student, body: str, *, message_kind: str) -> None:
        self.whatsapp.send_text(student.whatsapp_number, body, message_kind=message_kind)

    def _require_student(self, student_id: int) -> Student:
        student = self.db.get_student(student_id)
        if not student:
            raise ERPClientError("Student profile not found.")
        return student

    def _local_now(self) -> datetime:
        return datetime.now(self.timezone)
