from __future__ import annotations

import csv
import secrets
from datetime import timedelta
from io import StringIO
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from apscheduler.schedulers.base import STATE_RUNNING
from twilio.request_validator import RequestValidator

from .monitoring import init_monitoring
from .rate_limit import InMemoryRateLimiter
from .runtime import build_runtime
from .erp_client import ERPClientError
from .errors import StudentValidationError
from .scheduler import build_scheduler
from .service import NotificationDeliveryError
from .task_queue import TaskDispatcher
from .telegram import TelegramError
from .whatsapp import WhatsAppError


def create_app(*, start_scheduler: bool = True) -> Flask:
    runtime = build_runtime()
    settings = runtime.settings
    db = runtime.db
    service = runtime.service
    sentry_enabled = init_monitoring(settings)

    app = Flask(__name__, template_folder="templates")
    app.secret_key = settings.app_secret
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = settings.public_base_url.startswith("https://")
    app.config["service"] = service
    app.config["settings"] = settings
    app.config["rate_limiter"] = InMemoryRateLimiter()
    app.config["sentry_enabled"] = sentry_enabled

    dispatcher = TaskDispatcher(settings=settings, db=db, service=service)
    app.config["task_dispatcher"] = dispatcher

    scheduler = None
    if start_scheduler and settings.run_scheduler:
        scheduler = build_scheduler(settings, service, dispatcher)
        scheduler.start()
    app.config["scheduler"] = scheduler

    @app.before_request
    def require_admin_login():
        if request.endpoint in {
            "healthz",
            "admin_login",
            "admin_login_submit",
            "twilio_status_webhook",
            "twilio_inbound_webhook",
            "static",
        }:
            return None
        if not _admin_auth_enabled():
            return None
        if session.get("admin_authenticated"):
            return None
        return redirect(url_for("admin_login", next=request.path))

    @app.before_request
    def verify_csrf_token():
        if request.method != "POST":
            return None
        if request.endpoint in {
            "twilio_status_webhook",
            "twilio_inbound_webhook",
            "static",
        }:
            return None
        expected = session.get("_csrf_token")
        received = request.form.get("csrf_token", "")
        if not expected or not received or not secrets.compare_digest(expected, received):
            return ("Invalid CSRF token.", 400)
        return None

    @app.context_processor
    def inject_csrf_token():
        return {"csrf_token": _csrf_token}

    @app.get("/admin/login")
    def admin_login():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))
        if session.get("admin_authenticated"):
            return redirect(url_for("dashboard"))
        return render_template("admin_login.html", next_path=_safe_next_path(request.args.get("next")))

    @app.post("/admin/login")
    def admin_login_submit():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))
        limit_response = _enforce_rate_limit(
            bucket=f"admin-login:{_client_ip()}",
            limit=settings.admin_rate_limit_count,
            window_seconds=settings.admin_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_path = _safe_next_path(request.form.get("next_path"))

        if username == settings.admin_username and password == settings.admin_password:
            session.clear()
            session["admin_authenticated"] = True
            flash("Admin login successful.", "success")
            return redirect(next_path)

        flash("Invalid admin username or password.", "error")
        return render_template("admin_login.html", next_path=next_path), 401

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        flash("Admin session closed.", "success")
        return redirect(url_for("admin_login"))

    @app.get("/")
    def dashboard():
        return _render_dashboard()

    @app.get("/healthz")
    def healthz():
        service = _service()
        students = service.list_students()
        scheduler = app.config.get("scheduler")
        return jsonify(
            {
                "status": "ok",
                "app_env": service.settings.app_env,
                "use_waitress": service.settings.use_waitress,
                "twilio_mode": service.settings.twilio_whatsapp_mode,
                "twilio_configured": service.whatsapp.configured,
                "telegram_configured": service.telegram.configured,
                "student_count": len(students),
                "task_queue_mode": service.settings.task_queue_mode,
                "run_scheduler_configured": service.settings.run_scheduler,
                "scheduler_active": _scheduler_is_running(scheduler),
                "sentry_enabled": bool(app.config.get("sentry_enabled")),
                "outbound_queue": service.get_outbound_queue_summary(),
            }
        )

    @app.post("/webhooks/twilio/status")
    def twilio_status_webhook():
        limit_response = _enforce_rate_limit(
            bucket=f"twilio-status:{_client_ip()}",
            limit=settings.webhook_rate_limit_count,
            window_seconds=settings.webhook_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response
        if not _validate_twilio_signature(settings):
            return ("Invalid Twilio signature.", 403)

        _service().record_twilio_delivery_status(
            provider_sid=request.form.get("MessageSid", "").strip(),
            delivery_status=request.form.get("MessageStatus", "").strip() or "unknown",
            delivery_error_code=request.form.get("ErrorCode", "").strip() or None,
            delivery_error_message=request.form.get("ErrorMessage", "").strip() or None,
        )
        return ("", 204)

    @app.post("/webhooks/twilio/inbound")
    def twilio_inbound_webhook():
        limit_response = _enforce_rate_limit(
            bucket=f"twilio-inbound:{_client_ip()}",
            limit=settings.webhook_rate_limit_count,
            window_seconds=settings.webhook_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response
        if not _validate_twilio_signature(settings):
            return ("Invalid Twilio signature.", 403)

        reply_text = _service().handle_inbound_whatsapp_command(
            from_number=request.form.get("From", ""),
            body=request.form.get("Body", ""),
        )
        escaped = (
            reply_text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return Response(
            f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>',
            mimetype="application/xml",
        )

    @app.post("/students")
    def save_student():
        service = _service()
        student_id_raw = request.form.get("student_id") or None
        try:
            student_id = int(student_id_raw) if student_id_raw else None
        except ValueError:
            flash("Student id is not valid.", "error")
            return _render_dashboard(), 400
        try:
            saved_id = service.save_student(
                student_id=student_id,
                student_label=request.form.get("student_label", ""),
                user_name=request.form.get("user_name", ""),
                password=request.form.get("password", ""),
                whatsapp_number=request.form.get("whatsapp_number", ""),
                telegram_chat_id=request.form.get("telegram_chat_id", ""),
                email_address="",
                enabled=request.form.get("enabled") == "on",
                timezone=request.form.get("timezone", ""),
            )
        except StudentValidationError as exc:
            flash(str(exc), "error")
            return _render_dashboard(edit_id=student_id), 400
        except Exception as exc:
            _report_internal_exception("Student save failed unexpectedly.", exc)
            flash("Student profile could not be saved due to an internal error.", "error")
            return _render_dashboard(edit_id=student_id), 500

        _log_admin_action(
            action="save_student",
            target_type="student",
            target_id=str(saved_id),
            details="Student profile saved from dashboard.",
        )
        flash("Student profile saved.", "success")
        return redirect(url_for("dashboard", edit=saved_id))

    @app.post("/students/<int:student_id>/delete")
    def delete_student(student_id: int):
        deleted = _service().delete_student(student_id)
        if not deleted:
            flash("Student profile not found.", "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="delete_student",
            target_type="student",
            target_id=str(student_id),
            details="Student profile deleted from dashboard.",
        )
        flash("Student profile deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/login/start")
    def start_login(student_id: int):
        try:
            _service().start_login(student_id)
        except ERPClientError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="start_login",
            target_type="student",
            target_id=str(student_id),
            details="Manual ERP login started.",
        )
        flash("Login session created. Enter the captcha to complete ERP login.", "success")
        return redirect(url_for("login_page", student_id=student_id))

    @app.get("/students/<int:student_id>/login")
    def login_page(student_id: int):
        student = _service().get_student(student_id)
        pending = _service().db.get_pending_login(student_id)
        if not student or not pending:
            flash("No pending login session. Start login first.", "error")
            return redirect(url_for("dashboard"))
        return render_template("login.html", student=student, pending=pending)

    @app.post("/students/<int:student_id>/login/refresh")
    def refresh_login(student_id: int):
        try:
            _service().refresh_login(student_id)
        except ERPClientError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="refresh_login",
            target_type="student",
            target_id=str(student_id),
            details="Captcha refreshed for ERP login.",
        )
        flash("Captcha refreshed.", "success")
        return redirect(url_for("login_page", student_id=student_id))

    @app.post("/students/<int:student_id>/login/complete")
    def complete_login(student_id: int):
        captcha = request.form.get("captcha", "")
        try:
            message = _service().complete_login(student_id, captcha)
        except ERPClientError as exc:
            flash(str(exc), "error")
            return redirect(url_for("login_page", student_id=student_id))
        _log_admin_action(
            action="complete_login",
            target_type="student",
            target_id=str(student_id),
            details="ERP login completed from dashboard.",
        )
        flash(message, "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/preview")
    def preview_today(student_id: int):
        try:
            preview_text = _service().preview_today(student_id)
        except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="preview_today",
            target_type="student",
            target_id=str(student_id),
            details="Morning preview generated from dashboard.",
        )
        flash("Preview generated from the current ERP session.", "success")
        return _render_dashboard(preview_text=preview_text)

    @app.post("/students/<int:student_id>/send-morning")
    def send_morning(student_id: int):
        try:
            _service().send_morning_update(student_id, force=True)
        except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="send_morning_update",
            target_type="student",
            target_id=str(student_id),
            details="Manual morning summary sent.",
        )
        flash("Morning summary sent to configured channels.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/send-attendance-summary")
    def send_attendance_summary(student_id: int):
        try:
            _service().send_attendance_summary_report(student_id, force=True)
        except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="send_attendance_summary_report",
            target_type="student",
            target_id=str(student_id),
            details="Manual attendance summary report sent.",
        )
        flash("Attendance summary report sent to configured channels.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/send-evening")
    def send_evening(student_id: int):
        try:
            _service().send_evening_report(student_id, force=True)
        except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="send_evening_report",
            target_type="student",
            target_id=str(student_id),
            details="Manual end-of-day report sent.",
        )
        flash("End-of-day attendance report sent to configured channels.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/send-shortage-report")
    def send_shortage_report(student_id: int):
        try:
            _service().send_shortage_report(student_id, force=True)
        except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="send_shortage_report",
            target_type="student",
            target_id=str(student_id),
            details="Manual attendance shortage report sent.",
        )
        flash("Attendance shortage report sent to configured channels.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/send-test")
    def send_test(student_id: int):
        try:
            _service().send_test_message(student_id)
        except (WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_admin_action(
            action="send_test_message",
            target_type="student",
            target_id=str(student_id),
            details="Manual test message sent across configured channels.",
        )
        flash("Test message sent to configured channels.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/dead-letter/<path:idempotency_key>/retry")
    def retry_dead_letter(idempotency_key: str):
        try:
            message = _service().retry_dead_letter_message(idempotency_key)
        except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            if _wants_json_response():
                return jsonify({"ok": False, "message": str(exc)}), 400
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("dashboard"))
        _log_admin_action(
            action="retry_dead_letter_message",
            target_type="outbound_message",
            target_id=idempotency_key,
            details="Manual dead-letter retry executed from dashboard.",
        )
        if _wants_json_response():
            return jsonify(
                {
                    "ok": True,
                    "message": message,
                    "idempotency_key": idempotency_key,
                    "queue": _service().get_outbound_queue_summary(),
                }
            )
        flash(message, "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/checks/run")
    def run_checks():
        _service().run_scheduled_dispatch()
        _service().run_due_checks()
        _service().run_substitution_sweep()
        _service().run_monitor_sweep()
        _service().run_retry_sweep()
        _log_admin_action(
            action="run_live_checks",
            target_type="system",
            target_id="dashboard",
            details="Manual live checks executed from dashboard.",
        )
        flash("Scheduled, attendance, evening-report, substitution, monitoring, and retry checks executed.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/exports/message-history.csv")
    def export_message_history():
        message_history_all = _service()
        message_state = _build_message_export_state(message_history_all, request.form)
        _log_admin_action(
            action="export_message_history",
            target_type="system",
            target_id="message_history",
            details=(
                "CSV export requested "
                f"(page {message_state['pagination']['page']} of {message_state['pagination']['total_pages']})."
            ),
        )
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
        for item in message_state["rows"]:
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
        filename = f"message-history-page-{message_state['pagination']['page']}.csv"
        return _csv_response(output.getvalue(), filename)

    @app.post("/exports/audit-log.csv")
    def export_audit_log():
        audit_log_all = _service()
        audit_state = _build_audit_export_state(audit_log_all, request.form)
        _log_admin_action(
            action="export_audit_log",
            target_type="system",
            target_id="audit_log",
            details=(
                "CSV export requested "
                f"(page {audit_state['pagination']['page']} of {audit_state['pagination']['total_pages']})."
            ),
        )
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["created_at", "actor", "action", "target_type", "target_id", "details"])
        for item in audit_state["rows"]:
            writer.writerow([item.created_at, item.actor, item.action, item.target_type, item.target_id, item.details])
        filename = f"admin-audit-log-page-{audit_state['pagination']['page']}.csv"
        return _csv_response(output.getvalue(), filename)

    @app.get("/dashboard/live-data")
    def dashboard_live_data():
        service = _service()
        message_state = _build_message_history_state(service, request.args)
        dead_letter_messages = service.get_dead_letter_messages(10)
        return jsonify(
            {
                "message_history_html": render_template(
                    "partials/message_history_results.html",
                    message_history=message_state["rows"],
                    message_history_filters=message_state["filters"],
                    message_history_pagination=message_state["pagination"],
                    audit_log_filters={
                        "query": request.args.get("audit_q", "").strip(),
                        "action": request.args.get("audit_action", "").strip().lower(),
                    },
                    audit_log_pagination={
                        "page": _parse_positive_int(request.args.get("audit_page"), default=1),
                    },
                ),
                "dead_letter_html": render_template(
                    "partials/dead_letter_content.html",
                    dead_letter_messages=dead_letter_messages,
                ),
                "outbound_summary": service.get_outbound_queue_summary(),
            }
        )

    return app


