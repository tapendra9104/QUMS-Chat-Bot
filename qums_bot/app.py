from __future__ import annotations

import csv
import json
import secrets
from datetime import datetime, timedelta, timezone
from io import StringIO
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from apscheduler.schedulers.base import STATE_RUNNING
from werkzeug.security import check_password_hash, generate_password_hash

from .config import load_settings
from .monitoring import capture_monitoring_exception, init_monitoring
from .rate_limit import InMemoryRateLimiter
from .runtime import build_runtime
from .erp_client import ERPClientError
from .errors import StudentValidationError
from .scheduler import build_scheduler
from .security import decrypt_text
from .service import (
    NOTIFICATION_CHANNEL_MODE_LABELS,
    STUDENT_ACTION_LABELS,
    STUDENT_ACTION_ORDER,
    NotificationDeliveryError,
)
from .task_queue import TaskDispatcher
from .telegram import TelegramError
from .whatsapp import WhatsAppError


ADMIN_RESET_STATE_KEY = "admin_password_reset"
ADMIN_USERNAME_OVERRIDE_KEY = "admin_username_override"
ADMIN_PASSWORD_HASH_OVERRIDE_KEY = "admin_password_hash_override"
ADMIN_TELEGRAM_USERNAME_OVERRIDE_KEY = "admin_telegram_username_override"
ADMIN_RESET_CODE_TTL_MINUTES = 10
STUDENT_RESET_STATE_PREFIX = "student_password_reset:"
STUDENT_RESET_CODE_TTL_MINUTES = 10
DELIVERY_BASED_STUDENT_ACTIONS = {
    "send_attendance_summary",
    "send_morning",
    "send_substitution_report",
    "send_day_report",
    "send_shortage_report",
    "send_channel_test",
}


