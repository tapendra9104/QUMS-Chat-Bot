from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

from .models import LectureEvent, LectureSlot, PendingLogin, Student


def utcnow_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def time_to_iso(value: time | None) -> str | None:
    return value.isoformat(timespec="minutes") if value else None


def time_from_iso(value: str | None) -> time | None:
    if not value:
        return None
    return time.fromisoformat(value)


def datetime_to_iso(value: datetime | None) -> str | None:
    if not value:
        return None
    return value.replace(microsecond=0).isoformat()


def datetime_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_label TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    password_encrypted TEXT NOT NULL,
                    whatsapp_number TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                    reg_id TEXT,
                    student_name TEXT,
                    session_cookies TEXT,
                    session_updated_at TEXT,
                    last_login_status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_logins (
                    student_id INTEGER PRIMARY KEY,
                    request_verification_token TEXT NOT NULL,
                    hdn_msg TEXT NOT NULL,
                    check_online TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    captcha_data_url TEXT NOT NULL,
                    cookies_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attendance_snapshots (
                    student_id INTEGER NOT NULL,
                    subject_key TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    subject_code TEXT NOT NULL,
                    teacher_name TEXT NOT NULL,
                    total_lecture INTEGER NOT NULL,
                    total_present INTEGER NOT NULL,
                    percentage TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (student_id, subject_key)
                );

                CREATE TABLE IF NOT EXISTS lecture_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    subject_key TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    teacher_name TEXT NOT NULL,
                    slot_label TEXT NOT NULL,
                    raw_cell TEXT NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    is_break INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    check_after TEXT,
                    note TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def list_students(self) -> list[Student]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM students ORDER BY id").fetchall()
        return [self._student_from_row(row) for row in rows]

    def get_student(self, student_id: int) -> Student | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        return self._student_from_row(row) if row else None

    def upsert_student(
        self,
        *,
        student_id: int | None,
        student_label: str,
        user_name: str,
        password_encrypted: str | None,
        whatsapp_number: str,
        enabled: bool,
        timezone: str,
    ) -> int:
        now = utcnow_iso()
        with self._connect() as conn:
            if student_id:
                current = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
                if not current:
                    raise ValueError("Student not found.")
                encrypted_password = password_encrypted or current["password_encrypted"]
                conn.execute(
                    """
                    UPDATE students
                    SET student_label = ?, user_name = ?, password_encrypted = ?, whatsapp_number = ?,
                        enabled = ?, timezone = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        student_label,
                        user_name,
                        encrypted_password,
                        whatsapp_number,
                        1 if enabled else 0,
                        timezone,
                        now,
                        student_id,
                    ),
                )
                return student_id

            if not password_encrypted:
                raise ValueError("Password is required for a new student.")
            cursor = conn.execute(
                """
                INSERT INTO students (
                    student_label, user_name, password_encrypted, whatsapp_number,
                    enabled, timezone, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student_label,
                    user_name,
                    password_encrypted,
                    whatsapp_number,
                    1 if enabled else 0,
                    timezone,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def delete_student(self, student_id: int) -> bool:
        with self._connect() as conn:
            student = conn.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
            if not student:
                return False

            conn.execute("DELETE FROM pending_logins WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM attendance_snapshots WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM lecture_events WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
        return True

    def update_student_session(
        self,
        *,
        student_id: int,
        cookies_json: str,
        last_login_status: str,
        reg_id: str | None = None,
        student_name: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE students
                SET session_cookies = ?, session_updated_at = ?, last_login_status = ?,
                    reg_id = COALESCE(?, reg_id), student_name = COALESCE(?, student_name),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    cookies_json,
                    utcnow_iso(),
                    last_login_status,
                    reg_id,
                    student_name,
                    utcnow_iso(),
                    student_id,
                ),
            )

    def update_student_status(self, student_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE students SET last_login_status = ?, updated_at = ? WHERE id = ?",
                (status, utcnow_iso(), student_id),
            )

    def save_pending_login(self, pending: PendingLogin) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_logins (
                    student_id, request_verification_token, hdn_msg, check_online,
                    client_ip, captcha_data_url, cookies_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(student_id) DO UPDATE SET
                    request_verification_token = excluded.request_verification_token,
                    hdn_msg = excluded.hdn_msg,
                    check_online = excluded.check_online,
                    client_ip = excluded.client_ip,
                    captcha_data_url = excluded.captcha_data_url,
                    cookies_json = excluded.cookies_json,
                    created_at = excluded.created_at
                """,
                (
                    pending.student_id,
                    pending.request_verification_token,
                    pending.hdn_msg,
                    pending.check_online,
                    pending.client_ip,
                    pending.captcha_data_url,
                    pending.cookies_json,
                    pending.created_at,
                ),
            )

    def get_pending_login(self, student_id: int) -> PendingLogin | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_logins WHERE student_id = ?",
                (student_id,),
            ).fetchone()
        if not row:
            return None
        return PendingLogin(
            student_id=row["student_id"],
            request_verification_token=row["request_verification_token"],
            hdn_msg=row["hdn_msg"],
            check_online=row["check_online"],
            client_ip=row["client_ip"],
            captcha_data_url=row["captcha_data_url"],
            cookies_json=row["cookies_json"],
            created_at=row["created_at"],
        )

    def clear_pending_login(self, student_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_logins WHERE student_id = ?", (student_id,))

    def get_attendance_snapshots(self, student_id: int) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attendance_snapshots WHERE student_id = ?",
                (student_id,),
            ).fetchall()
        return {row["subject_key"]: dict(row) for row in rows}

    def upsert_attendance_snapshot(
        self,
        *,
        student_id: int,
        subject_key: str,
        subject_name: str,
        subject_code: str,
        teacher_name: str,
        total_lecture: int,
        total_present: int,
        percentage: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO attendance_snapshots (
                    student_id, subject_key, subject_name, subject_code, teacher_name,
                    total_lecture, total_present, percentage, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(student_id, subject_key) DO UPDATE SET
                    subject_name = excluded.subject_name,
                    subject_code = excluded.subject_code,
                    teacher_name = excluded.teacher_name,
                    total_lecture = excluded.total_lecture,
                    total_present = excluded.total_present,
                    percentage = excluded.percentage,
                    updated_at = excluded.updated_at
                """,
                (
                    student_id,
                    subject_key,
                    subject_name,
                    subject_code,
                    teacher_name,
                    total_lecture,
                    total_present,
                    percentage,
                    utcnow_iso(),
                ),
            )

    def replace_lecture_events(
        self,
        *,
        student_id: int,
        event_date: date,
        slots: Iterable[LectureSlot],
        grace_minutes: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM lecture_events WHERE student_id = ? AND event_date = ?",
                (student_id, event_date.isoformat()),
            )
            for slot in slots:
                check_after = None
                if slot.end_time:
                    end_dt = datetime.combine(event_date, slot.end_time)
                    check_after = end_dt + timedelta(minutes=grace_minutes)
                conn.execute(
                    """
                    INSERT INTO lecture_events (
                        student_id, event_date, subject_key, subject_name, teacher_name,
                        slot_label, raw_cell, start_time, end_time, is_break, status,
                        check_after, note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        student_id,
                        event_date.isoformat(),
                        slot.subject_key,
                        slot.subject_name,
                        slot.teacher_name,
                        slot.slot_label,
                        slot.raw_cell,
                        time_to_iso(slot.start_time),
                        time_to_iso(slot.end_time),
                        1 if slot.is_break else 0,
                        "scheduled",
                        datetime_to_iso(check_after),
                        slot.note,
                    ),
                )

    def get_due_lecture_events(self, now: datetime) -> list[LectureEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM lecture_events
                WHERE is_break = 0
                  AND check_after IS NOT NULL
                  AND check_after <= ?
                  AND status IN ('scheduled', 'notified_unmarked')
                ORDER BY student_id, event_date, start_time
                """,
                (datetime_to_iso(now),),
            ).fetchall()
        return [self._lecture_event_from_row(row) for row in rows]

    def get_lecture_events_for_day(self, student_id: int, event_date: date) -> list[LectureEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM lecture_events
                WHERE student_id = ? AND event_date = ?
                ORDER BY start_time, slot_label
                """,
                (student_id, event_date.isoformat()),
            ).fetchall()
        return [self._lecture_event_from_row(row) for row in rows]

    def mark_event_status(
        self,
        event_id: int,
        status: str,
        *,
        next_check_after: datetime | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE lecture_events SET status = ?, check_after = ? WHERE id = ?",
                (status, datetime_to_iso(next_check_after), event_id),
            )

    def _student_from_row(self, row: sqlite3.Row) -> Student:
        return Student(
            id=row["id"],
            student_label=row["student_label"],
            user_name=row["user_name"],
            password_encrypted=row["password_encrypted"],
            whatsapp_number=row["whatsapp_number"],
            enabled=bool(row["enabled"]),
            timezone=row["timezone"],
            reg_id=row["reg_id"],
            student_name=row["student_name"],
            session_cookies=row["session_cookies"],
            session_updated_at=row["session_updated_at"],
            last_login_status=row["last_login_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _lecture_event_from_row(self, row: sqlite3.Row) -> LectureEvent:
        return LectureEvent(
            id=row["id"],
            student_id=row["student_id"],
            event_date=date.fromisoformat(row["event_date"]),
            subject_key=row["subject_key"],
            subject_name=row["subject_name"],
            teacher_name=row["teacher_name"],
            slot_label=row["slot_label"],
            raw_cell=row["raw_cell"],
            start_time=time_from_iso(row["start_time"]),
            end_time=time_from_iso(row["end_time"]),
            is_break=bool(row["is_break"]),
            status=row["status"],
            check_after=datetime_from_iso(row["check_after"]),
            note=row["note"],
        )