def _service() -> BotService:
    from flask import current_app

    return current_app.config["service"]


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _admin_auth_enabled() -> bool:
    service = _service()
    return bool(service.settings.admin_username and service.settings.admin_password)


def _render_dashboard(*, edit_id: int | None = None, preview_text: str | None = None):
    service = _service()
    students = service.list_students()
    student_dashboard_views = _build_student_dashboard_views(students)
    message_history_options = service.get_message_history_filter_options()
    audit_log_options = service.get_admin_audit_filter_options()
    edit_student = service.get_student(edit_id) if edit_id else None
    settings = service.settings
    dashboard_now = service._local_now()
    outbound_summary = service.get_outbound_queue_summary()
    dead_letter_messages = service.get_dead_letter_messages(10)
    message_state = _build_message_history_state(service, request.args)
    audit_state = _build_audit_log_state(service, request.args)
    whatsapp_statuses = {student.id: service.get_whatsapp_status(student) for student in students}
    student_automation_statuses = {
        student.id: service.get_student_automation_status(student, now=dashboard_now)
        for student in students
    }
    action_center = _build_action_center(
        students=students,
        student_dashboard_views=student_dashboard_views,
        whatsapp_statuses=whatsapp_statuses,
        outbound_summary=outbound_summary,
        dead_letters=dead_letter_messages,
        dashboard_now=dashboard_now,
    )
    return render_template(
        "dashboard.html",
        students=students,
        student_dashboard_views=student_dashboard_views,
        edit_student=edit_student,
        preview_text=preview_text,
        whatsapp_statuses=whatsapp_statuses,
        student_automation_statuses=student_automation_statuses,
        message_history=message_state["rows"],
        message_history_filter_options=message_history_options,
        message_history_filters=message_state["filters"],
        message_history_pagination=message_state["pagination"],
        audit_log=audit_state["rows"],
        audit_log_filter_options=audit_log_options,
        audit_log_filters=audit_state["filters"],
        audit_log_pagination=audit_state["pagination"],
        action_center=action_center,
        outbound_summary=outbound_summary,
        dead_letter_messages=dead_letter_messages,
        twilio_configured=service.whatsapp.configured,
        telegram_configured=service.telegram.configured,
        settings=settings,
        scheduler_overview=_build_scheduler_overview(dashboard_now),
        scheduler_active=_scheduler_is_running(current_app.config.get("scheduler")),
        sentry_enabled=bool(current_app.config.get("sentry_enabled")),
    )


