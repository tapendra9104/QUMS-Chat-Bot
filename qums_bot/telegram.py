from __future__ import annotations

import re
from typing import Any

import requests

from .config import Settings


class TelegramError(Exception):
    pass


class TelegramSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    def send_text(
        self,
        chat_id: str,
        body: str,
        *,
        message_kind: str = "generic",
        reply_markup: dict[str, Any] | None = None,
    ) -> str:
        if not self.configured:
            raise TelegramError("Telegram bot token is missing in .env.")

        cleaned_chat_id = self._delivery_chat_id(chat_id)

        payload: dict[str, Any] = {
            "chat_id": cleaned_chat_id,
            "text": body,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        payload = self._request_json(
            "sendMessage",
            json=payload,
        )
        result = payload.get("result") or {}
        return str(result.get("message_id") or "")

    def edit_text(
        self,
        *,
        chat_id: str,
        message_id: str,
        body: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> str:
        payload_json: dict[str, Any] = {
            "chat_id": self._delivery_chat_id(chat_id),
            "message_id": int(message_id),
            "text": body,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload_json["reply_markup"] = reply_markup

        payload = self._request_json(
            "editMessageText",
            json=payload_json,
        )
        result = payload.get("result") or {}
        if isinstance(result, dict):
            return str(result.get("message_id") or message_id)
        return str(message_id)

    def send_document(
        self,
        *,
        chat_id: str,
        filename: str,
        content_bytes: bytes,
        caption: str | None = None,
    ) -> str:
        payload = self._request_json(
            "sendDocument",
            data={
                "chat_id": self._delivery_chat_id(chat_id),
                "caption": caption or "",
            },
            files={"document": (filename, content_bytes, "text/csv")},
        )
        result = payload.get("result") or {}
        return str(result.get("message_id") or "")

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 0,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "getUpdates",
            json={
                "offset": offset,
                "timeout": timeout_seconds,
                "allowed_updates": allowed_updates or ["message", "callback_query"],
            },
            timeout=max(timeout_seconds + 5, 15),
        )
        return list(payload.get("result") or [])

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self._request_json(
            "answerCallbackQuery",
            json={
                "callback_query_id": callback_query_id,
                "text": text or "",
                "show_alert": show_alert,
            },
        )

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self._request_json(
            "setMyCommands",
            json={"commands": commands},
        )

    def _request_json(
        self,
        method: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> dict[str, Any]:
        if not self.configured:
            raise TelegramError("Telegram bot token is missing in .env.")
        try:
            response = self._session.post(
                f"{self.settings.telegram_api_base_url}/bot{self.settings.telegram_bot_token}/{method}",
                json=json,
                data=data,
                files=files,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise TelegramError(f"Telegram request failed for {method}: {exc}") from exc

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram returned a non-JSON response with status {response.status_code} for {method}."
            ) from exc

        if not response.ok or not payload.get("ok"):
            detail = str(payload.get("description") or f"Telegram HTTP {response.status_code}")
            raise TelegramError(f"Telegram {method} failed: {detail}")
        return payload

    def _delivery_chat_id(self, chat_id: str) -> str:
        cleaned_chat_id = str(chat_id or "").strip()
        if not cleaned_chat_id:
            raise TelegramError("Telegram chat id is required for Telegram delivery.")
        if not re.fullmatch(r"-?\d{5,20}", cleaned_chat_id):
            raise TelegramError(
                "Telegram delivery requires a numeric chat id. Open the bot, send /start, "
                "then save that numeric chat id instead of an @username."
            )
        return cleaned_chat_id
