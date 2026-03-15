from __future__ import annotations

import argparse
import secrets
import shlex
import socket
import sys
import tarfile
import tempfile
from pathlib import Path

import paramiko
from dotenv import dotenv_values


EXCLUDES = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".tmp-test",
    "__pycache__",
    "backups",
    "data",
    "logs",
    "manual-checks",
    "manual-test-suite",
    "tmp-runtime-check",
    "tmp-test2",
}

DEFAULT_ENV = {
    "ERP_BASE_URL": "https://qums.quantumuniversity.edu.in",
    "APP_ENV": "production",
    "USE_WAITRESS": "1",
    "WAITRESS_THREADS": "4",
    "DASHBOARD_AUTO_REFRESH_SECONDS": "1",
    "RUN_SCHEDULER": "1",
    "TASK_QUEUE_MODE": "inline",
    "REDIS_URL": "",
    "TASK_QUEUE_NAME": "qums-bot",
    "LOCAL_TIMEZONE": "Asia/Kolkata",
    "MORNING_DIGEST_TIME": "06:30",
    "EVENING_REPORT_TIME": "19:00",
    "ATTENDANCE_POLL_INTERVAL_MINUTES": "1",
    "SUBSTITUTION_POLL_INTERVAL_MINUTES": "1",
    "MONITOR_POLL_INTERVAL_MINUTES": "1",
    "SANDBOX_EXPIRY_WARNING_MINUTES": "10",
    "LECTURE_GRACE_MINUTES": "20",
    "ATTENDANCE_CORRECTION_LOOKBACK_DAYS": "14",
    "ATTENDANCE_SHORTAGE_BUFFER_LECTURES": "1",
    "DELIVERY_RETRY_LIMIT": "3",
    "DELIVERY_RETRY_BACKOFF_SECONDS": "60",
    "LOW_ATTENDANCE_THRESHOLDS": "75,70,65",
    "FLASK_HOST": "127.0.0.1",
    "FLASK_PORT": "5000",
    "WEBHOOK_RATE_LIMIT_COUNT": "60",
    "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS": "60",
    "ADMIN_RATE_LIMIT_COUNT": "10",
    "ADMIN_RATE_LIMIT_WINDOW_SECONDS": "60",
    "SENTRY_DSN": "",
    "SENTRY_TRACES_SAMPLE_RATE": "0.0",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "TWILIO_WHATSAPP_MODE": "sandbox",
    "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
    "TWILIO_SANDBOX_JOIN_CODE": "",
    "TWILIO_STATUS_MESSAGE_LIMIT": "50",
    "TWILIO_STATUS_CALLBACK_URL": "",
    "TWILIO_CONTENT_SID_DEFAULT": "",
    "TWILIO_CONTENT_SID_MORNING": "",
    "TWILIO_CONTENT_SID_ATTENDANCE": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_API_BASE_URL": "https://api.telegram.org",
    "TELEGRAM_ADMIN_CHAT_IDS": "",
    "TELEGRAM_POLL_INTERVAL_SECONDS": "1",
    "TELEGRAM_BOT_LINK": "",
    "OWNER_TELEGRAM_CONTACT": "",
    "OWNER_WHATSAPP_CONTACT": "",
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SMTP_FROM_EMAIL": "",
    "SMTP_USE_TLS": "1",
    "SMTP_USE_SSL": "0",
    "EMAIL_SUBJECT_PREFIX": "QUMS Bot",
}