def _build_scheduler_overview(now):
    from flask import current_app

    service = _service()
    scheduler = current_app.config.get("scheduler")
    job_definitions = [
        ("scheduled-dispatch", "Morning Summary Scanner", "Checks whether any student has reached the morning digest time."),
        ("attendance-checks", "Attendance Scanner", "Checks finished lectures and sends present, absent, or pending updates."),
        ("substitution-checks", "Substitution Scanner", "Checks for newly assigned substitute lectures and sends alerts."),
        ("monitor-checks", "Monitoring Scanner", "Checks sandbox expiry and ERP session status."),
        ("delivery-retry-checks", "Delivery Retry Scanner", "Retries transient channel delivery failures and moves exhausted items to the dead-letter queue."),
        ("telegram-inbound-checks", "Telegram Command Scanner", "Polls Telegram for admin commands, callbacks, and student form input."),
        ("telegram-admin-refresh-checks", "Telegram Dashboard Sync", "Updates the Telegram admin dashboard message when the underlying dashboard data changes."),
    ]
    jobs = []
    for job_id, label, detail in job_definitions:
        job = scheduler.get_job(job_id) if scheduler else None
        next_run = None
        if job and job.next_run_time:
            next_run = job.next_run_time.astimezone(service.timezone).replace(microsecond=0)
        jobs.append(
            {
                "id": job_id,
                "label": label,
                "detail": detail,
                "next_run_iso": next_run.isoformat() if next_run else None,
                "next_run_label": service._format_datetime(next_run) if next_run else "Not scheduled",
            }
        )
    return {
        "server_now_iso": now.replace(microsecond=0).isoformat(),
        "server_now_label": service._format_datetime(now),
        "jobs": jobs,
    }


