"""Extended ERP response parsers for new feature modules.

Parses internal marks, assignments, fee receipts, and exam schedule
responses from the Quantum University ERP system.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .models import Assignment, ExamEntry, FeeReceipt, InternalMark

logger = logging.getLogger(__name__)


def _safe_json_list(payload: dict[str, Any] | str, key: str = "state") -> list[dict[str, Any]]:
    """Extract a JSON-encoded list from a typical ERP response.

    ERP endpoints often return ``{"state": "[{...}, ...]"}`` where the value
    is a JSON-encoded string.  This helper normalises that to a Python list.
    Handles multiple response shapes: direct string, nested JSON, various keys.
    """
    # If the entire payload is a string, try to parse it
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []

    # Try primary key, then fallbacks
    for k in [key, "d", "data", "result", "Data", "Result"]:
        raw = payload.get(k)
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
            if isinstance(parsed, dict):
                return [parsed]
            continue
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            return [raw]

    # If the payload itself is a list at the top level
    if isinstance(payload, dict):
        # Check if the entire payload has list-like keys (numbered)
        # or try treating the whole dict as a single record
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v

    return []



def parse_internal_marks(payload: dict[str, Any]) -> list[InternalMark]:
    """Parse ``/Web_Exam/GetStudentInternalMarks`` response."""
    items = _safe_json_list(payload)
    results: list[InternalMark] = []
    for row in items:
        try:
            results.append(InternalMark(
                subject=str(row.get("Subject", "")).strip(),
                subject_code=str(row.get("SubjectCode", "")).strip(),
                component=str(row.get("SubPaper", "")).strip(),
                max_marks=str(row.get("MaxMark", "")).strip(),
                obtained_marks=str(row.get("Marks", "")).strip(),
                is_pass=str(row.get("IsPass", "")).strip(),
                sessional_counter=str(row.get("SessionalCounter", "")).strip(),
                raw=row,
            ))
        except Exception as exc:
            logger.debug("Skipping unparseable internal marks row: %s", exc)
    return results


def parse_assignments(payload: dict[str, Any]) -> list[Assignment]:
    """Parse ``/Web_StudentAcademic/GetStudentAssignment`` response."""
    items = _safe_json_list(payload)
    results: list[Assignment] = []
    for row in items:
        try:
            results.append(Assignment(
                subject=str(row.get("Subject", row.get("SubjectName", ""))).strip(),
                subject_code=str(row.get("SubjectCode", "")).strip(),
                title=str(row.get("AssignmentTitle", row.get("Title", ""))).strip(),
                assigned_date=str(row.get("AssignedDate", row.get("AssignDate", ""))).strip(),
                deadline_date=str(row.get("DeadlineDate", row.get("SubmissionDate", ""))).strip(),
                status=str(row.get("Status", row.get("SubmissionStatus", "Pending"))).strip(),
                raw=row,
            ))
        except Exception as exc:
            logger.debug("Skipping unparseable assignment row: %s", exc)
    return results


def parse_fee_receipts(payload: dict[str, Any]) -> list[FeeReceipt]:
    """Parse ``/Web_StudentFinance/GetStudentFeeReceipt`` response."""
    items = _safe_json_list(payload)
    results: list[FeeReceipt] = []
    for row in items:
        try:
            results.append(FeeReceipt(
                receipt_no=str(row.get("CombineReceiptNo", row.get("ReceiptNo", ""))).strip(),
                receipt_date=str(row.get("ReceiptDate", "")).strip(),
                amount=str(row.get("Amount", row.get("TotalAmount", "0"))).strip(),
                remarks=str(row.get("Remarks", "")).strip(),
                raw=row,
            ))
        except Exception as exc:
            logger.debug("Skipping unparseable fee receipt row: %s", exc)
    return results


def parse_exam_list(payload: dict[str, Any]) -> list[ExamEntry]:
    """Parse ``/OnlineExam/GetExamList`` response."""
    items = _safe_json_list(payload)
    results: list[ExamEntry] = []
    for row in items:
        try:
            results.append(ExamEntry(
                exam_name=str(row.get("ExamName", row.get("PaperName", ""))).strip(),
                subject=str(row.get("Subject", row.get("SubjectName", ""))).strip(),
                exam_date=str(row.get("ExamDate", row.get("Date", ""))).strip(),
                start_time=str(row.get("StartTime", "")).strip(),
                end_time=str(row.get("EndTime", "")).strip(),
                status=str(row.get("Status", "")).strip(),
                raw=row,
            ))
        except Exception as exc:
            logger.debug("Skipping unparseable exam entry row: %s", exc)
    return results