ENV_ORDER = [
    "ERP_BASE_URL",
    "DATABASE_PATH",
    "APP_SECRET",
    "APP_ENV",
    "USE_WAITRESS",
    "WAITRESS_THREADS",
    "DASHBOARD_AUTO_REFRESH_SECONDS",
    "RUN_SCHEDULER",
    "TASK_QUEUE_MODE",
    "REDIS_URL",
    "TASK_QUEUE_NAME",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ADMIN_TELEGRAM_USERNAME",
    "LOCAL_TIMEZONE",
    "MORNING_DIGEST_TIME",
    "EVENING_REPORT_TIME",
    "ATTENDANCE_POLL_INTERVAL_MINUTES",
    "SUBSTITUTION_POLL_INTERVAL_MINUTES",
    "MONITOR_POLL_INTERVAL_MINUTES",
    "SANDBOX_EXPIRY_WARNING_MINUTES",
    "LECTURE_GRACE_MINUTES",
    "ATTENDANCE_CORRECTION_LOOKBACK_DAYS",
    "ATTENDANCE_SHORTAGE_BUFFER_LECTURES",
    "DELIVERY_RETRY_LIMIT",
    "DELIVERY_RETRY_BACKOFF_SECONDS",
    "LOW_ATTENDANCE_THRESHOLDS",
    "FLASK_HOST",
    "FLASK_PORT",
    "PUBLIC_BASE_URL",
    "WEBHOOK_RATE_LIMIT_COUNT",
    "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS",
    "ADMIN_RATE_LIMIT_COUNT",
    "ADMIN_RATE_LIMIT_WINDOW_SECONDS",
    "SENTRY_DSN",
    "SENTRY_TRACES_SAMPLE_RATE",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_MODE",
    "TWILIO_WHATSAPP_FROM",
    "TWILIO_SANDBOX_JOIN_CODE",
    "TWILIO_STATUS_MESSAGE_LIMIT",
    "TWILIO_STATUS_CALLBACK_URL",
    "TWILIO_CONTENT_SID_DEFAULT",
    "TWILIO_CONTENT_SID_MORNING",
    "TWILIO_CONTENT_SID_ATTENDANCE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_API_BASE_URL",
    "TELEGRAM_ADMIN_CHAT_IDS",
    "TELEGRAM_POLL_INTERVAL_SECONDS",
    "TELEGRAM_BOT_LINK",
    "OWNER_TELEGRAM_CONTACT",
    "OWNER_WHATSAPP_CONTACT",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL",
    "SMTP_USE_TLS",
    "SMTP_USE_SSL",
    "EMAIL_SUBJECT_PREFIX",
]


def quote(value: str) -> str:
    return shlex.quote(value)


def should_include(path: Path, repo_root: Path) -> bool:
    relative = path.relative_to(repo_root)
    parts = relative.parts
    if not parts:
        return True
    if parts[0] in EXCLUDES:
        return False
    if any(part == "__pycache__" for part in parts):
        return False
    if relative.name == ".env":
        return False
    return True


def create_archive(repo_root: Path) -> Path:
    archive_path = Path(tempfile.gettempdir()) / "qums-bot-deploy.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        for path in repo_root.rglob("*"):
            if not should_include(path, repo_root):
                continue
            arcname = path.relative_to(repo_root)
            tar.add(path, arcname=str(arcname))
    return archive_path


def build_remote_env(
    env_path: Path,
    *,
    app_dir: str,
    public_base_url: str,
    timezone: str,
    admin_username: str,
    admin_password: str,
    app_secret: str,
) -> tuple[Path, dict[str, str]]:
    source_values: dict[str, str] = {}
    if env_path.exists():
        source_values = {k: str(v) for k, v in dotenv_values(env_path).items() if v is not None}

    values = DEFAULT_ENV.copy()
    values.update(source_values)
    values["DATABASE_PATH"] = f"{app_dir}/data/bot.sqlite3"
    values["APP_ENV"] = "production"
    values["USE_WAITRESS"] = "1"
    values["RUN_SCHEDULER"] = "1"
    values["TASK_QUEUE_MODE"] = "inline"
    values["FLASK_HOST"] = "127.0.0.1"
    values["FLASK_PORT"] = "5000"
    values["LOCAL_TIMEZONE"] = timezone or values.get("LOCAL_TIMEZONE", "Asia/Kolkata")
    values["PUBLIC_BASE_URL"] = public_base_url or values.get("PUBLIC_BASE_URL", "")
    values["ADMIN_USERNAME"] = admin_username or values.get("ADMIN_USERNAME") or "admin"
    values["ADMIN_PASSWORD"] = admin_password or values.get("ADMIN_PASSWORD") or secrets.token_urlsafe(18)
    values["APP_SECRET"] = app_secret or values.get("APP_SECRET") or secrets.token_urlsafe(48)

    temp_env_path = Path(tempfile.gettempdir()) / "qums-bot-vps.env"
    with temp_env_path.open("w", encoding="utf-8", newline="\n") as handle:
        for key in ENV_ORDER:
            handle.write(f"{key}={values.get(key, '')}\n")
    return temp_env_path, values