def _build_message_history_state(
    service,
    params,
    *,
    page_param: str = "message_page",
    per_page_param: str = "message_per_page",
    default_per_page: int = 20,
    max_per_page: int = 100,
):
    filters = {
        "query": params.get("message_q", "").strip(),
        "channel": params.get("message_channel", "").strip().lower(),
        "category": params.get("message_category", "").strip().lower(),
    }
    page = _parse_positive_int(params.get(page_param), default=1)
    per_page = _parse_bounded_positive_int(params.get(per_page_param), default=default_per_page, maximum=max_per_page)
    total_items = service.count_message_history(**filters)
    page_rows, pagination = _paginate_rows(
        total_items=total_items,
        page=page,
        per_page=per_page,
        fetch_page=lambda limit, offset: service.get_message_history_page(limit=limit, offset=offset, **filters),
    )
    return {"rows": page_rows, "filters": filters, "pagination": pagination}


def _build_message_export_state(service, params):
    filters = {
        "query": params.get("message_q", "").strip(),
        "channel": params.get("message_channel", "").strip().lower(),
        "category": params.get("message_category", "").strip().lower(),
    }
    total_items = service.count_message_history(**filters)
    if params.get("page") or params.get("per_page"):
        page = _parse_positive_int(params.get("page"), default=1)
        per_page = _parse_bounded_positive_int(params.get("per_page"), default=20, maximum=500)
        page_rows, pagination = _paginate_rows(
            total_items=total_items,
            page=page,
            per_page=per_page,
            fetch_page=lambda limit, offset: service.get_message_history_page(limit=limit, offset=offset, **filters),
        )
        return {"rows": page_rows, "filters": filters, "pagination": pagination}
    rows = service.get_message_history_page(limit=max(total_items, 1), offset=0, **filters) if total_items else []
    return {
        "rows": rows,
        "filters": filters,
        "pagination": {
            "page": 1,
            "per_page": len(rows) or 1,
            "total_items": total_items,
            "total_pages": 1,
            "has_prev": False,
            "has_next": False,
            "prev_page": 1,
            "next_page": 1,
        },
    }