def create_app(*, start_scheduler: bool = True) -> Flask:
    settings = load_settings()
    sentry_enabled = init_monitoring(
        settings,
        component="web",
        include_flask_integration=True,
    )
    runtime = build_runtime(settings)
    db = runtime.db
    service = runtime.service

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
    def require_dashboard_access():
        endpoint = request.endpoint or ""
        if not endpoint:
            return None
        public_endpoints = {
            "healthz",
            "dashboard",
            "dashboard_live_data",
            "submit_application",
            "login_alias",
            "student_login_submit",
            "student_logout",
            "student_forgot_password",
            "student_forgot_password_request",
            "student_forgot_password_reset",
            "admin_login",
            "admin_login_submit",
            "admin_forgot_password",
            "admin_forgot_password_request",
            "admin_forgot_password_reset",
            "static",
        }
        admin_only_endpoints = {
            "admin_logout",
            "admin_account_update",
            "save_student",
            "update_student_controls",
            "accept_application",
            "reject_application",
            "clear_application",
            "delete_student",
            "start_login",
            "login_page",
            "refresh_login",
            "complete_login",
            "preview_today",
            "send_morning",
            "send_attendance_summary",
            "send_evening",
            "send_shortage_report",
            "send_test",
            "retry_dead_letter",
            "run_checks",
            "export_message_history",
            "export_audit_log",
        }
        shared_dashboard_endpoints = {
            "send_substitution_report",
        }
        student_only_endpoints = {
            "student_password_change_request",
            "student_password_change_submit",
            "student_profile_update",
        }
        if endpoint in public_endpoints:
            return None
        if endpoint in admin_only_endpoints:
            if _is_admin_authenticated():
                return None
            if not _admin_auth_enabled():
                return None
            return _auth_required_response(
                url_for("admin_login", next=request.path),
                "Admin sign-in required. Please sign in again.",
            )
        if endpoint in shared_dashboard_endpoints:
            if _is_admin_authenticated() or _is_student_authenticated():
                return None
            if not _admin_auth_enabled():
                return None
            return _auth_required_response(
                url_for("login_alias", next=request.path),
                "Please sign in to continue.",
            )
        if endpoint in student_only_endpoints:
            if _is_student_authenticated():
                return None
            return _auth_required_response(
                url_for("login_alias", next=request.path),
                "Student sign-in required. Please sign in again.",
            )
        if _is_admin_authenticated() or _is_student_authenticated() or not _admin_auth_enabled():
            return None
        return _auth_required_response(
            url_for("login_alias", next=request.path),
            "Please sign in to continue.",
        )

    @app.before_request
    def verify_csrf_token():
        if request.method != "POST":
            return None
        if not request.endpoint:
            return None
        if request.endpoint in {
            "static",
        }:
            return None
        expected = session.get("_csrf_token")
        received = request.form.get("csrf_token", "")
        if not expected or not received or not secrets.compare_digest(expected, received):
            session.pop("_csrf_token", None)
            message = "Your session expired or the page is stale. Please refresh and try again."
            if _is_ajax_request():
                return jsonify({"message": message, "reload": True}), 400
            flash(message, "error")
            return redirect(_csrf_retry_location())
        return None

    @app.after_request
    def disable_html_caching(response: Response):
        if request.method == "GET" and response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.context_processor
    def inject_csrf_token():
        return {"csrf_token": _csrf_token}

    @app.get("/admin/login")
    def admin_login():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))
        if session.get("admin_authenticated"):
            return redirect(url_for("dashboard"))
        return render_template(
            "admin_login.html",
            next_path=_safe_next_path(request.args.get("next")),
            recovery_configured=bool(_get_admin_account_state()["recovery_telegram_username"]),
        )

    @app.get("/login")
    def login_alias():
        if _is_admin_authenticated() or _is_student_authenticated() or _is_pending_application_authenticated():
            return redirect(url_for("dashboard"))
        return render_template(
            "site_login.html",
            next_path=_safe_next_path(request.args.get("next")),
            admin_login_path=url_for("admin_login", next=_safe_next_path(request.args.get("next"))),
        )

    @app.post("/login")
    def student_login_submit():
        limit_response = _enforce_rate_limit(
            bucket=f"student-login:{_client_ip()}",
            limit=settings.admin_rate_limit_count,
            window_seconds=settings.admin_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response

        login_username = request.form.get("login_username", "").strip()
        password = request.form.get("password", "")
        next_path = _safe_next_path(request.form.get("next_path"))
        try:
            student = _service().get_student_by_site_login_username(login_username)
        except StudentValidationError:
            student = None
        if (
            student
            and student.enabled
            and student.site_login_username
            and student.site_password_hash
            and password
            and check_password_hash(student.site_password_hash, password)
        ):
            session.clear()
            session["student_authenticated"] = True
            session["student_id"] = student.id
            session["student_username"] = student.site_login_username
            flash(f"Signed in as {student.site_login_username}.", "success")
            return redirect(next_path)

        try:
            application = _service().get_application_request_by_site_login_username(login_username)
        except StudentValidationError:
            application = None
        if (
            not application
            or not application.site_login_username
            or not application.site_password_hash
            or not password
            or not check_password_hash(application.site_password_hash, password)
        ):
            flash("Invalid login username or password.", "error")
            return render_template(
                "site_login.html",
                next_path=next_path,
                admin_login_path=url_for("admin_login", next=next_path),
            ), 401

        session.clear()
        session["pending_application_authenticated"] = True
        session["pending_application_id"] = application.id
        session["pending_application_username"] = application.site_login_username
        application_status = _normalize_application_request_status(application.status)
        if application_status == "accepted":
            flash(
                "Signed in successfully. Your application has been approved, and the full student dashboard will open automatically.",
                "success",
            )
        elif application_status == "rejected":
            flash(
                "Signed in successfully. This application has been reviewed and closed. Student features remain unavailable for this request.",
                "warning",
            )
        else:
            flash(
                f"Signed in successfully as {application.site_login_username}. Your website account is active, but student features remain unavailable until an administrator approves the request.",
                "warning",
            )
        return redirect(next_path)

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

        if _verify_admin_credentials(username, password):
            session.clear()
            session["admin_authenticated"] = True
            session["admin_username"] = username
            flash("Admin login successful.", "success")
            return redirect(next_path)

        flash("Invalid admin username or password.", "error")
        return render_template(
            "admin_login.html",
            next_path=next_path,
            recovery_configured=bool(_get_admin_account_state()["recovery_telegram_username"]),
        ), 401

    @app.get("/admin/forgot-password")
    def admin_forgot_password():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))
        if session.get("admin_authenticated"):
            return redirect(url_for("dashboard"))
        return render_template(
            "admin_forgot_password.html",
            recovery_configured=bool(_get_admin_account_state()["recovery_telegram_username"]),
            reset_code_ttl_minutes=ADMIN_RESET_CODE_TTL_MINUTES,
            telegram_username="",
            next_login_path=_safe_next_path(request.args.get("next")),
        )

    @app.post("/admin/forgot-password/request")
    def admin_forgot_password_request():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))
        limit_response = _enforce_rate_limit(
            bucket=f"admin-forgot:{_client_ip()}",
            limit=settings.admin_rate_limit_count,
            window_seconds=settings.admin_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response
        telegram_username = request.form.get("telegram_username", "").strip()
        try:
            normalized = _issue_admin_password_reset_code(telegram_username)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template(
                "admin_forgot_password.html",
                recovery_configured=bool(_get_admin_account_state()["recovery_telegram_username"]),
                reset_code_ttl_minutes=ADMIN_RESET_CODE_TTL_MINUTES,
                telegram_username=telegram_username,
                next_login_path=url_for("admin_login"),
            ), 400
        except TelegramError as exc:
            _report_internal_exception("Telegram password reset delivery failed.", exc)
            flash("Reset code could not be sent to Telegram right now.", "error")
            return render_template(
                "admin_forgot_password.html",
                recovery_configured=bool(_get_admin_account_state()["recovery_telegram_username"]),
                reset_code_ttl_minutes=ADMIN_RESET_CODE_TTL_MINUTES,
                telegram_username=telegram_username,
                next_login_path=url_for("admin_login"),
            ), 502

        flash(
            f"A reset code was sent to the configured Telegram admin chat for {normalized}.",
            "success",
        )
        return render_template(
            "admin_forgot_password.html",
            recovery_configured=True,
            reset_code_ttl_minutes=ADMIN_RESET_CODE_TTL_MINUTES,
            telegram_username=normalized,
            next_login_path=url_for("admin_login"),
        )

    @app.post("/admin/forgot-password/reset")
    def admin_forgot_password_reset():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))
        limit_response = _enforce_rate_limit(
            bucket=f"admin-reset:{_client_ip()}",
            limit=settings.admin_rate_limit_count,
            window_seconds=settings.admin_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response

        telegram_username = request.form.get("telegram_username", "").strip()
        reset_code = request.form.get("reset_code", "").strip()
        new_username = request.form.get("new_username", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        try:
            normalized_telegram = _normalize_telegram_username(telegram_username)
            validated_username = _validate_admin_login_username(new_username)
            validated_password = _validate_admin_password(new_password, confirm_password)
            _consume_admin_password_reset_code(normalized_telegram, reset_code)
            _persist_admin_account_credentials(
                username=validated_username,
                password=validated_password,
                recovery_telegram_username=normalized_telegram,
            )
            try:
                _service().log_admin_action(
                    actor=f"password-reset:{normalized_telegram}",
                    action="admin_password_reset",
                    target_type="admin_account",
                    target_id=validated_username,
                    details="Admin login credentials were reset through Telegram verification.",
                )
            except Exception as exc:
                _report_internal_exception("Admin password reset audit logging failed.", exc)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template(
                "admin_forgot_password.html",
                recovery_configured=bool(_get_admin_account_state()["recovery_telegram_username"]),
                reset_code_ttl_minutes=ADMIN_RESET_CODE_TTL_MINUTES,
                telegram_username=telegram_username,
                next_login_path=url_for("admin_login"),
            ), 400

        session.clear()
        flash("Admin login credentials were updated. Sign in with the new username and password.", "success")
        return redirect(url_for("admin_login"))

    @app.get("/forgot-password")
    def student_forgot_password():
        if _is_student_authenticated() or _is_pending_application_authenticated():
            return redirect(url_for("dashboard"))
        return render_template(
            "student_forgot_password.html",
            reset_code_ttl_minutes=STUDENT_RESET_CODE_TTL_MINUTES,
            login_username="",
            next_login_path=_safe_next_path(request.args.get("next")) or url_for("login_alias"),
        )

    @app.post("/forgot-password/request")
    def student_forgot_password_request():
        limit_response = _enforce_rate_limit(
            bucket=f"student-forgot:{_client_ip()}",
            limit=settings.admin_rate_limit_count,
            window_seconds=settings.admin_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response
        login_username = request.form.get("login_username", "").strip()
        try:
            student = _issue_student_password_reset_code(login_username, purpose="forgot_password")
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template(
                "student_forgot_password.html",
                reset_code_ttl_minutes=STUDENT_RESET_CODE_TTL_MINUTES,
                login_username=login_username,
                next_login_path=url_for("login_alias"),
            ), 400
        except StudentValidationError:
            flash("Student login username is not valid.", "error")
            return render_template(
                "student_forgot_password.html",
                reset_code_ttl_minutes=STUDENT_RESET_CODE_TTL_MINUTES,
                login_username=login_username,
                next_login_path=url_for("login_alias"),
            ), 400
        except TelegramError as exc:
            _report_internal_exception("Student password reset delivery failed.", exc)
            flash("Reset code could not be sent to Telegram right now.", "error")
            return render_template(
                "student_forgot_password.html",
                reset_code_ttl_minutes=STUDENT_RESET_CODE_TTL_MINUTES,
                login_username=login_username,
                next_login_path=url_for("login_alias"),
            ), 502

        flash(f"A verification code was sent to Telegram for {student.site_login_username}.", "success")
        return render_template(
            "student_forgot_password.html",
            reset_code_ttl_minutes=STUDENT_RESET_CODE_TTL_MINUTES,
            login_username=student.site_login_username,
            next_login_path=url_for("login_alias"),
        )

    @app.post("/forgot-password/reset")
    def student_forgot_password_reset():
        limit_response = _enforce_rate_limit(
            bucket=f"student-reset:{_client_ip()}",
            limit=settings.admin_rate_limit_count,
            window_seconds=settings.admin_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response

        login_username = request.form.get("login_username", "").strip()
        reset_code = request.form.get("reset_code", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        try:
            student = _require_student_for_site_login(login_username)
            validated_password = _validate_site_password(new_password, confirm_password)
            _consume_student_password_reset_code(student, reset_code)
            _service().update_student_site_password(student_id=student.id, new_password=validated_password)
            _notify_student_password_change(student, change_type="forgot_password_reset")
            _service().log_admin_action(
                actor=f"student-reset:{student.site_login_username}",
                action="student_password_reset",
                target_type="student",
                target_id=str(student.id),
                details="Student website password was reset through Telegram verification.",
            )
        except (ValueError, StudentValidationError) as exc:
            flash(str(exc), "error")
            return render_template(
                "student_forgot_password.html",
                reset_code_ttl_minutes=STUDENT_RESET_CODE_TTL_MINUTES,
                login_username=login_username,
                next_login_path=url_for("login_alias"),
            ), 400

        session.clear()
        flash("Password updated. Sign in with your new password.", "success")
        return redirect(url_for("login_alias"))

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        flash("Admin session closed.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    def student_logout():
        session.clear()
        flash("Signed out successfully.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/admin/account/update")
    def admin_account_update():
        current_password = request.form.get("current_password", "")
        login_username = request.form.get("login_username", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        recovery_telegram_username = request.form.get("recovery_telegram_username", "").strip()
        try:
            if not _verify_admin_password(current_password):
                raise ValueError("Current password is not valid.")
            validated_username = _validate_admin_login_username(login_username)
            normalized_recovery = (
                _normalize_telegram_username(recovery_telegram_username)
                if recovery_telegram_username.strip()
                else ""
            )
            if normalized_recovery and (not _service().telegram.configured or not _service().settings.telegram_admin_chat_ids):
                raise ValueError("Telegram recovery cannot be enabled until the Telegram bot token and admin chat ids are configured.")
            if new_password or confirm_password:
                password_to_store = _validate_admin_password(new_password, confirm_password)
                password_changed = True
            else:
                password_to_store = current_password
                password_changed = False
            _persist_admin_account_credentials(
                username=validated_username,
                password=password_to_store,
                recovery_telegram_username=normalized_recovery,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))

        _log_admin_action(
            action="update_admin_account",
            target_type="admin_account",
            target_id=validated_username,
            details=(
                f"Admin login username updated to {validated_username} and Telegram recovery username "
                f"{normalized_recovery or 'disabled'}; password {'changed' if password_changed else 'kept'}."
            ),
        )
        flash("Admin login settings updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/student/password/request")
    def student_password_change_request():
        student = _current_student()
        if not student:
            return redirect(url_for("login_alias", next=url_for("dashboard")))
        try:
            _issue_student_password_reset_code(student.site_login_username, purpose="change_password")
        except (ValueError, StudentValidationError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        except TelegramError as exc:
            _report_internal_exception("Student password change code delivery failed.", exc)
            flash("Verification code could not be sent to Telegram right now.", "error")
            return redirect(url_for("dashboard"))
        flash("A verification code was sent to your Telegram account.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/student/password/change")
    def student_password_change_submit():
        student = _current_student()
        if not student:
            return redirect(url_for("login_alias", next=url_for("dashboard")))

        reset_code = request.form.get("reset_code", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        try:
            validated_password = _validate_site_password(new_password, confirm_password)
            _consume_student_password_reset_code(student, reset_code)
            _service().update_student_site_password(student_id=student.id, new_password=validated_password)
            _notify_student_password_change(student, change_type="self_service")
            _service().log_admin_action(
                actor=f"student:{student.site_login_username}",
                action="student_password_change",
                target_type="student",
                target_id=str(student.id),
                details="Student website password was changed from the signed-in dashboard.",
            )
        except (ValueError, StudentValidationError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))

        flash("Your website password has been changed.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/student/profile/update")
    def student_profile_update():
        student = _current_student()
        if not student:
            return redirect(url_for("login_alias", next=url_for("dashboard")))

        previous_student = student
        supplied_erp_password = request.form.get("password", "")
        try:
            saved_id = _service().save_student(
                student_id=student.id,
                student_label=request.form.get("student_label", ""),
                user_name=request.form.get("user_name", ""),
                password=supplied_erp_password,
                site_login_username=student.site_login_username,
                site_login_password="",
                whatsapp_number="",
                telegram_chat_id=request.form.get("telegram_chat_id", ""),
                email_address="",
                enabled=student.enabled,
                timezone=request.form.get("timezone", ""),
            )
            updated_student = _service().get_student(saved_id)
            if not updated_student:
                raise ValueError("Updated student profile could not be loaded.")
            changes = _describe_student_profile_changes(
                previous_student,
                updated_student,
                erp_password_changed=bool((supplied_erp_password or "").strip()),
            )
            if changes:
                admin_activity = _format_student_profile_update_activity(changes)
                _service().db.update_student_bot_activity(
                    updated_student.id,
                    admin_activity,
                )
                try:
                    _service().log_admin_action(
                        actor=f"student:{updated_student.site_login_username}",
                        action="student_profile_update",
                        target_type="student",
                        target_id=str(updated_student.id),
                        details="; ".join(changes),
                    )
                except Exception as exc:
                    _report_internal_exception("Student profile update audit logging failed.", exc)
                admin_telegram_notified = _notify_admin_student_profile_update(updated_student, changes)
                if admin_telegram_notified:
                    flash("Profile updated successfully. The admin dashboard and admin Telegram notification have been updated.", "success")
                else:
                    flash("Profile updated successfully. The admin dashboard has been updated.", "success")
            else:
                flash("No profile changes were detected.", "warning")
        except StudentValidationError as exc:
            flash(str(exc), "error")
        except ValueError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            _report_internal_exception("Student profile update failed unexpectedly.", exc)
            flash("Profile could not be updated due to an internal error.", "error")
        return redirect(url_for("dashboard"))

    @app.get("/")
    def dashboard():
        edit_id = None
        if _is_admin_authenticated():
            edit_id = _parse_positive_int(request.args.get("edit"), default=0) or None
            if edit_id:
                edit_student = _service().get_student(edit_id)
                if edit_student and _service().is_student_action_disabled(edit_student, "edit"):
                    flash("Edit is disabled for this student profile.", "error")
                    edit_id = None
        return _render_dashboard(edit_id=edit_id)

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
                "telegram_configured": service.telegram.configured,
                "student_count": len(students),
                "task_queue_mode": service.settings.task_queue_mode,
                "run_scheduler_configured": service.settings.run_scheduler,
                "scheduler_active": _scheduler_is_running(scheduler),
                "sentry_enabled": bool(app.config.get("sentry_enabled")),
                "outbound_queue": service.get_outbound_queue_summary(),
            }
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
        if student_id:
            existing_student = service.get_student(student_id)
            if not existing_student:
                flash("Student profile not found.", "error")
                return _render_dashboard(), 404
            if service.is_student_action_disabled(existing_student, "edit"):
                flash("Edit is disabled for this student profile.", "error")
                return _render_dashboard(), 403
        try:
            saved_id = service.save_student(
                student_id=student_id,
                student_label=request.form.get("student_label", ""),
                user_name=request.form.get("user_name", ""),
                password=request.form.get("password", ""),
                site_login_username=request.form.get("site_login_username", ""),
                site_login_password=request.form.get("site_login_password", ""),
                whatsapp_number="",
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

    @app.post("/students/<int:student_id>/controls")
    def update_student_controls(student_id: int):
        service = _service()
        student = service.get_student(student_id)
        if not student:
            flash("Student profile not found.", "error")
            return redirect(url_for("dashboard"))
        try:
            updated_student = service.update_student_controls(
                student_id=student_id,
                enabled=request.form.get("enabled") == "on",
                notification_channel_mode=request.form.get("notification_channel_mode", "telegram_only"),
                disabled_actions=request.form.getlist("disabled_actions"),
            )
        except StudentValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            _report_internal_exception("Student control update failed unexpectedly.", exc)
            flash("Student controls could not be updated due to an internal error.", "error")
            return redirect(url_for("dashboard"))

        _log_admin_action(
            action="update_student_controls",
            target_type="student",
            target_id=str(student_id),
            details=(
                f"Profile {'enabled' if updated_student.enabled else 'blocked'}, "
                f"delivery {service.get_student_notification_channel_label(updated_student)}, "
                f"disabled actions: "
                f"{', '.join(service.get_student_disabled_actions(updated_student)) or 'none'}."
            ),
        )
        flash("Student controls updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/applications")
    def submit_application():
        limit_response = _enforce_rate_limit(
            bucket=f"application:{_client_ip()}",
            limit=settings.admin_rate_limit_count,
            window_seconds=settings.admin_rate_limit_window_seconds,
        )
        if limit_response is not None:
            return limit_response
        service = _service()
        try:
            result = service.submit_application_request(
                applicant_name=request.form.get("applicant_name", ""),
                student_label=request.form.get("student_label", ""),
                user_name=request.form.get("user_name", ""),
                password=request.form.get("password", ""),
                site_login_username=request.form.get("site_login_username", ""),
                site_login_password=request.form.get("site_login_password", ""),
                whatsapp_number="",
                telegram_chat_id=request.form.get("telegram_chat_id", ""),
                timezone=request.form.get("timezone", ""),
                reg_id=request.form.get("reg_id", ""),
                note=request.form.get("note", ""),
                created_from_ip=_client_ip(),
            )
        except StudentValidationError as exc:
            flash(str(exc), "error")
            return _render_dashboard(), 400
        except Exception as exc:
            _report_internal_exception("Public application submission failed unexpectedly.", exc)
            flash("The application could not be submitted right now. Please try again shortly.", "error")
            return _render_dashboard(), 500

        try:
            service.log_admin_action(
                actor=f"public-application:{_client_ip()}",
                action="submit_application_request",
                target_type="application_request",
                target_id=str(result["id"]),
                details="A new public application request was submitted from the website dashboard.",
            )
        except Exception as exc:
            _report_internal_exception("Public application audit logging failed.", exc)
        if result["notification_sent"]:
            flash(
                "Application submitted successfully. Your website account is active, but student features will remain unavailable until an administrator approves the request. The admin team has been notified on Telegram.",
                "success",
            )
        else:
            flash(
                "Application submitted successfully. Your website account is active, but student features will remain unavailable until an administrator approves the request. The request was saved, but the Telegram admin notification could not be delivered right now.",
                "warning",
            )
        return redirect(url_for("dashboard"))

    @app.post("/applications/<int:application_id>/accept")
    def accept_application(application_id: int):
        service = _service()
        try:
            result = service.approve_application_request(
                application_id,
                site_login_username=request.form.get("site_login_username", ""),
                site_login_password=request.form.get("site_login_password", ""),
            )
        except StudentValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            _report_internal_exception("Application approval failed unexpectedly.", exc)
            flash("The application could not be accepted right now. Please try again.", "error")
            return redirect(url_for("dashboard"))

        student = result["student"]
        site_login_username = str(result["site_login_username"])
        website_password_source = str(result["website_password_source"])
        site_login_password_display = str(result["site_login_password_display"] or "")
        _log_admin_action(
            action="accept_application_request",
            target_type="application_request",
            target_id=str(application_id),
            details=f"Application approved and student profile {student.id} was created.",
        )
        if website_password_source == "application_signup":
            flash(
                f"Application approved. {student.student_label} can sign in with username {site_login_username} using the website password created during sign-up.",
                "success",
            )
        elif website_password_source == "erp_submitted":
            flash(
                f"Application approved. {student.student_label} can sign in with username {site_login_username} using the ERP password submitted with the application.",
                "success",
            )
        elif website_password_source == "generated":
            flash(
                f"Application approved. {student.student_label} can sign in with username {site_login_username} using the temporary website password {site_login_password_display}.",
                "success",
            )
        else:
            flash(
                f"Application approved. {student.student_label} can sign in with username {site_login_username} using the website password {site_login_password_display}.",
                "success",
            )
        if result["notification_sent"]:
            flash("Approval notification sent to the student's Telegram account.", "success")
        elif result["notification_error"]:
            flash("The student profile was created, but the Telegram approval notification could not be delivered.", "warning")
        return redirect(url_for("dashboard", edit=student.id))

    @app.post("/applications/<int:application_id>/reject")
    def reject_application(application_id: int):
        service = _service()
        try:
            result = service.reject_application_request(application_id)
        except StudentValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            _report_internal_exception("Application rejection failed unexpectedly.", exc)
            flash("The application could not be rejected right now. Please try again.", "error")
            return redirect(url_for("dashboard"))

        application = result["application"]
        _log_admin_action(
            action="reject_application_request",
            target_type="application_request",
            target_id=str(application_id),
            details="Application rejected and removed from the active review queue.",
        )
        flash(
            f"Application rejected. {application.student_label}'s request has been removed from the active review queue.",
            "success",
        )
        if result["notification_sent"]:
            flash("Rejection notification sent to the applicant's Telegram account.", "success")
        elif result["notification_error"]:
            flash("The application was rejected, but the Telegram rejection notification could not be delivered.", "warning")
        return redirect(url_for("dashboard"))

    @app.post("/applications/<int:application_id>/clear")
    def clear_application(application_id: int):
        service = _service()
        try:
            application = service.clear_application_request(application_id)
        except StudentValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            _report_internal_exception("Application clear failed unexpectedly.", exc)
            flash("The application record could not be cleared right now. Please try again.", "error")
            return redirect(url_for("dashboard"))

        _log_admin_action(
            action="clear_application_request",
            target_type="application_request",
            target_id=str(application_id),
            details=f"Application record for {application.student_label} was cleared from the dashboard.",
        )
        flash(f"Application record for {application.student_label} was cleared from the dashboard.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/delete")
    def delete_student(student_id: int):
        student = _service().get_student(student_id)
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
        if student:
            _notify_admin_student_profile_deleted(student)
        flash("Student profile deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/login/start")
    def start_login(student_id: int):
        guard_response = _guard_admin_student_action(student_id, "start_login")
        if guard_response is not None:
            return guard_response
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
        guard_response = _guard_admin_student_action(student_id, "open_captcha")
        if guard_response is not None:
            return guard_response
        student = _service().get_student(student_id)
        pending = _service().db.get_pending_login(student_id)
        if not student or not pending:
            flash("No pending login session. Start login first.", "error")
            return redirect(url_for("dashboard"))
        return render_template("login.html", student=student, pending=pending)

    @app.post("/students/<int:student_id>/login/refresh")
    def refresh_login(student_id: int):
        guard_response = _guard_admin_student_action(student_id, "open_captcha")
        if guard_response is not None:
            return guard_response
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
        guard_response = _guard_admin_student_action(student_id, "open_captcha")
        if guard_response is not None:
            return guard_response
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
        guard_response = _guard_admin_student_action(student_id, "preview_today")
        if guard_response is not None:
            return guard_response
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
        guard_response = _guard_admin_student_action(student_id, "send_morning", require_delivery=True)
        if guard_response is not None:
            return guard_response
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

    @app.post("/students/<int:student_id>/send-substitution-report")
    def send_substitution_report(student_id: int):
        guard_response = _guard_dashboard_student_action(student_id, "send_substitution_report", require_delivery=True)
        if guard_response is not None:
            return guard_response
        try:
            _service().send_substitution_report(student_id, force=True)
        except (ERPClientError, WhatsAppError, TelegramError, NotificationDeliveryError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        _log_dashboard_student_action(
            action="send_substitution_report",
            target_type="student",
            target_id=str(student_id),
            details="Manual substitution report sent. Automatic substitution alerts remain enabled.",
        )
        flash("Manual substitution report sent to configured channels. Automatic substitution alerts still run in the background.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/send-attendance-summary")
    def send_attendance_summary(student_id: int):
        guard_response = _guard_admin_student_action(student_id, "send_attendance_summary", require_delivery=True)
        if guard_response is not None:
            return guard_response
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
        guard_response = _guard_admin_student_action(student_id, "send_day_report", require_delivery=True)
        if guard_response is not None:
            return guard_response
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
        guard_response = _guard_admin_student_action(student_id, "send_shortage_report", require_delivery=True)
        if guard_response is not None:
            return guard_response
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
        guard_response = _guard_admin_student_action(student_id, "send_channel_test", require_delivery=True)
        if guard_response is not None:
            return guard_response
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
        is_admin_authenticated = _is_admin_authenticated()
        is_student_authenticated = _is_student_authenticated()
        is_pending_authenticated = _is_pending_application_authenticated()
        if _expects_authenticated_dashboard_request() and not (is_admin_authenticated or is_student_authenticated or is_pending_authenticated):
            return _auth_required_response(
                _dashboard_reauth_url(request.path),
                "Your dashboard session expired. Please sign in again.",
            )
        current_student = _current_student()
        current_application_account = _current_pending_application()
        students = [current_student] if is_student_authenticated and current_student else (service.list_students() if is_admin_authenticated else [])
        student_dashboard_views = _build_student_dashboard_views(students)
        dashboard_now = service._local_now()
        student_automation_statuses = {
            student.id: service.get_student_automation_status(student, now=dashboard_now)
            for student in students
        }
        if is_admin_authenticated:
            dead_letter_messages = service.get_dead_letter_messages(10)
            outbound_summary = service.get_outbound_queue_summary()
            message_state = _build_message_history_state(service, request.args)
        elif current_student:
            dead_letter_messages = service.get_dead_letter_messages_for_student(current_student.id, 10)
            outbound_summary = service.get_outbound_queue_summary_for_student(current_student.id)
            message_state = _build_message_history_state(service, request.args, student_id=current_student.id)
        else:
            dead_letter_messages = []
            outbound_summary = {"claimed": 0, "sent": 0, "failed": 0, "dead_letter": 0}
            message_state = {
                "rows": [],
                "filters": {"query": "", "channel": "", "category": ""},
                "pagination": {"page": 1, "per_page": 20, "total_items": 0, "total_pages": 1, "has_prev": False, "has_next": False, "prev_page": 1, "next_page": 1},
            }
        audit_state = _build_audit_log_state(service, request.args) if is_admin_authenticated else {
            "rows": [],
            "filters": {"query": "", "action": ""},
            "pagination": {"page": 1, "per_page": 20, "total_items": 0, "total_pages": 1, "has_prev": False, "has_next": False, "prev_page": 1, "next_page": 1},
        }
        action_center = (
            _build_action_center(
                students=students,
                student_dashboard_views=student_dashboard_views,
                outbound_summary=outbound_summary,
                dead_letters=dead_letter_messages,
                dashboard_now=dashboard_now,
            )
            if is_admin_authenticated
            else []
        )
        scheduler_overview = _build_scheduler_overview(dashboard_now)
        response = jsonify(
            {
                "hero_live_grid_html": render_template(
                    "partials/hero_live_grid.html",
                    scheduler_overview=scheduler_overview,
                    is_admin_authenticated=is_admin_authenticated,
                    is_student_authenticated=is_student_authenticated,
                    is_pending_authenticated=is_pending_authenticated,
                    current_application_account=current_application_account,
                    settings=service.settings,
                ),
                "student_cards_html": render_template(
                    "partials/student_cards.html" if (is_admin_authenticated or is_student_authenticated) else "partials/public_prototype_cards.html",
                    students=students,
                    student_dashboard_views=student_dashboard_views,
                    student_automation_statuses=student_automation_statuses,
                    is_admin_authenticated=is_admin_authenticated,
                    is_student_authenticated=is_student_authenticated,
                    is_pending_authenticated=is_pending_authenticated,
                    current_application_account=current_application_account,
                    can_view_student_details=bool(is_admin_authenticated or is_student_authenticated),
                    settings=service.settings,
                ),
                "action_center_html": (
                    render_template(
                        "partials/action_center_content.html",
                        action_center=action_center,
                    )
                    if is_admin_authenticated
                    else ""
                ),
                "message_history_html": (
                    render_template(
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
                    )
                    if is_admin_authenticated or is_student_authenticated
                    else ""
                ),
                "dead_letter_html": (
                    render_template(
                        "partials/dead_letter_content.html",
                        dead_letter_messages=dead_letter_messages,
                        can_retry_dead_letters=is_admin_authenticated,
                    )
                    if is_admin_authenticated or is_student_authenticated
                    else ""
                ),
                "audit_log_html": (
                    render_template(
                        "partials/audit_log_results.html",
                        audit_log=audit_state["rows"],
                        audit_log_filters=audit_state["filters"],
                        audit_log_pagination=audit_state["pagination"],
                        message_history_filters=message_state["filters"],
                        message_history_pagination=message_state["pagination"],
                    )
                    if is_admin_authenticated
                    else ""
                ),
                "outbound_summary": outbound_summary,
            }
        )
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response

    return app


def _service() -> BotService:
    from flask import current_app

    return current_app.config["service"]


def _is_ajax_request() -> bool:
    requested_with = request.headers.get("X-Requested-With", "")
    accept = request.headers.get("Accept", "")
    return requested_with == "XMLHttpRequest" or "application/json" in accept.lower()


def _auth_required_response(login_url: str, message: str):
    if _is_ajax_request():
        return jsonify({"message": message, "reload": True, "login_url": login_url}), 401
    return redirect(login_url)


def _dashboard_auth_role() -> str:
    role = request.headers.get("X-Dashboard-Auth-Role", "").strip().lower()
    if role in {"admin", "student", "pending", "public"}:
        return role
    return "public"


def _expects_authenticated_dashboard_request() -> bool:
    return _dashboard_auth_role() in {"admin", "student", "pending"}


def _dashboard_reauth_url(next_path: str) -> str:
    if _dashboard_auth_role() == "admin":
        return url_for("admin_login", next=next_path)
    return url_for("login_alias", next=next_path)


def _csrf_retry_location() -> str:
    endpoint = request.endpoint or ""
    if endpoint == "admin_login_submit":
        return url_for("admin_login", next=_safe_next_path(request.form.get("next_path")))
    if endpoint == "student_login_submit":
        next_path = _safe_next_path(request.form.get("next_path"))
        return url_for("login_alias", next=next_path)
    if endpoint in {"admin_forgot_password_request", "admin_forgot_password_reset"}:
        return url_for("admin_forgot_password")
    if endpoint in {"student_forgot_password_request", "student_forgot_password_reset"}:
        return url_for("student_forgot_password")
    if endpoint in {"student_password_change_request", "student_password_change_submit"}:
        return url_for("dashboard")
    referrer = request.referrer or ""
    if referrer:
        return referrer
    return url_for("dashboard")


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _admin_auth_enabled() -> bool:
    account = _get_admin_account_state()
    return bool(account["username"] and account["password_available"])


def _get_admin_account_state() -> dict[str, object]:
    service = _service()
    db = service.db
    username_override = (db.get_runtime_state(ADMIN_USERNAME_OVERRIDE_KEY) or "").strip()
    password_hash_override = (db.get_runtime_state(ADMIN_PASSWORD_HASH_OVERRIDE_KEY) or "").strip()
    recovery_override = (db.get_runtime_state(ADMIN_TELEGRAM_USERNAME_OVERRIDE_KEY) or "").strip()
    username = username_override or service.settings.admin_username
    recovery_username = recovery_override or service.settings.admin_telegram_username
    normalized_recovery = ""
    if recovery_username:
        try:
            normalized_recovery = _normalize_telegram_username(recovery_username)
        except ValueError:
            normalized_recovery = ""
    return {
        "username": username,
        "password_hash_override": password_hash_override,
        "password_available": bool(password_hash_override or service.settings.admin_password),
        "recovery_telegram_username": normalized_recovery,
    }


def _verify_admin_password(password: str) -> bool:
    account = _get_admin_account_state()
    password_hash_override = str(account["password_hash_override"] or "")
    if password_hash_override:
        return bool(password) and check_password_hash(password_hash_override, password)
    return bool(password) and secrets.compare_digest(password, _service().settings.admin_password)


def _verify_admin_credentials(username: str, password: str) -> bool:
    account = _get_admin_account_state()
    expected_username = str(account["username"] or "").strip()
    if not expected_username or not secrets.compare_digest(username, expected_username):
        return False
    return _verify_admin_password(password)


def _persist_admin_account_credentials(
    *,
    username: str,
    password: str,
    recovery_telegram_username: str,
) -> None:
    db = _service().db
    db.upsert_runtime_state(state_key=ADMIN_USERNAME_OVERRIDE_KEY, state_value=username)
    db.upsert_runtime_state(
        state_key=ADMIN_PASSWORD_HASH_OVERRIDE_KEY,
        state_value=generate_password_hash(password),
    )
    if recovery_telegram_username:
        db.upsert_runtime_state(
            state_key=ADMIN_TELEGRAM_USERNAME_OVERRIDE_KEY,
            state_value=recovery_telegram_username,
        )
    else:
        db.delete_runtime_state(ADMIN_TELEGRAM_USERNAME_OVERRIDE_KEY)


def _normalize_telegram_username(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError("Telegram username is required.")
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned.split(prefix, 1)[1].strip("/")
            break
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    if not cleaned or not cleaned.replace("_", "a").isalnum() or not (5 <= len(cleaned) <= 32):
        raise ValueError("Telegram username must be a valid @username or t.me link.")
    return f"@{cleaned}"


def _validate_admin_login_username(value: str) -> str:
    cleaned = str(value or "").strip()
    if len(cleaned) < 3 or len(cleaned) > 64:
        raise ValueError("Login username must be between 3 and 64 characters.")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(ch not in allowed for ch in cleaned):
        raise ValueError("Login username can use letters, numbers, dots, underscores, and hyphens only.")
    return cleaned


def _validate_admin_password(password: str, confirm_password: str) -> str:
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters long.")
    if password != confirm_password:
        raise ValueError("Password confirmation does not match.")
    return password


def _issue_admin_password_reset_code(telegram_username: str) -> str:
    service = _service()
    account = _get_admin_account_state()
    expected_recovery = str(account["recovery_telegram_username"] or "")
    if not expected_recovery:
        raise ValueError("Telegram password recovery is not configured yet. Sign in and set a recovery Telegram username first.")
    normalized = _normalize_telegram_username(telegram_username)
    if normalized != expected_recovery:
        raise ValueError("Telegram username does not match the configured recovery username.")
    if not service.telegram.configured or not service.settings.telegram_admin_chat_ids:
        raise ValueError("Telegram password recovery is not available on this deployment.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ADMIN_RESET_CODE_TTL_MINUTES)
    payload = json.dumps(
        {
            "code_hash": generate_password_hash(code),
            "expires_at": expires_at.replace(microsecond=0).isoformat(),
            "telegram_username": normalized,
        }
    )
    service.db.upsert_runtime_state(state_key=ADMIN_RESET_STATE_KEY, state_value=payload)
    message = (
        "QUMS Bot admin password reset code\n"
        f"Code: {code}\n"
        f"Expires in: {ADMIN_RESET_CODE_TTL_MINUTES} minutes\n"
        "If you did not request this, ignore this message."
    )
    for chat_id in service.settings.telegram_admin_chat_ids:
        service.telegram.send_text(chat_id, message, message_kind="admin_password_reset")
    return normalized


def _consume_admin_password_reset_code(telegram_username: str, reset_code: str) -> None:
    raw_state = _service().db.get_runtime_state(ADMIN_RESET_STATE_KEY)
    if not raw_state:
        raise ValueError("Reset code is not active. Request a new code first.")
    try:
        payload = json.loads(raw_state)
    except json.JSONDecodeError:
        _service().db.delete_runtime_state(ADMIN_RESET_STATE_KEY)
        raise ValueError("Reset code is not active. Request a new code first.") from None

    expires_at_raw = str(payload.get("expires_at") or "")
    expected_username = str(payload.get("telegram_username") or "")
    code_hash = str(payload.get("code_hash") or "")
    if not expires_at_raw or not expected_username or not code_hash:
        _service().db.delete_runtime_state(ADMIN_RESET_STATE_KEY)
        raise ValueError("Reset code is not active. Request a new code first.")
    if telegram_username != expected_username:
        raise ValueError("Telegram username does not match the active reset request.")
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        _service().db.delete_runtime_state(ADMIN_RESET_STATE_KEY)
        raise ValueError("Reset code is not active. Request a new code first.") from None
    if expires_at < datetime.now(timezone.utc):
        _service().db.delete_runtime_state(ADMIN_RESET_STATE_KEY)
        raise ValueError("Reset code has expired. Request a new code again.")
    if not reset_code or not check_password_hash(code_hash, reset_code):
        raise ValueError("Reset code is not valid.")
    _service().db.delete_runtime_state(ADMIN_RESET_STATE_KEY)


def _student_reset_state_key(student_id: int) -> str:
    return f"{STUDENT_RESET_STATE_PREFIX}{student_id}"


def _require_student_for_site_login(login_username: str):
    student = _service().get_student_by_site_login_username(login_username)
    if not student or not student.enabled or not student.site_login_username or not student.site_password_hash:
        raise ValueError("Student login username is not valid.")
    return student


def _validate_site_password(password: str, confirm_password: str) -> str:
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters long.")
    if password != confirm_password:
        raise ValueError("Password confirmation does not match.")
    return password


def _issue_student_password_reset_code(login_username: str, *, purpose: str):
    service = _service()
    student = _require_student_for_site_login(login_username)
    if not student.telegram_chat_id:
        raise ValueError("Telegram recovery is not configured for this user yet.")
    if not service.telegram.configured:
        raise ValueError("Telegram password recovery is not available on this deployment.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=STUDENT_RESET_CODE_TTL_MINUTES)
    payload = json.dumps(
        {
            "code_hash": generate_password_hash(code),
            "expires_at": expires_at.replace(microsecond=0).isoformat(),
            "student_id": student.id,
            "purpose": purpose,
        }
    )
    service.db.upsert_runtime_state(state_key=_student_reset_state_key(student.id), state_value=payload)
    message = (
        "QUMS Bot website password verification code\n"
        f"User: {student.site_login_username}\n"
        f"Code: {code}\n"
        f"Expires in: {STUDENT_RESET_CODE_TTL_MINUTES} minutes\n"
        "If you did not request this, ignore this message."
    )
    service.telegram.send_text(student.telegram_chat_id, message, message_kind="student_password_reset")
    return student


def _consume_student_password_reset_code(student, reset_code: str) -> None:
    raw_state = _service().db.get_runtime_state(_student_reset_state_key(student.id))
    if not raw_state:
        raise ValueError("Reset code is not active. Request a new code first.")
    try:
        payload = json.loads(raw_state)
    except json.JSONDecodeError:
        _service().db.delete_runtime_state(_student_reset_state_key(student.id))
        raise ValueError("Reset code is not active. Request a new code first.") from None
    expires_at_raw = str(payload.get("expires_at") or "")
    code_hash = str(payload.get("code_hash") or "")
    student_id = int(payload.get("student_id") or 0)
    if student_id != student.id or not expires_at_raw or not code_hash:
        _service().db.delete_runtime_state(_student_reset_state_key(student.id))
        raise ValueError("Reset code is not active. Request a new code first.")
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        _service().db.delete_runtime_state(_student_reset_state_key(student.id))
        raise ValueError("Reset code is not active. Request a new code first.") from None
    if expires_at < datetime.now(timezone.utc):
        _service().db.delete_runtime_state(_student_reset_state_key(student.id))
        raise ValueError("Reset code has expired. Request a new code again.")
    if not reset_code or not check_password_hash(code_hash, reset_code):
        raise ValueError("Reset code is not valid.")
    _service().db.delete_runtime_state(_student_reset_state_key(student.id))


def _notify_student_password_change(student, *, change_type: str) -> None:
    service = _service()
    changed_at = service._format_datetime(service._local_now())
    service.db.update_student_bot_activity(
        student.id,
        f"Website password changed for {student.site_login_username}.",
    )
    if student.telegram_chat_id:
        service.telegram.send_text(
            student.telegram_chat_id,
            "\n".join(
                [
                    "QUMS Bot password updated",
                    f"Login username: {student.site_login_username}",
                    f"Changed at: {changed_at}",
                    "Your website password was changed successfully.",
                ]
            ),
            message_kind="student_password_changed",
        )
    admin_message = "\n".join(
        [
            "Student website password changed",
            f"Student: {student.student_label}",
            f"Login username: {student.site_login_username}",
            f"Telegram: {student.telegram_chat_id or 'Not set'}",
            f"Changed at: {changed_at}",
            f"Source: {'Forgot password reset' if change_type == 'forgot_password_reset' else 'Signed-in student change'}",
        ]
    )
    for chat_id in service.settings.telegram_admin_chat_ids:
        service.telegram.send_text(chat_id, admin_message, message_kind="student_password_changed_admin")


def _describe_student_profile_changes(previous_student, updated_student, *, erp_password_changed: bool) -> list[str]:
    changes: list[str] = []
    field_pairs = [
        ("Student label", previous_student.student_label, updated_student.student_label),
        ("ERP user ID", previous_student.user_name, updated_student.user_name),
        ("Telegram chat id", previous_student.telegram_chat_id or "Not set", updated_student.telegram_chat_id or "Not set"),
        ("Timezone", previous_student.timezone, updated_student.timezone),
    ]
    for label, old_value, new_value in field_pairs:
        if (old_value or "") != (new_value or ""):
            changes.append(f"{label}: {old_value or 'Not set'} -> {new_value or 'Not set'}")
    if erp_password_changed:
        changes.append("ERP password updated")
    return changes


def _format_student_profile_update_activity(changes: list[str]) -> str:
    return "Student profile self-service update: " + "; ".join(changes)


def _notify_admin_student_profile_update(student, changes: list[str]) -> bool:
    service = _service()
    if not service.telegram.configured or not service.settings.telegram_admin_chat_ids:
        return False
    message = "\n".join(
        [
            "Student profile updated from the self-service dashboard",
            f"Student: {student.student_label}",
            f"Login username: {student.site_login_username}",
            "Admin dashboard status: synchronized automatically",
            f"Updated at: {service._format_datetime(service._local_now())}",
            "Updated fields:",
            *[f"- {item}" for item in changes],
        ]
    )
    delivered = False
    for chat_id in service.settings.telegram_admin_chat_ids:
        try:
            service.telegram.send_text(chat_id, message, message_kind="student_profile_updated")
            delivered = True
        except TelegramError as exc:
            _report_internal_exception("Student profile update Telegram notification failed.", exc)
    return delivered


def _notify_admin_student_profile_deleted(student) -> None:
    service = _service()
    if not service.telegram.configured or not service.settings.telegram_admin_chat_ids:
        return
    message = "\n".join(
        [
            "Student profile deleted",
            f"Student: {student.student_label}",
            f"ERP user id: {student.user_name}",
            f"Login username: {student.site_login_username or 'Not set'}",
            f"Deleted at: {service._format_datetime(service._local_now())}",
        ]
    )
    for chat_id in service.settings.telegram_admin_chat_ids:
        try:
            service.telegram.send_text(chat_id, message, message_kind="student_profile_deleted")
        except TelegramError as exc:
            _report_internal_exception("Student profile deletion Telegram notification failed.", exc)


def _render_dashboard(*, edit_id: int | None = None, preview_text: str | None = None):
    service = _service()
    is_admin_authenticated = _is_admin_authenticated()
    is_student_authenticated = _is_student_authenticated()
    is_pending_authenticated = _is_pending_application_authenticated()
    current_student = _current_student()
    current_application_account = _current_pending_application()
    viewer_role = _viewer_role()
    students = [current_student] if is_student_authenticated and current_student else (service.list_students() if is_admin_authenticated else [])
    student_dashboard_views = _build_student_dashboard_views(students)
    message_history_options = service.get_message_history_filter_options() if (is_admin_authenticated or is_student_authenticated) else {"channels": [], "categories": []}
    audit_log_options = service.get_admin_audit_filter_options() if is_admin_authenticated else {"actions": []}
    edit_student = service.get_student(edit_id) if edit_id and is_admin_authenticated else None
    if edit_student and service.is_student_action_disabled(edit_student, "edit"):
        edit_student = None
    settings = service.settings
    dashboard_now = service._local_now()
    if is_admin_authenticated:
        outbound_summary = service.get_outbound_queue_summary()
        dead_letter_messages = service.get_dead_letter_messages(10)
        message_state = _build_message_history_state(service, request.args)
    elif current_student:
        outbound_summary = service.get_outbound_queue_summary_for_student(current_student.id)
        dead_letter_messages = service.get_dead_letter_messages_for_student(current_student.id, 10)
        message_state = _build_message_history_state(service, request.args, student_id=current_student.id)
    else:
        outbound_summary = {"claimed": 0, "sent": 0, "failed": 0, "dead_letter": 0}
        dead_letter_messages = []
        message_state = {
            "rows": [],
            "filters": {"query": "", "channel": "", "category": ""},
            "pagination": {"page": 1, "per_page": 20, "total_items": 0, "total_pages": 1, "has_prev": False, "has_next": False, "prev_page": 1, "next_page": 1},
        }
    audit_state = _build_audit_log_state(service, request.args) if is_admin_authenticated else {
        "rows": [],
        "filters": {"query": "", "action": ""},
        "pagination": {"page": 1, "per_page": 20, "total_items": 0, "total_pages": 1, "has_prev": False, "has_next": False, "prev_page": 1, "next_page": 1},
    }
    admin_account = _get_admin_account_state()
    student_automation_statuses = {
        student.id: service.get_student_automation_status(student, now=dashboard_now)
        for student in students
    }
    action_center = (
        _build_action_center(
            students=students,
            student_dashboard_views=student_dashboard_views,
            outbound_summary=outbound_summary,
            dead_letters=dead_letter_messages,
            dashboard_now=dashboard_now,
        )
        if is_admin_authenticated
        else []
    )
    application_requests = service.list_application_requests(12) if is_admin_authenticated else []
    application_request_summary = _build_application_request_summary(application_requests) if is_admin_authenticated else {
        "total_count": 0,
        "pending_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "reviewed_count": 0,
    }
    viewer_session = {
        "username": (
            session.get("admin_username")
            if is_admin_authenticated
            else (
                current_student.site_login_username
                if current_student
                else (current_application_account.site_login_username if current_application_account else "Guest")
            )
        ),
        "login_path": url_for("login_alias"),
        "admin_login_path": url_for("admin_login"),
        "authenticated": bool(is_admin_authenticated or is_student_authenticated or is_pending_authenticated),
        "role": viewer_role,
    }
    return render_template(
        "dashboard.html",
        students=students,
        student_dashboard_views=student_dashboard_views,
        edit_student=edit_student,
        preview_text=preview_text,
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
        admin_account=admin_account,
        viewer_session=viewer_session,
        current_student=current_student,
        is_admin_authenticated=is_admin_authenticated,
        is_student_authenticated=is_student_authenticated,
        is_pending_authenticated=is_pending_authenticated,
        current_application_account=current_application_account,
        can_view_student_details=bool(is_admin_authenticated or is_student_authenticated),
        can_retry_dead_letters=is_admin_authenticated,
        application_requests=application_requests,
        application_request_summary=application_request_summary,
        application_request_views=_build_application_request_views(application_requests),
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
    student_id: int | None = None,
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
    total_items = service.count_message_history(student_id=student_id, **filters)
    page_rows, pagination = _paginate_rows(
        total_items=total_items,
        page=page,
        per_page=per_page,
        fetch_page=lambda limit, offset: service.get_message_history_page(limit=limit, offset=offset, student_id=student_id, **filters),
    )
    return {"rows": page_rows, "filters": filters, "pagination": pagination}


def _build_message_export_state(service, params, *, student_id: int | None = None):
    filters = {
        "query": params.get("message_q", "").strip(),
        "channel": params.get("message_channel", "").strip().lower(),
        "category": params.get("message_category", "").strip().lower(),
    }
    total_items = service.count_message_history(student_id=student_id, **filters)
    if params.get("page") or params.get("per_page"):
        page = _parse_positive_int(params.get("page"), default=1)
        per_page = _parse_bounded_positive_int(params.get("per_page"), default=20, maximum=500)
        page_rows, pagination = _paginate_rows(
            total_items=total_items,
            page=page,
            per_page=per_page,
            fetch_page=lambda limit, offset: service.get_message_history_page(limit=limit, offset=offset, student_id=student_id, **filters),
        )
        return {"rows": page_rows, "filters": filters, "pagination": pagination}
    rows = (
        service.get_message_history_page(limit=max(total_items, 1), offset=0, student_id=student_id, **filters)
        if total_items
        else []
    )
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
        notification_mode = service.get_student_notification_channel_mode(student)
        disabled_actions = service.get_student_disabled_actions(student)
        blocked = not student.enabled
        action_states = {}
        for action_key in STUDENT_ACTION_ORDER:
            disabled = action_key in disabled_actions or (blocked and action_key != "edit")
            if notification_mode == "paused" and action_key in DELIVERY_BASED_STUDENT_ACTIONS:
                disabled = True
            action_states[action_key] = {
                "label": STUDENT_ACTION_LABELS[action_key],
                "disabled": disabled,
            }
        views[student.id] = {
            "erp_status": erp_status,
            "recent_activity": recent_activity,
            "captcha_ready": pending_login is not None,
            "last_erp_sync_at": last_erp_sync_at,
            "last_bot_action_at": last_bot_action_at,
            "blocked": blocked,
            "notification_channel_mode": notification_mode,
            "notification_channel_label": NOTIFICATION_CHANNEL_MODE_LABELS[notification_mode],
            "disabled_actions": sorted(disabled_actions),
            "disabled_action_labels": [STUDENT_ACTION_LABELS[action_key] for action_key in STUDENT_ACTION_ORDER if action_key in disabled_actions],
            "action_states": action_states,
        }
    return views


def _build_application_request_views(application_requests):
    views: dict[int, dict[str, str | None]] = {}
    service = _service()
    for item in application_requests:
        password_value = ""
        try:
            password_value = decrypt_text(service.settings.app_secret, item.password_encrypted)
        except Exception:
            password_value = ""
        views[item.id] = {
            "created_at": _format_dashboard_timestamp(item.created_at) or item.created_at,
            "updated_at": _format_dashboard_timestamp(item.updated_at) or item.updated_at,
            "password_value": password_value,
            "site_login_username_default": item.site_login_username or item.user_name,
            "site_password_configured": "yes" if item.site_password_hash else "",
            "site_login_password_default": "",
        }
    return views


def _build_application_request_summary(application_requests):
    total_count = len(application_requests)
    accepted_count = 0
    rejected_count = 0
    pending_count = 0
    for item in application_requests:
        status = _normalize_application_request_status(item.status)
        if status == "accepted":
            accepted_count += 1
        elif status == "rejected":
            rejected_count += 1
        else:
            pending_count += 1
    return {
        "total_count": total_count,
        "pending_count": pending_count,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "reviewed_count": accepted_count + rejected_count,
    }


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


def _build_action_center(*, students, student_dashboard_views, outbound_summary, dead_letters, dashboard_now):
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
    actor = str(_get_admin_account_state()["username"] or "dashboard-admin") if session.get("admin_authenticated") else "dashboard"
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


def _log_dashboard_student_action(*, action: str, target_type: str, target_id: str, details: str) -> None:
    current_student = _current_student()
    if current_student and not session.get("admin_authenticated"):
        try:
            _service().log_admin_action(
                actor=f"student:{current_student.site_login_username}",
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        except Exception as exc:
            _report_internal_exception("Student dashboard audit logging failed.", exc)
        return
    _log_admin_action(action=action, target_type=target_type, target_id=target_id, details=details)


def _report_internal_exception(message: str, exc: Exception) -> None:
    current_app.logger.exception(message)
    capture_monitoring_exception(exc)


def _safe_next_path(value: str | None) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return url_for("dashboard")
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc or not candidate.startswith("/"):
        return url_for("dashboard")
    return candidate


def _normalize_application_request_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    return normalized or "new"


def _is_admin_authenticated() -> bool:
    if not _admin_auth_enabled():
        return True
    return bool(session.get("admin_authenticated"))


def _current_student():
    if not session.get("student_authenticated"):
        _promote_pending_application_session()
    if not session.get("student_authenticated"):
        return None
    student_id = session.get("student_id")
    if not student_id:
        return None
    try:
        student = _service().get_student(int(student_id))
    except (TypeError, ValueError):
        return None
    if not student or not student.enabled or not student.site_login_username or not student.site_password_hash:
        return None
    return student


def _is_student_authenticated() -> bool:
    return _current_student() is not None


def _current_pending_application():
    if not session.get("pending_application_authenticated"):
        return None
    application_id = session.get("pending_application_id")
    if not application_id:
        return None
    try:
        application = _service().get_application_request(int(application_id))
    except (TypeError, ValueError):
        return None
    if not application or not application.site_login_username or not application.site_password_hash:
        return None
    return application


def _promote_pending_application_session() -> bool:
    if not session.get("pending_application_authenticated"):
        return False
    application = _current_pending_application()
    if not application or str(application.status or "").strip().lower() != "accepted":
        return False
    try:
        student = _service().get_student_by_site_login_username(application.site_login_username)
    except StudentValidationError:
        student = None
    if not student or not student.enabled or not student.site_login_username or not student.site_password_hash:
        return False
    session.pop("pending_application_authenticated", None)
    session.pop("pending_application_id", None)
    session.pop("pending_application_username", None)
    session["student_authenticated"] = True
    session["student_id"] = student.id
    session["student_username"] = student.site_login_username
    return True


def _is_pending_application_authenticated() -> bool:
    return _current_pending_application() is not None


def _viewer_role() -> str:
    if _is_admin_authenticated():
        return "admin"
    if _is_student_authenticated():
        return "student"
    if _is_pending_application_authenticated():
        return "pending"
    return "public"


def _guard_admin_student_action(student_id: int, action_key: str, *, require_delivery: bool = False):
    service = _service()
    student = service.get_student(student_id)
    if not student:
        flash("Student profile not found.", "error")
        return redirect(url_for("dashboard"))
    try:
        service.assert_student_action_allowed(student, action_key)
        if require_delivery:
            service.assert_student_notifications_available(student)
    except ERPClientError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard"))
    return None


def _guard_dashboard_student_action(student_id: int, action_key: str, *, require_delivery: bool = False):
    service = _service()
    student = service.get_student(student_id)
    if not student:
        flash("Student profile not found.", "error")
        return redirect(url_for("dashboard"))
    current_student = _current_student()
    if current_student and not session.get("admin_authenticated") and current_student.id != student.id:
        flash("You can only use actions for your own student profile.", "error")
        return redirect(url_for("dashboard"))
    try:
        service.assert_student_action_allowed(student, action_key)
        if require_delivery:
            service.assert_student_notifications_available(student)
    except ERPClientError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard"))
    return None


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
