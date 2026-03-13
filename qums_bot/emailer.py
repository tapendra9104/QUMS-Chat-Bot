from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from .config import Settings


class EmailDeliveryError(Exception):
    pass


class EmailSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        if not (self.settings.smtp_host and self.settings.smtp_from_email):
            return False
        if self.settings.smtp_username and not self.settings.smtp_password:
            return False
        return True

    def send_text(self, to_email: str, body: str, *, subject: str, message_kind: str = "generic") -> str:
        if not self.configured:
            raise EmailDeliveryError("SMTP email settings are incomplete in .env.")

        recipient = str(to_email or "").strip()
        if not recipient:
            raise EmailDeliveryError("Email address is required for email delivery.")

        message = EmailMessage()
        message["From"] = self.settings.smtp_from_email
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-Id"] = make_msgid()
        message.set_content(body)

        try:
            if self.settings.smtp_use_ssl:
                with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as client:
                    self._authenticate(client)
                    client.send_message(message)
            else:
                with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as client:
                    client.ehlo()
                    if self.settings.smtp_use_tls:
                        client.starttls()
                        client.ehlo()
                    self._authenticate(client)
                    client.send_message(message)
        except smtplib.SMTPException as exc:
            raise EmailDeliveryError(f"Email delivery failed: {exc}") from exc
        except OSError as exc:
            raise EmailDeliveryError(f"Email connection failed: {exc}") from exc

        return message["Message-Id"]

    def _authenticate(self, client: smtplib.SMTP) -> None:
        if self.settings.smtp_username:
            client.login(self.settings.smtp_username, self.settings.smtp_password)
