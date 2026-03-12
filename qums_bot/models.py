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
    whatsapp_number: str
    enabled: bool
    timezone: str
    reg_id: str | None
    student_name: str | None
    session_cookies: str | None
    session_updated_at: str | None
    last_login_status: str | None
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
    note: str
