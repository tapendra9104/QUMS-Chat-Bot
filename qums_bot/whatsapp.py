from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


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
    sandbox_expires_at: str | None = None
    last_outbound_status: str | None = None
    last_error_code: int | None = None


class WhatsAppSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return False

    def send_text(self, to_number: str, body: str, *, message_kind: str = "generic") -> str:
        raise WhatsAppError("WhatsApp delivery has been removed from this project. Use Telegram instead.")

    def get_channel_status(self, to_number: str) -> WhatsAppChannelStatus:
        return WhatsAppChannelStatus(
            configured=False,
            mode="removed",
            sender="",
            ready=False,
            state="removed",
            detail="WhatsApp delivery has been removed from this project. Notifications now use Telegram only.",
        )