def _build_audit_log_state(
    service,
    params,
    *,
    page_param: str = "audit_page",
    per_page_param: str = "audit_per_page",
    default_per_page: int = 20,
    max_per_page: int = 100,
):
    filters = {
        "query": params.get("audit_q", "").strip(),
        "action": params.get("audit_action", "").strip().lower(),
    }
    page = _parse_positive_int(params.get(page_param), default=1)
    per_page = _parse_bounded_positive_int(params.get(per_page_param), default=default_per_page, maximum=max_per_page)
    total_items = service.count_admin_audit_log(**filters)
    page_rows, pagination = _paginate_rows(
        total_items=total_items,
        page=page,
        per_page=per_page,
        fetch_page=lambda limit, offset: service.get_admin_audit_log_page(limit=limit, offset=offset, **filters),
    )
    return {"rows": page_rows, "filters": filters, "pagination": pagination}


def _build_audit_export_state(service, params):
    filters = {
        "query": params.get("audit_q", "").strip(),
        "action": params.get("audit_action", "").strip().lower(),
    }
    total_items = service.count_admin_audit_log(**filters)
    if params.get("page") or params.get("per_page"):
        page = _parse_positive_int(params.get("page"), default=1)
        per_page = _parse_bounded_positive_int(params.get("per_page"), default=20, maximum=500)
        page_rows, pagination = _paginate_rows(
            total_items=total_items,
            page=page,
            per_page=per_page,
            fetch_page=lambda limit, offset: service.get_admin_audit_log_page(limit=limit, offset=offset, **filters),
        )
        return {"rows": page_rows, "filters": filters, "pagination": pagination}
    rows = service.get_admin_audit_log_page(limit=max(total_items, 1), offset=0, **filters) if total_items else []
    return {
        "rows": rows,
        "filters": filters,
        "pagination": {
            "page": 1,
            "per_page": len(rows) or 1,
            "total_items": total_items,
            "total_pages": 1,
            "has_prev": False,
            "has_next": False,
            "prev_page": 1,
            "next_page": 1,
        },
    }


