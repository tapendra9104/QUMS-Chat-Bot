from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from typing import Any, Optional

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from .config import Settings


SANDBOX_SENDER = "whatsapp:+14155238886"
SANDBOX_EXPIRY_HOURS = 72


class WhatsAppError(Exception):
    pass


@dataclass(frozen=True)
class WhatsAppChannelStatus:
    configured: bool
    mode: str
    sender: str
    ready: bool
    state: str
    detail: str
    join_command: str | None = None
    last_inbound_at: str | None = None
    last_outbound_status: str | None = None
    last_error_code: int | None = None


class WhatsAppSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Optional[Client] = None

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.twilio_account_sid
            and self.settings.twilio_auth_token
            and self.settings.twilio_whatsapp_from
        )

    def send_text(self, to_number: str, body: str, *, message_kind: str = "generic") -> str:
        if not self.configured:
            raise WhatsAppError("Twilio WhatsApp credentials are missing in .env.")

        template_sid = self._template_sid_for(message_kind)
        if template_sid:
            return self._send_template(to_number, body, template_sid)

        message_sid = ""
        for index, chunk in enumerate(self._split_body(body)):
            if index:
                sleep(3)
            message_sid = self._send_chunk(to_number, chunk)
        return message_sid

    def get_channel_status(self, to_number: str) -> WhatsAppChannelStatus:
        sender = self._normalize_number(self.settings.twilio_whatsapp_from or SANDBOX_SENDER)
        join_command = self._join_command()
        if not self.configured:
            return WhatsAppChannelStatus(
                configured=False,
                mode=self.settings.twilio_whatsapp_mode,
                sender=sender,
                ready=False,
                state="not_configured",
                detail="Twilio credentials are incomplete.",
                join_command=join_command,
            )

        try:
            recent_messages = self._recent_messages(to_number)
        except TwilioRestException as exc:
            return WhatsAppChannelStatus(
                configured=True,
                mode=self.settings.twilio_whatsapp_mode,
                sender=sender,
                ready=False,
                state="twilio_error",
                detail=self._format_error(exc),
                join_command=join_command,
            )

        if self.settings.twilio_whatsapp_mode == "production":
            return self._production_status(to_number, recent_messages)
        return self._sandbox_status(to_number, recent_messages)

    @property
    def _client_instance(self) -> Client:
        if self._client is None:
            self._client = Client(
                self.settings.twilio_account_sid,
                self.settings.twilio_auth_token,
            )
        return self._client

    def _send_chunk(self, to_number: str, body: str) -> str:
        try:
            message = self._client_instance.messages.create(
                from_=self._normalize_number(self.settings.twilio_whatsapp_from),
                to=self._normalize_number(to_number),
                body=body,
            )
        except TwilioRestException as exc:
            raise WhatsAppError(self._format_error(exc)) from exc

        message = self._await_terminal_status(message.sid, initial_status=str(message.status or ""))
        status = str(message.status or "").lower()
        if status in {"failed", "undelivered"}:
            raise WhatsAppError(self._format_delivery_failure(message, to_number))
        return message.sid

    def _send_template(self, to_number: str, body: str, template_sid: str) -> str:
        payload = json.dumps({"1": body})
        try:
            message = self._client_instance.messages.create(
                from_=self._normalize_number(self.settings.twilio_whatsapp_from),
                to=self._normalize_number(to_number),
                content_sid=template_sid,
                content_variables=payload,
            )
        except TwilioRestException as exc:
            raise WhatsAppError(self._format_error(exc)) from exc

        message = self._await_terminal_status(message.sid, initial_status=str(message.status or ""))
        status = str(message.status or "").lower()
        if status in {"failed", "undelivered"}:
            raise WhatsAppError(self._format_delivery_failure(message, to_number))
        return message.sid

    def _await_terminal_status(self, sid: str, *, initial_status: str) -> Any:
        current = self._client_instance.messages(sid).fetch()
        status = str(current.status or initial_status or "").lower()
        deadline = monotonic() + 12
        while monotonic() < deadline and status in {"accepted", "queued", "sending", "scheduled", ""}:
            sleep(1)
            current = self._client_instance.messages(sid).fetch()
            status = str(current.status or "").lower()
        return current

    def _recent_messages(self, to_number: str) -> list[Any]:
        sender = self._normalize_number(self.settings.twilio_whatsapp_from)
        recipient = self._normalize_number(to_number)
        recent: list[Any] = []

        for message in self._client_instance.messages.list(limit=self.settings.twilio_status_message_limit):
            from_number = self._normalize_optional_number(getattr(message, "from_", None))
            to_number_value = self._normalize_optional_number(getattr(message, "to", None))
            participants = {value for value in {from_number, to_number_value} if value}
            if sender not in participants or recipient not in participants:
                continue
            recent.append(message)

        recent.sort(key=lambda item: getattr(item, "date_created", None) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return recent

    def _sandbox_status(self, to_number: str, recent_messages: list[Any]) -> WhatsAppChannelStatus:
        sender = self._normalize_number(self.settings.twilio_whatsapp_from)
        recipient = self._normalize_number(to_number)
        join_command = self._join_command()
        latest_outbound = self._latest_outbound(recent_messages, recipient)
        latest_inbound = self._latest_inbound(recent_messages, recipient)
        last_error = self._message_error_code(latest_outbound)

        if sender != SANDBOX_SENDER:
            return WhatsAppChannelStatus(
                configured=True,
                mode="sandbox",
                sender=sender,
                ready=False,
                state="sender_invalid",
                detail="Sandbox mode requires the Twilio sandbox sender whatsapp:+14155238886.",
                join_command=join_command,
                last_inbound_at=self._message_timestamp(latest_inbound),
                last_outbound_status=self._message_status(latest_outbound),
                last_error_code=last_error,
            )

        if latest_inbound and self._is_recent_sandbox_join(latest_inbound):
            if last_error == 63015:
                return WhatsAppChannelStatus(
                    configured=True,
                    mode="sandbox",
                    sender=sender,
                    ready=False,
                    state="sandbox_join_required",
                    detail=(
                        f"Sandbox delivery to {recipient} failed after the last join window. "
                        "Send the current join code from this WhatsApp number again."
                    ),
                    join_command=join_command,
                    last_inbound_at=self._message_timestamp(latest_inbound),
                    last_outbound_status=self._message_status(latest_outbound),
                    last_error_code=last_error,
                )

            return WhatsAppChannelStatus(
                configured=True,
                mode="sandbox",
                sender=sender,
                ready=True,
                state="sandbox_ready",
                detail=(
                    f"Sandbox session for {recipient} appears active. "
                    f"It expires about {SANDBOX_EXPIRY_HOURS} hours after the last inbound sandbox message."
                ),
                join_command=join_command,
                last_inbound_at=self._message_timestamp(latest_inbound),
                last_outbound_status=self._message_status(latest_outbound),
                last_error_code=last_error,
            )

        return WhatsAppChannelStatus(
            configured=True,
            mode="sandbox",
            sender=sender,
            ready=False,
            state="sandbox_join_required",
            detail=(
                f"{recipient} is not ready for sandbox delivery. "
                "The user must send the current sandbox join code from their own WhatsApp account."
            ),
            join_command=join_command,
            last_inbound_at=self._message_timestamp(latest_inbound),
            last_outbound_status=self._message_status(latest_outbound),
            last_error_code=last_error,
        )

    def _production_status(self, to_number: str, recent_messages: list[Any]) -> WhatsAppChannelStatus:
        sender = self._normalize_number(self.settings.twilio_whatsapp_from)
        recipient = self._normalize_number(to_number)
        latest_outbound = self._latest_outbound(recent_messages, recipient)
        last_error = self._message_error_code(latest_outbound)
        has_template = bool(
            self.settings.twilio_content_sid_default
            or self.settings.twilio_content_sid_morning
            or self.settings.twilio_content_sid_attendance
        )

        if sender == SANDBOX_SENDER:
            return WhatsAppChannelStatus(
                configured=True,
                mode="production",
                sender=sender,
                ready=False,
                state="sandbox_sender_in_production",
                detail="Production mode cannot use the Twilio sandbox sender. Configure a WhatsApp-enabled production sender.",
                last_outbound_status=self._message_status(latest_outbound),
                last_error_code=last_error,
            )

        if not has_template:
            return WhatsAppChannelStatus(
                configured=True,
                mode="production",
                sender=sender,
                ready=False,
                state="template_missing",
                detail=(
                    "Production sender is configured, but no WhatsApp content template SID is set. "
                    "Scheduled messages outside the 24-hour service window will fail."
                ),
                last_outbound_status=self._message_status(latest_outbound),
                last_error_code=last_error,
            )

        if last_error == 63016:
            return WhatsAppChannelStatus(
                configured=True,
                mode="production",
                sender=sender,
                ready=False,
                state="template_required",
                detail="Recent production delivery failed outside the 24-hour window. Use an approved WhatsApp template SID.",
                last_outbound_status=self._message_status(latest_outbound),
                last_error_code=last_error,
            )

        return WhatsAppChannelStatus(
            configured=True,
            mode="production",
            sender=sender,
            ready=True,
            state="production_ready",
            detail="Production sender and template configuration are present.",
            last_outbound_status=self._message_status(latest_outbound),
            last_error_code=last_error,
        )

    def _template_sid_for(self, message_kind: str) -> str:
        if self.settings.twilio_whatsapp_mode != "production":
            return ""
        if message_kind == "morning" and self.settings.twilio_content_sid_morning:
            return self.settings.twilio_content_sid_morning
        if message_kind == "attendance" and self.settings.twilio_content_sid_attendance:
            return self.settings.twilio_content_sid_attendance
        if message_kind in {"morning", "attendance"}:
            return self.settings.twilio_content_sid_default
        return ""

    def _join_command(self) -> str | None:
        code = self.settings.twilio_sandbox_join_code.strip()
        return f"join {code}" if code else None

    def _normalize_number(self, value: str) -> str:
        cleaned = value.strip()
        return cleaned if cleaned.startswith("whatsapp:") else f"whatsapp:{cleaned}"

    def _normalize_optional_number(self, value: str | None) -> str:
        if not value:
            return ""
        return self._normalize_number(value)

    def _latest_outbound(self, messages: list[Any], recipient: str) -> Any | None:
        sender = self._normalize_number(self.settings.twilio_whatsapp_from)
        for message in messages:
            if self._normalize_optional_number(getattr(message, "from_", None)) != sender:
                continue
            if self._normalize_optional_number(getattr(message, "to", None)) != recipient:
                continue
            return message
        return None

    def _latest_inbound(self, messages: list[Any], recipient: str) -> Any | None:
        sender = self._normalize_number(self.settings.twilio_whatsapp_from)
        for message in messages:
            if self._normalize_optional_number(getattr(message, "from_", None)) != recipient:
                continue
            if self._normalize_optional_number(getattr(message, "to", None)) != sender:
                continue
            return message
        return None

    def _is_recent_sandbox_join(self, message: Any) -> bool:
        created = getattr(message, "date_created", None)
        if not created:
            return False
        created_utc = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created_utc <= timedelta(hours=SANDBOX_EXPIRY_HOURS)

    def _message_status(self, message: Any | None) -> str | None:
        if not message:
            return None
        value = str(getattr(message, "status", "") or "").strip().lower()
        return value or None

    def _message_error_code(self, message: Any | None) -> int | None:
        if not message:
            return None
        value = getattr(message, "error_code", None)
        return int(value) if value is not None else None

    def _message_timestamp(self, message: Any | None) -> str | None:
        if not message:
            return None
        created = getattr(message, "date_created", None)
        if not created:
            return None
        return created.isoformat()

    def _split_body(self, body: str, limit: int = 1500) -> list[str]:
        if len(body) <= limit:
            return [body]

        chunks: list[str] = []
        current = ""
        for line in body.splitlines():
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= limit:
                current = candidate
                continue

            if current:
                chunks.append(current)
                current = ""

            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line

        if current:
            chunks.append(current)

        return chunks or [body[:limit]]

    def _format_error(self, exc: TwilioRestException) -> str:
        details = str(exc)
        sender = self._normalize_number(self.settings.twilio_whatsapp_from)
        lower_details = details.lower()

        if "could not find a channel with the specified from address" in lower_details:
            return (
                f"Twilio cannot send WhatsApp from {sender}. "
                "TWILIO_WHATSAPP_FROM must be a WhatsApp-enabled sender. "
                "Use whatsapp:+14155238886 for sandbox, or configure a production WhatsApp sender in Twilio."
            )

        if "not a valid whatsapp-capable inbound message address" in lower_details:
            return (
                "Twilio rejected the destination number. Make sure the recipient is a WhatsApp number in E.164 format, "
                f"for example whatsapp:+91XXXXXXXXXX. Twilio said: {details}"
            )

        if "outside the allowed window" in lower_details or "template" in lower_details:
            return (
                "Twilio blocked the message because business-initiated WhatsApp messages can require an approved template "
                f"outside the 24-hour service window. Twilio said: {details}"
            )

        return details

    def _format_delivery_failure(self, message: Any, to_number: str) -> str:
        error_code = int(getattr(message, "error_code", 0) or 0)
        status = str(getattr(message, "status", "") or "").lower()
        details = str(getattr(message, "error_message", "") or "").strip()
        destination = self._normalize_number(to_number)

        if error_code == 63015:
            join_help = ""
            if self._join_command():
                join_help = f" Send `{self._join_command()}` from that WhatsApp number to +14155238886."
            return (
                f"Twilio could not deliver to {destination}. "
                "This WhatsApp number has not joined your Twilio sandbox, or its sandbox session expired."
                f"{join_help}"
            )

        if error_code == 63007:
            sender = self._normalize_number(self.settings.twilio_whatsapp_from)
            return (
                f"Twilio cannot send WhatsApp from {sender}. "
                "Use a WhatsApp-enabled sender such as the Twilio sandbox number or a production WhatsApp sender."
            )

        if error_code == 21617:
            return (
                "Twilio rejected the message because the WhatsApp body was too long. "
                "The bot splits long plain-text messages automatically, but your production template may still be too restrictive."
            )

        if error_code == 63016:
            return (
                "Twilio blocked the message because it is outside the WhatsApp customer service window. "
                "Configure an approved WhatsApp template SID for scheduled production messages."
            )

        if details:
            return f"Twilio delivery failed with status {status}: {details}"
        return f"Twilio delivery failed with status {status} and error code {error_code}."
