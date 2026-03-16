from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, load_settings
from .db import Database
from .erp_client import ERPClient
from .service import BotService
from .telegram import TelegramSender


@dataclass(frozen=True)
class AppRuntime:
    settings: Settings
    db: Database
    erp_client: ERPClient
    telegram: TelegramSender
    service: BotService


def build_runtime(settings: Settings | None = None) -> AppRuntime:
    active_settings = settings or load_settings()
    db = Database(active_settings.database_path)
    db.init()
    erp_client = ERPClient(active_settings)
    telegram = TelegramSender(active_settings)
    service = BotService(
        settings=active_settings,
        db=db,
        erp_client=erp_client,
        telegram=telegram,
    )
    return AppRuntime(
        settings=active_settings,
        db=db,
        erp_client=erp_client,
        telegram=telegram,
        service=service,
    )
