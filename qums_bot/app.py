from __future__ import annotations

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from .config import load_settings
from .db import Database
from .erp_client import ERPClient, ERPClientError
from .scheduler import build_scheduler
from .service import BotService
from .whatsapp import WhatsAppError, WhatsAppSender


def create_app() -> Flask:
    settings = load_settings()
    db = Database(settings.database_path)
    db.init()

    service = BotService(
        settings=settings,
        db=db,
        erp_client=ERPClient(settings),
        whatsapp=WhatsAppSender(settings),
    )

    app = Flask(__name__, template_folder="templates")
    app.secret_key = settings.app_secret
    app.config["service"] = service
    app.config["settings"] = settings

    scheduler = build_scheduler(settings, service)
    scheduler.start()
    app.config["scheduler"] = scheduler

    @app.before_request
    def require_admin_login():
        if request.endpoint in {"healthz", "admin_login", "admin_login_submit", "static"}:
            return None
        if not _admin_auth_enabled():
            return None
        if session.get("admin_authenticated"):
            return None
        return redirect(url_for("admin_login", next=request.path))

    @app.get("/admin/login")
    def admin_login():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))
        if session.get("admin_authenticated"):
            return redirect(url_for("dashboard"))
        return render_template("admin_login.html", next_path=request.args.get("next") or url_for("dashboard"))

    @app.post("/admin/login")
    def admin_login_submit():
        if not _admin_auth_enabled():
            return redirect(url_for("dashboard"))

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_path = request.form.get("next_path") or url_for("dashboard")

        if username == settings.admin_username and password == settings.admin_password:
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
        return jsonify(
            {
                "status": "ok",
                "app_env": service.settings.app_env,
                "use_waitress": service.settings.use_waitress,
                "twilio_mode": service.settings.twilio_whatsapp_mode,
                "twilio_configured": service.whatsapp.configured,
                "student_count": len(students),
            }
        )

    @app.post("/students")
    def save_student():
        service = _service()
        student_id = request.form.get("student_id") or None
        try:
            saved_id = service.save_student(
                student_id=int(student_id) if student_id else None,
                student_label=request.form.get("student_label", ""),
                user_name=request.form.get("user_name", ""),
                password=request.form.get("password", ""),
                whatsapp_number=request.form.get("whatsapp_number", ""),
                enabled=request.form.get("enabled") == "on",
                timezone=request.form.get("timezone", ""),
            )
        except Exception as exc:
            flash(str(exc), "error")
            return _render_dashboard(edit_id=int(student_id) if student_id else None), 400

        flash("Student profile saved.", "success")
        return redirect(url_for("dashboard", edit=saved_id))

    @app.post("/students/<int:student_id>/delete")
    def delete_student(student_id: int):
        deleted = _service().delete_student(student_id)
        if not deleted:
            flash("Student profile not found.", "error")
            return redirect(url_for("dashboard"))
        flash("Student profile deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/login/start")
    def start_login(student_id: int):
        try:
            _service().start_login(student_id)
        except ERPClientError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
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
        flash(message, "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/preview")
    def preview_today(student_id: int):
        try:
            preview_text = _service().preview_today(student_id)
        except (ERPClientError, WhatsAppError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        flash("Preview generated from the current ERP session.", "success")
        return _render_dashboard(preview_text=preview_text)

    @app.post("/students/<int:student_id>/send-morning")
    def send_morning(student_id: int):
        try:
            _service().send_morning_update(student_id)
        except (ERPClientError, WhatsAppError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        flash("Morning summary sent to WhatsApp.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/students/<int:student_id>/send-test")
    def send_test(student_id: int):
        try:
            _service().send_test_message(student_id)
        except WhatsAppError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))
        flash("Test WhatsApp message sent.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/checks/run")
    def run_checks():
        _service().run_due_checks()
        flash("Attendance checks executed.", "success")
        return redirect(url_for("dashboard"))

    return app


def _service() -> BotService:
    from flask import current_app

    return current_app.config["service"]


def _admin_auth_enabled() -> bool:
    service = _service()
    return bool(service.settings.admin_username and service.settings.admin_password)


def _render_dashboard(*, edit_id: int | None = None, preview_text: str | None = None):
    service = _service()
    students = service.list_students()
    edit_student = service.get_student(edit_id) if edit_id else None
    settings = service.settings
    whatsapp_statuses = {student.id: service.get_whatsapp_status(student) for student in students}
    return render_template(
        "dashboard.html",
        students=students,
        edit_student=edit_student,
        preview_text=preview_text,
        whatsapp_statuses=whatsapp_statuses,
        twilio_configured=service.whatsapp.configured,
        settings=settings,
    )