def run_remote(client: paramiko.SSHClient, command: str, timeout: int = 600) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, stdout.read().decode(), stderr.read().decode()


def upload_file(sftp: paramiko.SFTPClient, local_path: Path, remote_path: str) -> None:
    remote_dir = str(Path(remote_path).parent).replace("\\", "/")
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        parts = remote_dir.strip("/").split("/")
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else f"/{part}"
            try:
                sftp.stat(current)
            except FileNotFoundError:
                sftp.mkdir(current)
    sftp.put(str(local_path), remote_path)


def write_console(text: str) -> None:
    data = text if text.endswith("\n") else f"{text}\n"
    try:
        sys.stdout.write(data)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(data.encode("utf-8", "replace"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy this workspace to a VPS over SSH/SFTP.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", required=True)
    parser.add_argument("--app-dir", default="/opt/qums-bot")
    parser.add_argument("--service-name", default="qums-bot")
    parser.add_argument("--app-user", default="qumsbot")
    parser.add_argument("--server-name", default="")
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument("--remote-tmp-dir", default="/tmp/qums-bot-deploy")
    parser.add_argument("--env-path", default=".env")
    parser.add_argument("--admin-username", default="")
    parser.add_argument("--admin-password", default="")
    parser.add_argument("--app-secret", default="")
    parser.add_argument("--use-sudo", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    bootstrap_path = repo_root / "scripts" / "bootstrap_vps.sh"
    if not bootstrap_path.exists():
        raise SystemExit(f"Bootstrap script not found: {bootstrap_path}")

    env_path = repo_root / args.env_path
    generated_env_path, generated_env = build_remote_env(
        env_path,
        app_dir=args.app_dir,
        public_base_url=args.public_base_url,
        timezone=args.timezone,
        admin_username=args.admin_username,
        admin_password=args.admin_password,
        app_secret=args.app_secret,
    )

    archive_path = create_archive(repo_root)
    server_name = args.server_name or args.host

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=args.host,
            port=args.port,
            username=args.user,
            password=args.password,
            timeout=30,
            auth_timeout=60,
            banner_timeout=60,
        )

        sftp = client.open_sftp()
        try:
            run_remote(client, f"mkdir -p {quote(args.remote_tmp_dir)}")
            upload_file(sftp, archive_path, f"{args.remote_tmp_dir}/app.tar.gz")
            upload_file(sftp, bootstrap_path, f"{args.remote_tmp_dir}/bootstrap_vps.sh")
            upload_file(sftp, generated_env_path, f"{args.remote_tmp_dir}/.env")
        finally:
            sftp.close()

        env_parts = [
            f"APP_DIR={quote(args.app_dir)}",
            f"SERVICE_NAME={quote(args.service_name)}",
            f"APP_USER={quote(args.app_user)}",
            "SOURCE_MODE=archive",
            f"ARCHIVE_PATH={quote(args.remote_tmp_dir + '/app.tar.gz')}",
            f"SERVER_NAME={quote(server_name)}",
            f"LOCAL_TIMEZONE_DEFAULT={quote(args.timezone)}",
        ]
        if args.public_base_url:
            env_parts.append(f"PUBLIC_BASE_URL_DEFAULT={quote(args.public_base_url)}")
        env_parts.append(f"ENV_FILE_SOURCE={quote(args.remote_tmp_dir + '/.env')}")

        remote_command = " ".join(env_parts + [f"bash {quote(args.remote_tmp_dir + '/bootstrap_vps.sh')}"])
        if args.use_sudo:
            remote_command = f"sudo env {remote_command}"

        exit_status, stdout, stderr = run_remote(client, remote_command, timeout=3600)
        if stdout.strip():
            write_console(stdout)
        if stderr.strip():
            write_console(stderr)
        if exit_status != 0:
            raise SystemExit(exit_status)

        cleanup_command = f"rm -rf {quote(args.remote_tmp_dir)}"
        run_remote(client, cleanup_command)
        write_console(f"Admin username: {generated_env['ADMIN_USERNAME']}")
        write_console(f"Admin password: {generated_env['ADMIN_PASSWORD']}")
        return 0
    except (socket.timeout, TimeoutError) as exc:
        raise SystemExit(f"SSH connection timed out: {exc}") from exc
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            archive_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            generated_env_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