def _paginate_rows(*, total_items: int, page: int, per_page: int, fetch_page):
    total_pages = max((total_items + per_page - 1) // per_page, 1)
    current_page = max(1, min(page, total_pages))
    start = (current_page - 1) * per_page
    end = start + per_page
    rows = fetch_page(per_page, start) if total_items else []
    return rows, {
        "page": current_page,
        "per_page": per_page,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_prev": current_page > 1,
        "has_next": current_page < total_pages,
        "prev_page": current_page - 1,
        "next_page": current_page + 1,
    }


def _parse_positive_int(value, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_bounded_positive_int(value, *, default: int, maximum: int) -> int:
    parsed = _parse_positive_int(value, default=default)
    return min(parsed, maximum)


def _build_student_dashboard_views(students):
    service = _service()
    views: dict[int, dict[str, object]] = {}
    for student in students:
        pending_login = service.db.get_pending_login(student.id)
        erp_status = _derive_dashboard_erp_status(student, pending_login=pending_login)
        recent_activity = (student.last_bot_activity_text or "").strip() or None
        last_erp_sync_at = _format_dashboard_timestamp(student.last_erp_sync_at or student.session_updated_at)
        last_bot_action_at = _format_dashboard_timestamp(student.last_bot_action_at or student.updated_at)
        views[student.id] = {
            "erp_status": erp_status,
            "recent_activity": recent_activity,
            "captcha_ready": pending_login is not None,
            "last_erp_sync_at": last_erp_sync_at,
            "last_bot_action_at": last_bot_action_at,
        }
    return views


def _derive_dashboard_erp_status(student, *, pending_login):
    raw_status = (student.erp_status_text or "").strip()
    if pending_login:
        return "Waiting for manual captcha entry."
    if not student.session_cookies:
        if raw_status:
            return raw_status
        return "Not logged in yet."
    if raw_status:
        return raw_status
    if student.session_updated_at:
        return f"ERP session saved. Last session update: {_format_dashboard_timestamp(student.session_updated_at) or student.session_updated_at}"
    return "ERP session saved."


def _format_dashboard_timestamp(value):
    if not value:
        return None
    service = _service()
    try:
        parsed = service._parse_datetime(value)
    except ValueError:
        return value
    if not parsed:
        return None
    return service._format_datetime(parsed.astimezone(service.timezone))


def _build_action_center(*, students, student_dashboard_views, whatsapp_statuses, outbound_summary, dead_letters, dashboard_now):
    service = _service()
    items: list[dict[str, str]] = []
    for student in students:
        student_view = student_dashboard_views.get(student.id, {})
        erp_status = str(student_view.get("erp_status") or "")
        if bool(student_view.get("captcha_ready")):
            items.append(
                {
                    "level": "warning",
                    "title": f"{student.student_label}: Captcha entry pending",
                    "detail": "A login session is ready. Use Open Captcha to complete ERP login.",
                }
            )
        elif not student.session_cookies:
            items.append(
                {
                    "level": "warning",
                    "title": f"{student.student_label}: ERP login required",
                    "detail": "No active ERP session is saved for this student.",
                }
            )
        elif "expired" in erp_status.lower():
            items.append(
                {
                    "level": "critical",
                    "title": f"{student.student_label}: ERP session expired",
                    "detail": erp_status or "ERP login is required again.",
                }
            )
        wa = whatsapp_statuses.get(student.id)
        if wa and wa.sandbox_expires_at:
            try:
                expires_at = service._parse_datetime(wa.sandbox_expires_at)
            except ValueError:
                expires_at = None
            if expires_at:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=service.timezone)
                remaining = expires_at.astimezone(service.timezone) - dashboard_now
                if remaining <= timedelta(minutes=service.settings.sandbox_expiry_warning_minutes):
                    items.append(
                        {
                            "level": "warning",
                            "title": f"{student.student_label}: Sandbox expiring",
                            "detail": f"WhatsApp sandbox expires at {service._format_datetime(expires_at.astimezone(service.timezone))}.",
                        }
                    )
        if wa and not wa.ready:
            items.append(
                {
                    "level": "warning",
                    "title": f"{student.student_label}: WhatsApp not ready",
                    "detail": wa.detail,
                }
            )
    failed_count = outbound_summary.get("failed", 0)
    dead_letter_count = outbound_summary.get("dead_letter", 0)
    if failed_count:
        items.append(
            {
                "level": "warning",
                "title": "Pending delivery retries",
                "detail": f"{failed_count} outbound message(s) are waiting for automatic retry.",
            }
        )
    if dead_letter_count:
        items.append(
            {
                "level": "critical",
                "title": "Dead-letter queue has failed alerts",
                "detail": f"{dead_letter_count} outbound message(s) exhausted retries and require manual review.",
            }
        )
    for item in dead_letters[:3]:
        items.append(
            {
                "level": "critical",
                "title": f"Dead letter: {item.title}",
                "detail": item.delivery_error_message or "Delivery failed permanently.",
            }
        )
    return items


def _log_admin_action(*, action: str, target_type: str, target_id: str, details: str) -> None:
    actor = (
        (_service().settings.admin_username or "dashboard-admin")
        if session.get("admin_authenticated")
        else "dashboard"
    )
    try:
        _service().log_admin_action(
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
    except Exception as exc:
        _report_internal_exception("Admin audit logging failed.", exc)
        return


def _report_internal_exception(message: str, exc: Exception) -> None:
    current_app.logger.exception(message)
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        return


def _safe_next_path(value: str | None) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return url_for("dashboard")
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc or not candidate.startswith("/"):
        return url_for("dashboard")
    return candidate


def _scheduler_is_running(scheduler) -> bool:
    if not scheduler:
        return False
    running = getattr(scheduler, "running", None)
    if running is not None:
        return bool(running)
    state = getattr(scheduler, "state", None)
    return state == STATE_RUNNING


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _validate_twilio_signature(settings) -> bool:
    if not settings.twilio_auth_token:
        return False
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False
    validator = RequestValidator(settings.twilio_auth_token)
    url = request.url
    if settings.public_base_url:
        url = f"{settings.public_base_url}{request.path}"
    params = {key: value for key, value in request.form.items()}
    return validator.validate(url, params, signature)


def _enforce_rate_limit(*, bucket: str, limit: int, window_seconds: int):
    from flask import current_app

    limiter = current_app.config["rate_limiter"]
    result = limiter.check(bucket=bucket, limit=limit, window_seconds=window_seconds)
    if result.allowed:
        return None
    return ("Too many requests. Please retry later.", 429, {"Retry-After": str(result.retry_after_seconds)})


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def _wants_json_response() -> bool:
    requested_with = request.headers.get("X-Requested-With", "")
    accept_header = request.headers.get("Accept", "")
    return requested_with == "XMLHttpRequest" or "application/json" in accept_header.lower()
