from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any


@dataclass
class Student:
    id: int
    student_label: str
    user_name: str
    password_encrypted: str
    site_login_username: str
    site_password_hash: str
    site_password_updated_at: str | None
    telegram_chat_id: str
    enabled: bool
    notification_channel_mode: str
    disabled_actions_json: str
    timezone: str
    reg_id: str | None
    student_name: str | None
    session_cookies: str | None
    session_updated_at: str | None
    last_login_status: str | None
    erp_status_text: str | None
    erp_status_updated_at: str | None
    last_bot_activity_text: str | None
    last_erp_sync_at: str | None
    last_bot_action_at: str | None
    created_at: str
    updated_at: str


@dataclass
class PendingLogin:
    student_id: int
    request_verification_token: str
    hdn_msg: str
    check_online: str
    client_ip: str
    captcha_data_url: str
    cookies_json: str
    created_at: str


@dataclass
class SubjectAttendance:
    subject_key: str
    subject_name: str
    subject_code: str
    teacher_name: str
    total_lecture: int
    total_present: int
    percentage: str
    raw: dict[str, Any]


@dataclass
class LectureSlot:
    slot_label: str
    subject_key: str
    subject_name: str
    teacher_name: str
    raw_cell: str
    start_time: time | None
    end_time: time | None
    is_break: bool
    note: str = ""


@dataclass
class Substitution:
    period: str
    time_text: str
    date_text: str
    original_subject: str
    original_teacher: str
    substitute_subject: str
    substitute_teacher: str
    raw: dict[str, Any]


@dataclass
class LectureEvent:
    id: int
    student_id: int
    event_date: date
    subject_key: str
    subject_name: str
    teacher_name: str
    slot_label: str
    raw_cell: str
    start_time: time | None
    end_time: time | None
    is_break: bool
    status: str
    check_after: datetime | None
    status_recorded_at: datetime | None
    note: str


@dataclass
class DailyAttendanceReport:
    student_id: int
    event_date: date
    total_lectures: int
    marked_count: int
    present_count: int
    absent_count: int
    unmarked_count: int
    report_body: str
    sent_at: str
    created_at: str
    updated_at: str


@dataclass
class MessageHistoryRecord:
    id: int
    student_id: int
    student_label: str
    telegram_chat_id: str
    channel: str
    recipient: str
    category: str
    message_kind: str
    provider_sid: str
    title: str
    body: str
    idempotency_key: str | None
    delivery_status: str | None
    delivery_error_code: int | None
    delivery_error_message: str | None
    status_updated_at: str | None
    sent_at: str


@dataclass
class OutboundMessageRecord:
    idempotency_key: str
    student_id: int
    channel: str
    recipient: str
    category: str
    message_kind: str
    title: str
    body: str
    status: str
    provider_sid: str | None
    delivery_status: str | None
    delivery_error_code: int | None
    delivery_error_message: str | None
    attempt_count: int
    next_retry_at: str | None
    dead_lettered_at: str | None
    claimed_at: str
    sent_at: str | None
    created_at: str
    updated_at: str


@dataclass
class AdminAuditRecord:
    id: int
    actor: str
    action: str
    target_type: str
    target_id: str
    details: str
    created_at: str


@dataclass
class ApplicationRequest:
    id: int
    applicant_name: str
    student_label: str
    user_name: str
    password_encrypted: str
    site_login_username: str
    site_password_hash: str
    reg_id: str | None
    telegram_chat_id: str
    timezone: str
    note: str | None
    created_from_ip: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass
class TelegramAdminChat:
    chat_id: str
    auto_refresh_enabled: bool
    dashboard_message_id: str | None
    last_dashboard_sent_at: str | None
    last_dashboard_hash: str | None
    created_at: str
    updated_at: str


@dataclass
class TelegramAdminSession:
    chat_id: str
    mode: str
    step: str
    student_id: int | None
    payload_json: str
    created_at: str
    updated_at: str
