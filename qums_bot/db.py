from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .models import (
    AdminAuditRecord,
    ApplicationRequest,
    DailyAttendanceReport,
    LectureEvent,
    LectureSlot,
    MessageHistoryRecord,
    OutboundMessageRecord,
    PendingLogin,
    Student,
    TelegramAdminChat,
    TelegramAdminSession,
)
from .errors import StudentValidationError


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _is_erp_session_status(status: str | None) -> bool:
        if not status:
            return False
        normalized = status.strip()
        if not normalized:
            return False
        return normalized == "Waiting for manual captcha entry." or normalized.startswith("ERP session")

    def _session_erp_status_text(self, status: str | None, *, has_session: bool) -> str | None:
        if self._is_erp_session_status(status):
            return status.strip()
        if has_session:
            return "ERP session saved."
        return None

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_label TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    password_encrypted TEXT NOT NULL,
                    site_login_username TEXT NOT NULL DEFAULT '',
                    site_password_hash TEXT NOT NULL DEFAULT '',
                    site_password_updated_at TEXT,
                    whatsapp_number TEXT NOT NULL,
                    telegram_chat_id TEXT NOT NULL DEFAULT '',
                    email_address TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    notification_channel_mode TEXT NOT NULL DEFAULT 'telegram_only',
                    disabled_actions_json TEXT NOT NULL DEFAULT '[]',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                    reg_id TEXT,
                    student_name TEXT,
                    session_cookies TEXT,
                    session_updated_at TEXT,
                    last_login_status TEXT,
                    erp_status_text TEXT,
                    erp_status_updated_at TEXT,
                    last_bot_activity_text TEXT,
                    last_erp_sync_at TEXT,
                    last_bot_action_at TEXT,
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
                    status_recorded_at TEXT,
                    note TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS daily_attendance_reports (
                    student_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    total_lectures INTEGER NOT NULL,
                    marked_count INTEGER NOT NULL,
                    present_count INTEGER NOT NULL,
                    absent_count INTEGER NOT NULL,
                    unmarked_count INTEGER NOT NULL,
                    report_body TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (student_id, event_date)
                );

                CREATE TABLE IF NOT EXISTS substitution_alerts (
                    student_id INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    period TEXT NOT NULL,
                    time_text TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    teacher_name TEXT NOT NULL,
                    end_time_text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    notified_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (student_id, event_date, alert_key)
                );

                CREATE TABLE IF NOT EXISTS notification_events (
                    student_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    notification_key TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (student_id, category, notification_key)
                );

                CREATE TABLE IF NOT EXISTS scheduler_job_slots (
                    job_name TEXT NOT NULL,
                    slot_key TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (job_name, slot_key)
                );

                CREATE TABLE IF NOT EXISTS outbound_messages (
                    idempotency_key TEXT PRIMARY KEY,
                    student_id INTEGER NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'whatsapp',
                    recipient TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL,
                    message_kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_sid TEXT,
                    delivery_status TEXT,
                    delivery_error_code INTEGER,
                    delivery_error_message TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    dead_lettered_at TEXT,
                    claimed_at TEXT NOT NULL,
                    sent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'whatsapp',
                    recipient TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL,
                    message_kind TEXT NOT NULL,
                    provider_sid TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    idempotency_key TEXT,
                    delivery_status TEXT,
                    delivery_error_code INTEGER,
                    delivery_error_message TEXT,
                    status_updated_at TEXT,
                    sent_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS application_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    applicant_name TEXT NOT NULL,
                    student_label TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    password_encrypted TEXT NOT NULL,
                    site_login_username TEXT NOT NULL DEFAULT '',
                    site_password_hash TEXT NOT NULL DEFAULT '',
                    reg_id TEXT,
                    whatsapp_number TEXT NOT NULL,
                    telegram_chat_id TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                    note TEXT,
                    created_from_ip TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_admin_chats (
                    chat_id TEXT PRIMARY KEY,
                    auto_refresh_enabled INTEGER NOT NULL DEFAULT 0,
                    dashboard_message_id TEXT,
                    last_dashboard_sent_at TEXT,
                    last_dashboard_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_admin_sessions (
                    chat_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    step TEXT NOT NULL,
                    student_id INTEGER,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_lecture_events_day
                    ON lecture_events (student_id, event_date, start_time);

                CREATE INDEX IF NOT EXISTS idx_lecture_events_pending
                    ON lecture_events (status, check_after, student_id, event_date);

                CREATE INDEX IF NOT EXISTS idx_scheduler_job_slots_claimed
                    ON scheduler_job_slots (claimed_at DESC, job_name);

                CREATE INDEX IF NOT EXISTS idx_outbound_messages_student
                    ON outbound_messages (student_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_outbound_messages_provider_sid
                    ON outbound_messages (provider_sid);

                CREATE INDEX IF NOT EXISTS idx_message_history_recent
                    ON message_history (sent_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_admin_audit_recent
                    ON admin_audit_log (created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_application_requests_recent
                    ON application_requests (created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_telegram_admin_refresh
                    ON telegram_admin_chats (auto_refresh_enabled, updated_at DESC);
                """
            )
            self._ensure_column(conn, "lecture_events", "status_recorded_at", "TEXT")
            self._ensure_column(conn, "students", "site_login_username", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "students", "site_password_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "students", "site_password_updated_at", "TEXT")
            self._ensure_column(conn, "students", "telegram_chat_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "students", "email_address", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "students", "notification_channel_mode", "TEXT NOT NULL DEFAULT 'telegram_only'")
            self._ensure_column(conn, "students", "disabled_actions_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "students", "erp_status_text", "TEXT")
            self._ensure_column(conn, "students", "erp_status_updated_at", "TEXT")
            self._ensure_column(conn, "students", "last_bot_activity_text", "TEXT")
            self._ensure_column(conn, "students", "last_erp_sync_at", "TEXT")
            self._ensure_column(conn, "students", "last_bot_action_at", "TEXT")
            self._ensure_column(conn, "application_requests", "site_login_username", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "application_requests", "site_password_hash", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                UPDATE students
                SET erp_status_text = COALESCE(
                        erp_status_text,
                        CASE
                            WHEN last_login_status = 'Waiting for manual captcha entry.' THEN last_login_status
                            WHEN last_login_status LIKE 'ERP session%' THEN last_login_status
                            WHEN session_cookies IS NOT NULL AND TRIM(session_cookies) != '' THEN 'ERP session saved.'
                            ELSE erp_status_text
                        END
                    ),
                    erp_status_updated_at = COALESCE(erp_status_updated_at, session_updated_at)
                """
            )
            conn.execute(
                """
                UPDATE students
                SET last_bot_activity_text = COALESCE(
                        last_bot_activity_text,
                        CASE
                            WHEN last_login_status IS NOT NULL
                                 AND last_login_status != ''
                                 AND last_login_status != 'Waiting for manual captcha entry.'
                                 AND last_login_status NOT LIKE 'ERP session%'
                            THEN last_login_status
                            ELSE last_bot_activity_text
                        END
                    ),
                    last_bot_action_at = COALESCE(last_bot_action_at, updated_at)
                """
            )
            conn.execute(
                """
                UPDATE students
                SET notification_channel_mode = 'telegram_only'
                WHERE notification_channel_mode IN ('all', 'whatsapp_only')
                """
            )
            self._ensure_column(conn, "message_history", "idempotency_key", "TEXT")
            self._ensure_column(conn, "message_history", "channel", "TEXT NOT NULL DEFAULT 'whatsapp'")
            self._ensure_column(conn, "message_history", "recipient", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "message_history", "delivery_status", "TEXT")
            self._ensure_column(conn, "message_history", "delivery_error_code", "INTEGER")
            self._ensure_column(conn, "message_history", "delivery_error_message", "TEXT")
            self._ensure_column(conn, "message_history", "status_updated_at", "TEXT")
            self._ensure_column(conn, "outbound_messages", "channel", "TEXT NOT NULL DEFAULT 'whatsapp'")
            self._ensure_column(conn, "outbound_messages", "recipient", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "outbound_messages", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "outbound_messages", "next_retry_at", "TEXT")
            self._ensure_column(conn, "outbound_messages", "dead_lettered_at", "TEXT")
            self._ensure_column(conn, "telegram_admin_chats", "last_dashboard_hash", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbound_messages_retry
                    ON outbound_messages (status, next_retry_at, updated_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lecture_events_recorded
                    ON lecture_events (student_id, status_recorded_at DESC, id DESC)
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

    def get_student_by_whatsapp_number(self, normalized_number: str) -> Student | None:
        candidates = {
            normalized_number.strip(),
            normalized_number.replace("whatsapp:", "", 1).strip(),
        }
        with self._connect() as conn:
            for candidate in candidates:
                row = conn.execute(
                    "SELECT * FROM students WHERE whatsapp_number = ?",
                    (candidate,),
                ).fetchone()
                if row:
                    return self._student_from_row(row)
        return None

    def get_student_by_site_login_username(self, normalized_username: str) -> Student | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM students
                WHERE LOWER(site_login_username) = ?
                """,
                (normalized_username.strip().lower(),),
            ).fetchone()
        return self._student_from_row(row) if row else None

    def upsert_student(
        self,
        *,
        student_id: int | None,
        student_label: str,
        user_name: str,
        password_encrypted: str | None,
        site_login_username: str = "",
        site_password_hash: str | None = None,
        whatsapp_number: str,
        telegram_chat_id: str,
        email_address: str,
        enabled: bool,
        notification_channel_mode: str = "telegram_only",
        disabled_actions_json: str = "[]",
        timezone: str,
    ) -> int:
        now = utcnow_iso()
        with self._connect() as conn:
            if student_id:
                current = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
                if not current:
                    raise StudentValidationError("Student not found.")
                encrypted_password = password_encrypted or current["password_encrypted"]
                resolved_site_password_hash = site_password_hash if site_password_hash is not None else current["site_password_hash"]
                resolved_site_password_updated_at = (
                    now if site_password_hash is not None and site_password_hash != current["site_password_hash"] else current["site_password_updated_at"]
                )
                conn.execute(
                    """
                    UPDATE students
                    SET student_label = ?, user_name = ?, password_encrypted = ?, site_login_username = ?,
                        site_password_hash = ?, site_password_updated_at = ?, whatsapp_number = ?,
                        telegram_chat_id = ?, email_address = ?, enabled = ?, notification_channel_mode = ?,
                        disabled_actions_json = ?, timezone = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        student_label,
                        user_name,
                        encrypted_password,
                        site_login_username,
                        resolved_site_password_hash,
                        resolved_site_password_updated_at,
                        whatsapp_number,
                        telegram_chat_id,
                        email_address,
                        1 if enabled else 0,
                        notification_channel_mode,
                        disabled_actions_json,
                        timezone,
                        now,
                        student_id,
                    ),
                )
                return student_id

            if not password_encrypted:
                raise StudentValidationError("Password is required for a new student.")
            cursor = conn.execute(
                """
                INSERT INTO students (
                    student_label, user_name, password_encrypted, site_login_username, site_password_hash,
                    site_password_updated_at, whatsapp_number, telegram_chat_id, email_address,
                    enabled, notification_channel_mode, disabled_actions_json, timezone, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student_label,
                    user_name,
                    password_encrypted,
                    site_login_username,
                    site_password_hash or "",
                    now if site_password_hash else None,
                    whatsapp_number,
                    telegram_chat_id,
                    email_address,
                    1 if enabled else 0,
                    notification_channel_mode,
                    disabled_actions_json,
                    timezone,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_student_controls(
        self,
        *,
        student_id: int,
        enabled: bool,
        notification_channel_mode: str,
        disabled_actions_json: str,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            current = conn.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
            if not current:
                raise StudentValidationError("Student not found.")
            conn.execute(
                """
                UPDATE students
                SET enabled = ?,
                    notification_channel_mode = ?,
                    disabled_actions_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    1 if enabled else 0,
                    notification_channel_mode,
                    disabled_actions_json,
                    now,
                    student_id,
                ),
            )

    def update_student_site_credentials(
        self,
        *,
        student_id: int,
        site_login_username: str | None = None,
        site_password_hash: str | None = None,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            current = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
            if not current:
                raise StudentValidationError("Student not found.")
            conn.execute(
                """
                UPDATE students
                SET site_login_username = ?,
                    site_password_hash = ?,
                    site_password_updated_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    site_login_username if site_login_username is not None else current["site_login_username"],
                    site_password_hash if site_password_hash is not None else current["site_password_hash"],
                    now if site_password_hash is not None else current["site_password_updated_at"],
                    now,
                    student_id,
                ),
            )

    def delete_student(self, student_id: int) -> bool:
        with self._connect() as conn:
            student = conn.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
            if not student:
                return False

            conn.execute("DELETE FROM pending_logins WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM attendance_snapshots WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM lecture_events WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM daily_attendance_reports WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM substitution_alerts WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM notification_events WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM outbound_messages WHERE student_id = ?", (student_id,))
            conn.execute("DELETE FROM message_history WHERE student_id = ?", (student_id,))
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
        now = utcnow_iso()
        erp_status_text = self._session_erp_status_text(last_login_status, has_session=bool(cookies_json))
        bot_activity_text = None if self._is_erp_session_status(last_login_status) else (last_login_status.strip() or None)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE students
                SET session_cookies = ?, session_updated_at = ?, last_login_status = ?,
                    erp_status_text = ?, erp_status_updated_at = ?,
                    last_bot_activity_text = COALESCE(?, last_bot_activity_text),
                    reg_id = COALESCE(?, reg_id), student_name = COALESCE(?, student_name),
                    last_bot_action_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    cookies_json,
                    now,
                    last_login_status,
                    erp_status_text,
                    now,
                    bot_activity_text,
                    reg_id,
                    student_name,
                    now,
                    now,
                    student_id,
                ),
            )

    def update_student_status(self, student_id: int, status: str) -> None:
        self.update_student_bot_activity(student_id, status)

    def update_student_bot_activity(self, student_id: int, status: str) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE students
                SET last_login_status = ?, last_bot_activity_text = ?, last_bot_action_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, status, now, now, student_id),
            )

    def update_student_erp_status(self, student_id: int, status: str) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE students
                SET last_login_status = ?, erp_status_text = ?, erp_status_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, status, now, now, student_id),
            )

    def mark_student_erp_sync(self, student_id: int, *, synced_at: str | None = None) -> None:
        now = synced_at or utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE students
                SET last_erp_sync_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, student_id),
            )

    def get_outbound_message(self, idempotency_key: str) -> OutboundMessageRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM outbound_messages
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        return self._outbound_message_from_row(row) if row else None

    def get_runtime_state(self, state_key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        return str(row["state_value"]) if row else None

    def upsert_runtime_state(self, *, state_key: str, state_value: str) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                (state_key, state_value, now),
            )

    def delete_runtime_state(self, state_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM runtime_state WHERE state_key = ?", (state_key,))

    def get_telegram_admin_chat(self, chat_id: str) -> TelegramAdminChat | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_admin_chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return self._telegram_admin_chat_from_row(row) if row else None

    def upsert_telegram_admin_chat(
        self,
        *,
        chat_id: str,
        auto_refresh_enabled: bool | None = None,
        dashboard_message_id: str | None = None,
        last_dashboard_sent_at: str | None = None,
        last_dashboard_hash: str | None = None,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM telegram_admin_chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if current:
                conn.execute(
                    """
                    UPDATE telegram_admin_chats
                    SET auto_refresh_enabled = ?,
                        dashboard_message_id = ?,
                        last_dashboard_sent_at = ?,
                        last_dashboard_hash = ?,
                        updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (
                        int(auto_refresh_enabled) if auto_refresh_enabled is not None else int(current["auto_refresh_enabled"]),
                        dashboard_message_id if dashboard_message_id is not None else current["dashboard_message_id"],
                        last_dashboard_sent_at if last_dashboard_sent_at is not None else current["last_dashboard_sent_at"],
                        last_dashboard_hash if last_dashboard_hash is not None else current["last_dashboard_hash"],
                        now,
                        chat_id,
                    ),
                )
                return
            conn.execute(
                """
                INSERT INTO telegram_admin_chats (
                    chat_id, auto_refresh_enabled, dashboard_message_id, last_dashboard_sent_at, last_dashboard_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    1 if auto_refresh_enabled else 0,
                    dashboard_message_id,
                    last_dashboard_sent_at,
                    last_dashboard_hash,
                    now,
                    now,
                ),
            )

    def list_telegram_admin_chats(self, *, auto_refresh_enabled: bool | None = None) -> list[TelegramAdminChat]:
        with self._connect() as conn:
            if auto_refresh_enabled is None:
                rows = conn.execute(
                    "SELECT * FROM telegram_admin_chats ORDER BY updated_at DESC, chat_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM telegram_admin_chats
                    WHERE auto_refresh_enabled = ?
                    ORDER BY updated_at DESC, chat_id
                    """,
                    (1 if auto_refresh_enabled else 0,),
                ).fetchall()
        return [self._telegram_admin_chat_from_row(row) for row in rows]

    def get_telegram_admin_session(self, chat_id: str) -> TelegramAdminSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_admin_sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return self._telegram_admin_session_from_row(row) if row else None

    def save_telegram_admin_session(
        self,
        *,
        chat_id: str,
        mode: str,
        step: str,
        student_id: int | None,
        payload_json: str,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_admin_sessions (
                    chat_id, mode, step, student_id, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    mode = excluded.mode,
                    step = excluded.step,
                    student_id = excluded.student_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (chat_id, mode, step, student_id, payload_json, now, now),
            )

    def clear_telegram_admin_session(self, chat_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM telegram_admin_sessions WHERE chat_id = ?", (chat_id,))

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
            existing_rows = conn.execute(
                """
                SELECT *
                FROM lecture_events
                WHERE student_id = ? AND event_date = ?
                """,
                (student_id, event_date.isoformat()),
            ).fetchall()
            preserved_events = {
                (
                    row["slot_label"],
                    row["start_time"],
                    row["end_time"],
                    int(row["is_break"]),
                ): row
                for row in existing_rows
            }
            conn.execute(
                "DELETE FROM lecture_events WHERE student_id = ? AND event_date = ?",
                (student_id, event_date.isoformat()),
            )
            for slot in slots:
                default_check_after = None
                check_after = None
                if slot.end_time:
                    end_dt = datetime.combine(event_date, slot.end_time)
                    default_check_after = end_dt + timedelta(minutes=grace_minutes)
                    check_after = default_check_after
                status = "scheduled"
                status_recorded_at = None
                note = slot.note

                preserved = preserved_events.get(
                    (
                        slot.slot_label,
                        time_to_iso(slot.start_time),
                        time_to_iso(slot.end_time),
                        1 if slot.is_break else 0,
                    )
                )
                if preserved and preserved["status"] in {
                    "notified_present",
                    "notified_absent",
                    "notified_unmarked",
                }:
                    status = preserved["status"]
                    if status == "notified_unmarked":
                        check_after = datetime_from_iso(preserved["check_after"]) or default_check_after
                    else:
                        check_after = None
                    status_recorded_at = datetime_from_iso(preserved["status_recorded_at"])
                    if preserved["note"]:
                        note = preserved["note"]
                conn.execute(
                    """
                    INSERT INTO lecture_events (
                        student_id, event_date, subject_key, subject_name, teacher_name,
                        slot_label, raw_cell, start_time, end_time, is_break, status,
                        check_after, status_recorded_at, note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        status,
                        datetime_to_iso(check_after),
                        datetime_to_iso(status_recorded_at),
                        note,
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

    def get_pending_lecture_events(self) -> list[LectureEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM lecture_events
                WHERE is_break = 0
                  AND check_after IS NOT NULL
                  AND status IN ('scheduled', 'notified_unmarked')
                ORDER BY student_id, event_date, start_time
                """
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

    def get_lecture_events_between(
        self,
        student_id: int,
        start_date: date,
        end_date: date,
    ) -> list[LectureEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM lecture_events
                WHERE student_id = ?
                  AND event_date >= ?
                  AND event_date <= ?
                ORDER BY event_date, start_time, slot_label, id
                """,
                (student_id, start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [self._lecture_event_from_row(row) for row in rows]

    def has_lecture_events_since(self, student_id: int, since_date: date) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM lecture_events
                WHERE student_id = ?
                  AND event_date >= ?
                LIMIT 1
                """,
                (student_id, since_date.isoformat()),
            ).fetchone()
        return row is not None

    def get_latest_marked_event_for_subject(
        self,
        *,
        student_id: int,
        subject_key: str,
        subject_name: str,
        since_date: date,
    ) -> LectureEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM lecture_events
                WHERE student_id = ?
                  AND event_date >= ?
                  AND status IN ('notified_present', 'notified_absent')
                  AND (subject_key = ? OR subject_name = ?)
                ORDER BY event_date DESC, status_recorded_at DESC, start_time DESC, id DESC
                LIMIT 1
                """,
                (student_id, since_date.isoformat(), subject_key, subject_name),
            ).fetchone()
        return self._lecture_event_from_row(row) if row else None

    def mark_event_status(
        self,
        event_id: int,
        status: str,
        *,
        next_check_after: datetime | None = None,
        status_recorded_at: datetime | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE lecture_events
                SET status = ?, check_after = ?, status_recorded_at = COALESCE(?, status_recorded_at)
                WHERE id = ?
                """,
                (status, datetime_to_iso(next_check_after), datetime_to_iso(status_recorded_at), event_id),
            )

    def get_next_pending_lecture_event(self, student_id: int) -> LectureEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM lecture_events
                WHERE student_id = ?
                  AND is_break = 0
                  AND check_after IS NOT NULL
                  AND status IN ('scheduled', 'notified_unmarked')
                ORDER BY check_after, event_date, start_time, id
                LIMIT 1
                """,
                (student_id,),
            ).fetchone()
        return self._lecture_event_from_row(row) if row else None

    def get_latest_recorded_lecture_event(self, student_id: int) -> LectureEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM lecture_events
                WHERE student_id = ?
                  AND is_break = 0
                  AND status_recorded_at IS NOT NULL
                ORDER BY status_recorded_at DESC, id DESC
                LIMIT 1
                """,
                (student_id,),
            ).fetchone()
        return self._lecture_event_from_row(row) if row else None

    def update_lecture_event_assignment(
        self,
        event_id: int,
        *,
        subject_key: str,
        subject_name: str,
        teacher_name: str,
        raw_cell: str,
        note: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE lecture_events
                SET subject_key = ?, subject_name = ?, teacher_name = ?, raw_cell = ?, note = ?
                WHERE id = ?
                """,
                (subject_key, subject_name, teacher_name, raw_cell, note, event_id),
            )

    def get_daily_attendance_report(
        self,
        student_id: int,
        event_date: date,
    ) -> DailyAttendanceReport | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM daily_attendance_reports
                WHERE student_id = ? AND event_date = ?
                """,
                (student_id, event_date.isoformat()),
            ).fetchone()
        return self._daily_attendance_report_from_row(row) if row else None

    def upsert_daily_attendance_report(
        self,
        *,
        student_id: int,
        event_date: date,
        total_lectures: int,
        marked_count: int,
        present_count: int,
        absent_count: int,
        unmarked_count: int,
        report_body: str,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_attendance_reports (
                    student_id, event_date, total_lectures, marked_count, present_count,
                    absent_count, unmarked_count, report_body, sent_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(student_id, event_date) DO UPDATE SET
                    total_lectures = excluded.total_lectures,
                    marked_count = excluded.marked_count,
                    present_count = excluded.present_count,
                    absent_count = excluded.absent_count,
                    unmarked_count = excluded.unmarked_count,
                    report_body = excluded.report_body,
                    sent_at = excluded.sent_at,
                    updated_at = excluded.updated_at
                """,
                (
                    student_id,
                    event_date.isoformat(),
                    total_lectures,
                    marked_count,
                    present_count,
                    absent_count,
                    unmarked_count,
                    report_body,
                    now,
                    now,
                    now,
                ),
            )

    def get_substitution_alert_keys(self, student_id: int, event_date: date) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT alert_key FROM substitution_alerts
                WHERE student_id = ? AND event_date = ?
                """,
                (student_id, event_date.isoformat()),
            ).fetchall()
        return {str(row["alert_key"]) for row in rows}

    def count_substitution_alerts_between(
        self,
        student_id: int,
        start_date: date,
        end_date: date,
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count_value
                FROM substitution_alerts
                WHERE student_id = ?
                  AND event_date >= ?
                  AND event_date <= ?
                """,
                (student_id, start_date.isoformat(), end_date.isoformat()),
            ).fetchone()
        return int(row["count_value"]) if row else 0

    def upsert_substitution_alert(
        self,
        *,
        student_id: int,
        event_date: date,
        alert_key: str,
        period: str,
        time_text: str,
        subject_name: str,
        teacher_name: str,
        end_time_text: str,
        source: str,
        notified_at: str | None,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO substitution_alerts (
                    student_id, event_date, alert_key, period, time_text, subject_name,
                    teacher_name, end_time_text, source, notified_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(student_id, event_date, alert_key) DO UPDATE SET
                    period = excluded.period,
                    time_text = excluded.time_text,
                    subject_name = excluded.subject_name,
                    teacher_name = excluded.teacher_name,
                    end_time_text = excluded.end_time_text,
                    source = excluded.source,
                    notified_at = COALESCE(excluded.notified_at, substitution_alerts.notified_at),
                    updated_at = excluded.updated_at
                """,
                (
                    student_id,
                    event_date.isoformat(),
                    alert_key,
                    period,
                    time_text,
                    subject_name,
                    teacher_name,
                    end_time_text,
                    source,
                    notified_at,
                    now,
                    now,
                ),
            )

    def has_notification_event(
        self,
        student_id: int,
        category: str,
        notification_key: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM notification_events
                WHERE student_id = ? AND category = ? AND notification_key = ?
                """,
                (student_id, category, notification_key),
            ).fetchone()
        return row is not None

    def upsert_notification_event(
        self,
        *,
        student_id: int,
        category: str,
        notification_key: str,
        message_text: str,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notification_events (
                    student_id, category, notification_key, message_text, notified_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(student_id, category, notification_key) DO UPDATE SET
                    message_text = excluded.message_text,
                    notified_at = excluded.notified_at,
                    updated_at = excluded.updated_at
                """,
                (
                    student_id,
                    category,
                    notification_key,
                    message_text,
                    now,
                    now,
                    now,
                ),
            )

    def try_claim_job_slot(self, job_name: str, slot_key: str) -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO scheduler_job_slots (job_name, slot_key, claimed_at)
                    VALUES (?, ?, ?)
                    """,
                    (job_name, slot_key, utcnow_iso()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def claim_outbound_message(
        self,
        *,
        idempotency_key: str,
        student_id: int,
        channel: str,
        recipient: str,
        category: str,
        message_kind: str,
        title: str,
        body: str,
    ) -> bool:
        now = utcnow_iso()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT status
                FROM outbound_messages
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                """
                INSERT INTO outbound_messages (
                    idempotency_key, student_id, channel, recipient, category, message_kind, title, body,
                    status, claimed_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    student_id,
                    channel,
                    recipient,
                    category,
                    message_kind,
                    title,
                    body,
                    "claimed",
                    now,
                    now,
                    now,
                ),
            )
        return True

    def mark_outbound_message_sent(self, idempotency_key: str, provider_sid: str) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'sent',
                    provider_sid = ?,
                    delivery_status = COALESCE(delivery_status, 'accepted'),
                    attempt_count = attempt_count + 1,
                    next_retry_at = NULL,
                    dead_lettered_at = NULL,
                    sent_at = ?,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (provider_sid, now, now, idempotency_key),
            )

    def mark_outbound_message_failed(
        self,
        idempotency_key: str,
        error_text: str,
        *,
        retry_limit: int,
        retry_backoff_seconds: int,
    ) -> None:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT attempt_count
                FROM outbound_messages
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if not row:
                return
            attempt_count = int(row["attempt_count"]) + 1
            status = "dead_letter" if attempt_count >= retry_limit else "failed"
            next_retry_at = None
            dead_lettered_at = None
            if status == "failed":
                next_retry_at = (now_dt + timedelta(seconds=retry_backoff_seconds * attempt_count)).isoformat()
            else:
                dead_lettered_at = now
            conn.execute(
                """
                UPDATE outbound_messages
                SET status = ?,
                    delivery_error_message = ?,
                    attempt_count = ?,
                    next_retry_at = ?,
                    dead_lettered_at = ?,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    status,
                    error_text,
                    attempt_count,
                    next_retry_at,
                    dead_lettered_at,
                    now,
                    idempotency_key,
                ),
            )

    def get_retryable_outbound_messages(self, *, now: datetime, limit: int = 20) -> list[OutboundMessageRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM outbound_messages
                WHERE status = 'failed'
                  AND next_retry_at IS NOT NULL
                  AND next_retry_at <= ?
                ORDER BY next_retry_at, updated_at, idempotency_key
                LIMIT ?
                """,
                (datetime_to_iso(now), limit),
            ).fetchall()
        return [self._outbound_message_from_row(row) for row in rows]

    def try_claim_retry_outbound_message(self, idempotency_key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'claimed',
                    updated_at = ?
                WHERE idempotency_key = ?
                  AND status = 'failed'
                """,
                (utcnow_iso(), idempotency_key),
            )
        return cursor.rowcount > 0

    def try_claim_dead_letter_outbound_message(self, idempotency_key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE outbound_messages
                SET status = 'claimed',
                    updated_at = ?
                WHERE idempotency_key = ?
                  AND status = 'dead_letter'
                """,
                (utcnow_iso(), idempotency_key),
            )
        return cursor.rowcount > 0

    def get_dead_letter_messages(self, limit: int = 20) -> list[OutboundMessageRecord]:
        return self.get_dead_letter_messages_for_student(student_id=None, limit=limit)

    def get_dead_letter_messages_for_student(self, student_id: int | None, limit: int = 20) -> list[OutboundMessageRecord]:
        with self._connect() as conn:
            if student_id is None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM outbound_messages
                    WHERE status = 'dead_letter'
                    ORDER BY updated_at DESC, idempotency_key DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM outbound_messages
                    WHERE status = 'dead_letter' AND student_id = ?
                    ORDER BY updated_at DESC, idempotency_key DESC
                    LIMIT ?
                    """,
                    (student_id, limit),
                ).fetchall()
        return [self._outbound_message_from_row(row) for row in rows]

    def get_outbound_queue_summary(self) -> dict[str, int]:
        return self.get_outbound_queue_summary_for_student(student_id=None)

    def get_outbound_queue_summary_for_student(self, student_id: int | None) -> dict[str, int]:
        with self._connect() as conn:
            if student_id is None:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count_value
                    FROM outbound_messages
                    GROUP BY status
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS count_value
                    FROM outbound_messages
                    WHERE student_id = ?
                    GROUP BY status
                    """,
                    (student_id,),
                ).fetchall()
        summary = {"claimed": 0, "sent": 0, "failed": 0, "dead_letter": 0}
        for row in rows:
            summary[str(row["status"])] = int(row["count_value"])
        return summary

    def insert_message_history(
        self,
        *,
        student_id: int,
        channel: str,
        recipient: str,
        category: str,
        message_kind: str,
        provider_sid: str,
        title: str,
        body: str,
        idempotency_key: str | None = None,
        delivery_status: str | None = None,
        delivery_error_code: int | None = None,
        delivery_error_message: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO message_history (
                    student_id, channel, recipient, category, message_kind, provider_sid, title, body,
                    idempotency_key, delivery_status, delivery_error_code, delivery_error_message,
                    status_updated_at, sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student_id,
                    channel,
                    recipient,
                    category,
                    message_kind,
                    provider_sid,
                    title,
                    body,
                    idempotency_key,
                    delivery_status,
                    delivery_error_code,
                    delivery_error_message,
                    utcnow_iso(),
                    utcnow_iso(),
                ),
            )

    def update_delivery_status_by_provider_sid(
        self,
        *,
        provider_sid: str,
        delivery_status: str,
        delivery_error_code: int | None,
        delivery_error_message: str | None,
    ) -> bool:
        updated_at = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_messages
                SET delivery_status = ?,
                    delivery_error_code = ?,
                    delivery_error_message = ?,
                    updated_at = ?
                WHERE provider_sid = ?
                """,
                (
                    delivery_status,
                    delivery_error_code,
                    delivery_error_message,
                    updated_at,
                    provider_sid,
                ),
            )
            history_cursor = conn.execute(
                """
                UPDATE message_history
                SET delivery_status = ?,
                    delivery_error_code = ?,
                    delivery_error_message = ?,
                    status_updated_at = ?
                WHERE provider_sid = ?
                """,
                (
                    delivery_status,
                    delivery_error_code,
                    delivery_error_message,
                    updated_at,
                    provider_sid,
                ),
            )
        return history_cursor.rowcount > 0

    def get_recent_message_history(self, limit: int | None = 50) -> list[MessageHistoryRecord]:
        with self._connect() as conn:
            query = """
                SELECT
                    mh.*,
                    s.student_label,
                    s.whatsapp_number,
                    s.telegram_chat_id,
                    s.email_address
                FROM message_history mh
                JOIN students s ON s.id = mh.student_id
                ORDER BY mh.sent_at DESC, mh.id DESC
            """
            params: tuple[object, ...] = ()
            if limit is not None:
                query += "\nLIMIT ?"
                params = (limit,)
            rows = conn.execute(query, params).fetchall()
        return [self._message_history_from_row(row) for row in rows]

    def get_message_history_page(
        self,
        *,
        query: str = "",
        channel: str = "",
        category: str = "",
        student_id: int | None = None,
        limit: int,
        offset: int,
    ) -> list[MessageHistoryRecord]:
        where_sql, params = self._message_history_where_clause(query=query, channel=channel, category=category, student_id=student_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    mh.*,
                    s.student_label,
                    s.whatsapp_number,
                    s.telegram_chat_id,
                    s.email_address
                FROM message_history mh
                JOIN students s ON s.id = mh.student_id
                {where_sql}
                ORDER BY mh.sent_at DESC, mh.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [self._message_history_from_row(row) for row in rows]

    def count_message_history(
        self,
        *,
        query: str = "",
        channel: str = "",
        category: str = "",
        student_id: int | None = None,
    ) -> int:
        where_sql, params = self._message_history_where_clause(query=query, channel=channel, category=category, student_id=student_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count_value
                FROM message_history mh
                JOIN students s ON s.id = mh.student_id
                {where_sql}
                """,
                params,
            ).fetchone()
        return int(row["count_value"]) if row else 0

    def get_message_history_filter_options(self) -> dict[str, list[str]]:
        with self._connect() as conn:
            channel_rows = conn.execute(
                """
                SELECT DISTINCT channel
                FROM message_history
                WHERE channel IS NOT NULL AND TRIM(channel) != ''
                ORDER BY channel
                """
            ).fetchall()
            category_rows = conn.execute(
                """
                SELECT DISTINCT category
                FROM message_history
                WHERE category IS NOT NULL AND TRIM(category) != ''
                ORDER BY category
                """
            ).fetchall()
        return {
            "channels": [str(row["channel"]) for row in channel_rows],
            "categories": [str(row["category"]) for row in category_rows],
        }

    def insert_admin_audit_log(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        details: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_audit_log (
                    actor, action, target_type, target_id, details, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (actor, action, target_type, target_id, details, utcnow_iso()),
            )

    def insert_application_request(
        self,
        *,
        applicant_name: str,
        student_label: str,
        user_name: str,
        password_encrypted: str,
        site_login_username: str,
        site_password_hash: str,
        reg_id: str | None,
        whatsapp_number: str,
        telegram_chat_id: str,
        timezone: str,
        note: str | None,
        created_from_ip: str | None,
        status: str = "new",
    ) -> int:
        now = utcnow_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO application_requests (
                    applicant_name, student_label, user_name, password_encrypted, site_login_username, site_password_hash, reg_id,
                    whatsapp_number, telegram_chat_id, timezone, note, created_from_ip,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    applicant_name,
                    student_label,
                    user_name,
                    password_encrypted,
                    site_login_username,
                    site_password_hash,
                    reg_id,
                    whatsapp_number,
                    telegram_chat_id,
                    timezone,
                    note,
                    created_from_ip,
                    status,
                    now,
                    now,
                ),
            )
            request_id = int(cursor.lastrowid)
        return request_id

    def list_application_requests(self, limit: int = 20) -> list[ApplicationRequest]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM application_requests
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._application_request_from_row(row) for row in rows]

    def get_application_request(self, application_id: int) -> ApplicationRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM application_requests
                WHERE id = ?
                """,
                (application_id,),
            ).fetchone()
        return self._application_request_from_row(row) if row else None

    def get_application_request_by_site_login_username(self, normalized_username: str) -> ApplicationRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM application_requests
                WHERE LOWER(site_login_username) = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (normalized_username.strip().lower(),),
            ).fetchone()
        return self._application_request_from_row(row) if row else None

    def update_application_request_status(self, application_id: int, status: str) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            current = conn.execute(
                "SELECT id FROM application_requests WHERE id = ?",
                (application_id,),
            ).fetchone()
            if not current:
                raise StudentValidationError("Application request not found.")
            conn.execute(
                """
                UPDATE application_requests
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, application_id),
            )

    def update_application_request_access(
        self,
        *,
        application_id: int,
        site_login_username: str,
        site_password_hash: str | None = None,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            current = conn.execute(
                "SELECT site_password_hash FROM application_requests WHERE id = ?",
                (application_id,),
            ).fetchone()
            if not current:
                raise StudentValidationError("Application request not found.")
            conn.execute(
                """
                UPDATE application_requests
                SET site_login_username = ?,
                    site_password_hash = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    site_login_username,
                    site_password_hash if site_password_hash is not None else current["site_password_hash"],
                    now,
                    application_id,
                ),
            )

    def delete_application_request(self, application_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM application_requests WHERE id = ?",
                (application_id,),
            )
        return bool(cursor.rowcount)

    def update_student_registration(
        self,
        *,
        student_id: int,
        reg_id: str | None = None,
        student_name: str | None = None,
    ) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            current = conn.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
            if not current:
                raise StudentValidationError("Student not found.")
            conn.execute(
                """
                UPDATE students
                SET reg_id = COALESCE(?, reg_id),
                    student_name = COALESCE(?, student_name),
                    updated_at = ?
                WHERE id = ?
                """,
                (reg_id, student_name, now, student_id),
            )

    def get_recent_admin_audit_log(self, limit: int | None = 50) -> list[AdminAuditRecord]:
        with self._connect() as conn:
            query = """
                SELECT *
                FROM admin_audit_log
                ORDER BY created_at DESC, id DESC
            """
            params: tuple[object, ...] = ()
            if limit is not None:
                query += "\nLIMIT ?"
                params = (limit,)
            rows = conn.execute(query, params).fetchall()
        return [self._admin_audit_from_row(row) for row in rows]

    def get_admin_audit_log_page(
        self,
        *,
        query: str = "",
        action: str = "",
        limit: int,
        offset: int,
    ) -> list[AdminAuditRecord]:
        where_sql, params = self._admin_audit_where_clause(query=query, action=action)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM admin_audit_log
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [self._admin_audit_from_row(row) for row in rows]

    def count_admin_audit_log(
        self,
        *,
        query: str = "",
        action: str = "",
    ) -> int:
        where_sql, params = self._admin_audit_where_clause(query=query, action=action)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count_value
                FROM admin_audit_log
                {where_sql}
                """,
                params,
            ).fetchone()
        return int(row["count_value"]) if row else 0

    def get_admin_audit_filter_options(self) -> dict[str, list[str]]:
        with self._connect() as conn:
            action_rows = conn.execute(
                """
                SELECT DISTINCT action
                FROM admin_audit_log
                WHERE action IS NOT NULL AND TRIM(action) != ''
                ORDER BY action
                """
            ).fetchall()
        return {"actions": [str(row["action"]) for row in action_rows]}

    def _message_history_where_clause(
        self,
        *,
        query: str,
        channel: str,
        category: str,
        student_id: int | None,
    ) -> tuple[str, tuple[object, ...]]:
        conditions: list[str] = []
        params: list[object] = []
        if student_id is not None:
            conditions.append("mh.student_id = ?")
            params.append(student_id)
        if channel:
            conditions.append("LOWER(mh.channel) = ?")
            params.append(channel.strip().lower())
        if category:
            conditions.append("LOWER(mh.category) = ?")
            params.append(category.strip().lower())
        if query:
            conditions.append(
                """
                LOWER(
                    COALESCE(s.student_label, '') || ' ' ||
                    COALESCE(mh.channel, '') || ' ' ||
                    COALESCE(mh.recipient, '') || ' ' ||
                    COALESCE(mh.category, '') || ' ' ||
                    COALESCE(mh.message_kind, '') || ' ' ||
                    COALESCE(mh.title, '') || ' ' ||
                    COALESCE(mh.body, '') || ' ' ||
                    COALESCE(mh.sent_at, '')
                ) LIKE ?
                """
            )
            params.append(f"%{query.strip().lower()}%")
        if not conditions:
            return "", ()
        return "WHERE " + " AND ".join(conditions), tuple(params)

    def _admin_audit_where_clause(
        self,
        *,
        query: str,
        action: str,
    ) -> tuple[str, tuple[object, ...]]:
        conditions: list[str] = []
        params: list[object] = []
        if action:
            conditions.append("LOWER(action) = ?")
            params.append(action.strip().lower())
        if query:
            conditions.append(
                """
                LOWER(
                    COALESCE(actor, '') || ' ' ||
                    COALESCE(action, '') || ' ' ||
                    COALESCE(target_type, '') || ' ' ||
                    COALESCE(target_id, '') || ' ' ||
                    COALESCE(details, '') || ' ' ||
                    COALESCE(created_at, '')
                ) LIKE ?
                """
            )
            params.append(f"%{query.strip().lower()}%")
        if not conditions:
            return "", ()
        return "WHERE " + " AND ".join(conditions), tuple(params)

    def _student_from_row(self, row: sqlite3.Row) -> Student:
        return Student(
            id=row["id"],
            student_label=row["student_label"],
            user_name=row["user_name"],
            password_encrypted=row["password_encrypted"],
            site_login_username=row["site_login_username"],
            site_password_hash=row["site_password_hash"],
            site_password_updated_at=row["site_password_updated_at"],
            whatsapp_number=row["whatsapp_number"],
            telegram_chat_id=row["telegram_chat_id"],
            email_address=row["email_address"],
            enabled=bool(row["enabled"]),
            notification_channel_mode=row["notification_channel_mode"] or "telegram_only",
            disabled_actions_json=row["disabled_actions_json"] or "[]",
            timezone=row["timezone"],
            reg_id=row["reg_id"],
            student_name=row["student_name"],
            session_cookies=row["session_cookies"],
            session_updated_at=row["session_updated_at"],
            last_login_status=row["last_login_status"],
            erp_status_text=row["erp_status_text"],
            erp_status_updated_at=row["erp_status_updated_at"],
            last_bot_activity_text=row["last_bot_activity_text"],
            last_erp_sync_at=row["last_erp_sync_at"],
            last_bot_action_at=row["last_bot_action_at"],
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
            status_recorded_at=datetime_from_iso(row["status_recorded_at"]),
            note=row["note"],
        )

    def _daily_attendance_report_from_row(self, row: sqlite3.Row) -> DailyAttendanceReport:
        return DailyAttendanceReport(
            student_id=row["student_id"],
            event_date=date.fromisoformat(row["event_date"]),
            total_lectures=row["total_lectures"],
            marked_count=row["marked_count"],
            present_count=row["present_count"],
            absent_count=row["absent_count"],
            unmarked_count=row["unmarked_count"],
            report_body=row["report_body"],
            sent_at=row["sent_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _message_history_from_row(self, row: sqlite3.Row) -> MessageHistoryRecord:
        return MessageHistoryRecord(
            id=row["id"],
            student_id=row["student_id"],
            student_label=row["student_label"],
            whatsapp_number=row["whatsapp_number"],
            telegram_chat_id=row["telegram_chat_id"],
            email_address=row["email_address"],
            channel=row["channel"],
            recipient=row["recipient"],
            category=row["category"],
            message_kind=row["message_kind"],
            provider_sid=row["provider_sid"],
            title=row["title"],
            body=row["body"],
            idempotency_key=row["idempotency_key"],
            delivery_status=row["delivery_status"],
            delivery_error_code=row["delivery_error_code"],
            delivery_error_message=row["delivery_error_message"],
            status_updated_at=row["status_updated_at"],
            sent_at=row["sent_at"],
        )

    def _outbound_message_from_row(self, row: sqlite3.Row) -> OutboundMessageRecord:
        return OutboundMessageRecord(
            idempotency_key=row["idempotency_key"],
            student_id=row["student_id"],
            channel=row["channel"],
            recipient=row["recipient"],
            category=row["category"],
            message_kind=row["message_kind"],
            title=row["title"],
            body=row["body"],
            status=row["status"],
            provider_sid=row["provider_sid"],
            delivery_status=row["delivery_status"],
            delivery_error_code=row["delivery_error_code"],
            delivery_error_message=row["delivery_error_message"],
            attempt_count=row["attempt_count"],
            next_retry_at=row["next_retry_at"],
            dead_lettered_at=row["dead_lettered_at"],
            claimed_at=row["claimed_at"],
            sent_at=row["sent_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _admin_audit_from_row(self, row: sqlite3.Row) -> AdminAuditRecord:
        return AdminAuditRecord(
            id=row["id"],
            actor=row["actor"],
            action=row["action"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            details=row["details"],
            created_at=row["created_at"],
        )

    def _application_request_from_row(self, row: sqlite3.Row) -> ApplicationRequest:
        return ApplicationRequest(
            id=row["id"],
            applicant_name=row["applicant_name"],
            student_label=row["student_label"],
            user_name=row["user_name"],
            password_encrypted=row["password_encrypted"],
            site_login_username=row["site_login_username"],
            site_password_hash=row["site_password_hash"],
            reg_id=row["reg_id"],
            whatsapp_number=row["whatsapp_number"],
            telegram_chat_id=row["telegram_chat_id"],
            timezone=row["timezone"],
            note=row["note"],
            created_from_ip=row["created_from_ip"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _telegram_admin_chat_from_row(self, row: sqlite3.Row) -> TelegramAdminChat:
        return TelegramAdminChat(
            chat_id=row["chat_id"],
            auto_refresh_enabled=bool(row["auto_refresh_enabled"]),
            dashboard_message_id=row["dashboard_message_id"],
            last_dashboard_sent_at=row["last_dashboard_sent_at"],
            last_dashboard_hash=row["last_dashboard_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _telegram_admin_session_from_row(self, row: sqlite3.Row) -> TelegramAdminSession:
        return TelegramAdminSession(
            chat_id=row["chat_id"],
            mode=row["mode"],
            step=row["step"],
            student_id=row["student_id"],
            payload_json=row["payload_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )
