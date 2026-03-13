from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from typing import Any, Iterable

from bs4 import BeautifulSoup

from .models import LectureSlot, SubjectAttendance, Substitution


WEEKDAY_ALIASES = {
    0: {"monday", "mon"},
    1: {"tuesday", "tue", "tues"},
    2: {"wednesday", "wed"},
    3: {"thursday", "thu", "thur", "thurs"},
    4: {"friday", "fri"},
    5: {"saturday", "sat"},
    6: {"sunday", "sun"},
}

BREAK_WORDS = {"break", "lunch", "recess"}
NO_CLASS_WORDS = {
    "holiday",
    "no class",
    "no classes",
    "off day",
    "class cancelled",
    "class canceled",
    "lecture cancelled",
    "lecture canceled",
    "not scheduled",
    "no lecture",
    "no lectures",
}


def parse_json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    if isinstance(parsed, str) and parsed and parsed[0] in "[{":
        try:
            return json.loads(parsed)
        except json.JSONDecodeError:
            return parsed
    return parsed


def extract_login_state(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")

    def value(selector: str, default: str = "") -> str:
        node = soup.select_one(selector)
        if not node:
            return default
        return (node.get("value") or "").strip()

    captcha = soup.select_one("#imgPhoto")
    hdn_msg = value("#hdnMsg")
    return {
        "request_verification_token": value('input[name="__RequestVerificationToken"]'),
        "hdn_msg": hdn_msg,
        "check_online": value("#checkOnline", "0"),
        "client_ip": "~~~~~" if hdn_msg == "QGC" else "",
        "captcha_data_url": (captcha.get("src") or "").strip() if captcha else "",
    }


def parse_student_detail_response(payload: dict[str, Any]) -> dict[str, Any] | None:
    rows = parse_json_value(payload.get("state"), [])
    if not rows:
        return None
    return rows[0]


def parse_timetable_slots(payload: dict[str, Any], target_date: date) -> list[LectureSlot]:
    data_str = payload.get("timetable") or payload.get("state")
    rows = parse_json_value(data_str, [])
    if not rows:
        return []

    columns = [col.strip() for col in str(payload.get("col") or "").split(",") if col.strip()]
    if not columns and isinstance(rows[0], dict):
        columns = [str(key).strip() for key in rows[0].keys()]

    if _has_weekday_columns(columns):
        return _parse_weekday_matrix(rows, columns, target_date)
    if _has_weekday_rows(rows, columns):
        return _parse_weekday_rows(rows, columns, target_date)
    return _parse_flat_slots(rows, columns)


def parse_substitutions(payload: dict[str, Any], target_date: date) -> list[Substitution]:
    rows = parse_json_value(payload.get("state"), [])
    substitutions: list[Substitution] = []

    for row in rows:
        date_text = (
            row.get("SubsDate")
            or row.get("SubstituteDate")
            or row.get("OrderDate")
            or row.get("ApplicableFrom")
            or row.get("Date")
            or ""
        )
        date_text = str(date_text).strip()
        substitutions.append(
            Substitution(
                period=str(row.get("Period") or row.get("PrePeriod") or "").strip(),
                time_text=str(row.get("Time") or row.get("Duration") or "").strip(),
                date_text=date_text,
                original_subject=str(row.get("Subject") or row.get("PreSubject") or "").strip(),
                original_teacher=str(row.get("Employee") or row.get("PreEmployee") or "").strip(),
                substitute_subject=str(row.get("SubsSubject") or row.get("subject") or "").strip(),
                substitute_teacher=str(row.get("SubsEmployee") or "").strip(),
                raw=row,
            )
        )

    todays = [item for item in substitutions if _date_matches(item.date_text, target_date)]
    return todays


def parse_attendance_summary(payload: dict[str, Any]) -> list[SubjectAttendance]:
    rows = parse_json_value(payload.get("state"), [])
    parsed: list[SubjectAttendance] = []
    for row in rows:
        subject_name = str(row.get("Subject") or "").strip()
        subject_code = str(row.get("SubjectCode") or "").strip()
        teacher_name = str(row.get("EMPNAME") or row.get("Employee") or "").strip()
        parsed.append(
            SubjectAttendance(
                subject_key=normalize_subject_key(subject_code or subject_name),
                subject_name=subject_name,
                subject_code=subject_code,
                teacher_name=teacher_name,
                total_lecture=to_int(row.get("TotalLecture")),
                total_present=to_int(row.get("TotalPresent")),
                percentage=str(row.get("Percentage") or "").strip(),
                raw=row,
            )
        )
    return parsed


def match_attendance_record(
    records: Iterable[SubjectAttendance],
    subject_key: str,
    subject_name: str,
) -> SubjectAttendance | None:
    subject_key_norm = normalize_subject_key(subject_key)
    subject_name_norm = normalize_subject_key(subject_name)
    items = list(records)

    for item in items:
        if item.subject_key == subject_key_norm:
            return item

    for item in items:
        item_name = normalize_subject_key(item.subject_name)
        if subject_name_norm and (subject_name_norm in item_name or item_name in subject_name_norm):
            return item

    best: SubjectAttendance | None = None
    best_score = 0.0
    subject_tokens = set(subject_name_norm.split("_"))
    for item in items:
        item_tokens = set(normalize_subject_key(item.subject_name).split("_"))
        if not subject_tokens or not item_tokens:
            continue
        score = len(subject_tokens & item_tokens) / len(subject_tokens | item_tokens)
        if score > best_score:
            best = item
            best_score = score
    return best if best_score >= 0.4 else None


def format_slot_time(slot: LectureSlot) -> str:
    if slot.start_time and slot.end_time:
        return f"{slot.start_time.strftime('%H:%M')} - {slot.end_time.strftime('%H:%M')}"
    return slot.slot_label


def normalize_subject_key(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return cleaned or "unknown_subject"


def html_to_lines(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = BeautifulSoup(text, "html.parser").get_text("\n")
    lines = [line.strip(" \t:-") for line in text.splitlines()]
    return [line for line in lines if line]


def parse_time_range(*candidates: str) -> tuple[time | None, time | None]:
    text = " ".join(part for part in candidates if part).replace(".", ":")
    patterns = [
        r"(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s*(?:-|to|TO|To|–)\s*(\d{1,2}:\d{2}\s*(?:AM|PM)?)",
        r"(\d{1,2}\s*(?:AM|PM))\s*(?:-|to|TO|To|–)\s*(\d{1,2}\s*(?:AM|PM))",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_single_time(match.group(1)), _parse_single_time(match.group(2))
    return None, None


def to_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    match = re.search(r"-?\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def _has_weekday_columns(columns: list[str]) -> bool:
    normalized = {column.strip().lower() for column in columns}
    all_names = set().union(*WEEKDAY_ALIASES.values())
    return bool(normalized & all_names)


def _row_label_column(columns: list[str]) -> str | None:
    for column in columns:
        normalized = column.strip().lower()
        if normalized in {"day/period", "days/period", "day", "days"}:
            return column
    return columns[0] if columns else None


def _has_weekday_rows(rows: list[dict[str, Any]], columns: list[str]) -> bool:
    if not rows or not columns:
        return False
    label_column = _row_label_column(columns)
    if not label_column:
        return False
    sample = str(rows[0].get(label_column) or "").strip().lower()
    all_names = set().union(*WEEKDAY_ALIASES.values())
    return sample in all_names


def _find_day_column(columns: list[str], target_date: date) -> str | None:
    aliases = WEEKDAY_ALIASES[target_date.weekday()]
    for column in columns:
        if column.strip().lower() in aliases:
            return column
    return None


def _parse_weekday_matrix(rows: list[dict[str, Any]], columns: list[str], target_date: date) -> list[LectureSlot]:
    day_column = _find_day_column(columns, target_date)
    if not day_column:
        return []

    first_column = "Day/Period" if "Day/Period" in columns else columns[0]
    slots: list[LectureSlot] = []
    for row in rows:
        slot_label = str(row.get(first_column) or "").strip()
        cell = str(row.get(day_column) or "").strip()
        slot = _build_slot(slot_label=slot_label, cell=cell)
        if slot:
            slots.append(slot)
    return _inject_break_slots(slots)


def _parse_weekday_rows(rows: list[dict[str, Any]], columns: list[str], target_date: date) -> list[LectureSlot]:
    label_column = _row_label_column(columns)
    if not label_column:
        return []

    aliases = WEEKDAY_ALIASES[target_date.weekday()]
    target_row = None
    for row in rows:
        label = str(row.get(label_column) or "").strip().lower()
        if label in aliases:
            target_row = row
            break
    if not target_row:
        return []

    slots: list[LectureSlot] = []
    for column in columns:
        if column == label_column:
            continue
        cell_value = target_row.get(column)
        if cell_value in (None, "", "null"):
            continue
        slot = _build_slot(slot_label=column, cell=str(cell_value).strip())
        if slot:
            slots.append(slot)
    return _inject_break_slots(slots)


def _parse_flat_slots(rows: list[dict[str, Any]], columns: list[str]) -> list[LectureSlot]:
    slots: list[LectureSlot] = []
    for row in rows:
        slot_label = str(row.get("Period") or row.get("Time") or row.get("Timing") or row.get("Day/Period") or "").strip()
        subject = str(row.get("Subject") or row.get("SubsSubject") or row.get("SubjectName") or "").strip()
        teacher = str(row.get("Employee") or row.get("Faculty") or row.get("EMPNAME") or row.get("SubsEmployee") or "").strip()
        time_text = str(row.get("Time") or row.get("Duration") or "").strip()
        cell = "\n".join(part for part in [subject, teacher, time_text] if part)

        # Flat ERP rows usually expose subject and teacher as separate fields.
        # Prefer those explicit values over the generic cell parser to avoid
        # accidentally merging the teacher into the subject name.
        if subject:
            start_time, end_time = parse_time_range(slot_label, time_text)
            is_no_class = _looks_like_no_class(subject)
            normalized_subject = _no_class_label(subject) if is_no_class else subject
            slot = LectureSlot(
                slot_label=slot_label or time_text or "Lecture",
                subject_key=normalize_subject_key(normalized_subject),
                subject_name=normalized_subject,
                teacher_name="" if is_no_class else teacher,
                raw_cell=cell,
                start_time=start_time,
                end_time=end_time,
                is_break=_looks_like_break(subject) or is_no_class,
                note=subject if is_no_class else "",
            )
        else:
            slot = _build_slot(slot_label=slot_label or time_text, cell=cell)
        if slot:
            slots.append(slot)
    return _inject_break_slots(slots)


def _build_slot(slot_label: str, cell: str) -> LectureSlot | None:
    combined = "\n".join(part for part in [slot_label, cell] if part).strip()
    if not combined:
        return None

    lines = html_to_lines(cell)
    if not lines:
        lines = _split_cell_entries(cell)
    if not lines and _looks_like_break(slot_label):
        lines = [slot_label]
    if not lines and _looks_like_no_class(combined):
        lines = [_no_class_label(combined)]

    is_no_class = _looks_like_no_class(combined)
    is_break = _looks_like_break(combined) or is_no_class
    if not lines and not is_break:
        return None

    start_time, end_time = parse_time_range(slot_label, cell)
    subject_name = ""
    teacher_name = ""

    parsed_entries = [_parse_subject_teacher_entry(line) for line in lines if line]
    parsed_entries = [entry for entry in parsed_entries if entry[0] or entry[1]]

    if parsed_entries:
        subjects = [entry[0] for entry in parsed_entries if entry[0]]
        teachers = [entry[1] for entry in parsed_entries if entry[1]]
        subject_name = " / ".join(subjects)
        teacher_name = " / ".join(teachers)
    else:
        for line in lines:
            if _is_time_text(line):
                continue
            if not subject_name:
                subject_name = line
                continue
            if not teacher_name:
                teacher_name = line
                break

    if is_no_class:
        subject_name = _no_class_label(subject_name or combined)
        teacher_name = ""
    elif is_break and not subject_name:
        subject_name = "Break"

    if not subject_name:
        return None

    return LectureSlot(
        slot_label=slot_label or subject_name,
        subject_key=normalize_subject_key(subject_name),
        subject_name=subject_name,
        teacher_name=teacher_name,
        raw_cell=cell,
        start_time=start_time,
        end_time=end_time,
        is_break=is_break,
        note=combined if is_no_class else "",
    )


def _looks_like_break(value: str) -> bool:
    normalized = value.lower()
    return any(word in normalized for word in BREAK_WORDS)


def _looks_like_no_class(value: str) -> bool:
    normalized = " ".join((value or "").strip().lower().split())
    return any(word in normalized for word in NO_CLASS_WORDS)


def _no_class_label(value: str) -> str:
    normalized = " ".join((value or "").strip().lower().split())
    if "holiday" in normalized:
        return "Holiday"
    if "off day" in normalized:
        return "Off Day"
    return "No Class"


def _is_time_text(value: str) -> bool:
    return bool(re.search(r"\d{1,2}[:.]\d{2}", value))


def _parse_single_time(value: str) -> time | None:
    normalized = re.sub(r"\s+", " ", value.strip().upper())
    formats = ("%H:%M", "%I:%M %p", "%I %p")
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt).time()
        except ValueError:
            continue
    return None


def _inject_break_slots(slots: list[LectureSlot]) -> list[LectureSlot]:
    if not slots:
        return slots

    result: list[LectureSlot] = []
    ordered = sorted(
        slots,
        key=lambda slot: (
            slot.start_time.isoformat() if slot.start_time else "99:99",
            slot.slot_label,
        ),
    )
    for index, slot in enumerate(ordered):
        result.append(slot)
        if index == len(ordered) - 1:
            continue
        current = ordered[index]
        following = ordered[index + 1]
        if not current.end_time or not following.start_time:
            continue
        current_minutes = current.end_time.hour * 60 + current.end_time.minute
        next_minutes = following.start_time.hour * 60 + following.start_time.minute
        if next_minutes - current_minutes < 15:
            continue
        result.append(
            LectureSlot(
                slot_label=f"{current.end_time.strftime('%H:%M')} - {following.start_time.strftime('%H:%M')}",
                subject_key="break",
                subject_name="Break",
                teacher_name="",
                raw_cell="",
                start_time=current.end_time,
                end_time=following.start_time,
                is_break=True,
            )
        )
    return result


def _split_cell_entries(cell: str) -> list[str]:
    normalized = str(cell or "").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    normalized = BeautifulSoup(normalized, "html.parser").get_text("\n")
    parts = [part.strip() for part in re.split(r"\s*-\s*(?=[A-Za-z])|\n+", normalized) if part.strip()]
    return parts


def _parse_subject_teacher_entry(value: str) -> tuple[str, str]:
    text = value.strip()
    if not text or _is_time_text(text):
        return "", ""
    if "," in text:
        subject_part, teacher_part = text.rsplit(",", 1)
        return _clean_subject(subject_part), teacher_part.strip()
    return _clean_subject(text), ""


def _clean_subject(value: str) -> str:
    subject = " ".join(value.split())
    while True:
        updated = re.sub(r"\s*\([A-Z]{2,}\d+[A-Z0-9]*\)\s*$", "", subject).strip()
        if updated != subject:
            subject = updated
            continue
        updated = re.sub(r"\s*\([A-Z][A-Za-z0-9_ -]*\)\s*$", "", subject).strip()
        if updated != subject:
            subject = updated
            continue
        break
    return subject


def _date_matches(value: str, target_date: date) -> bool:
    parsed = _parse_date(value)
    return parsed == target_date if parsed else False


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
