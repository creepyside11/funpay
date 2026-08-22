from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import certifi
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from cryptography.fernet import Fernet, InvalidToken

from FunPayAPI import Account, Runner, events, types
from FunPayAPI import exceptions as fp_exceptions
from playerok_plugin_system import (
    PLAYEROK_READY_PLUGIN_BY_UUID,
    PLAYEROK_READY_PLUGINS,
    PlayerokPluginData,
    PlayerokPluginManager,
    PlayerokPluginValidationError,
    playerok_ready_plugin_source,
    playerok_setting_label,
)
from plugin_system import PluginData, PluginManager, PluginValidationError

try:
    from playerokapi.account import Account as PlayerokAccount
    from playerokapi.enums import ItemDealDirections as PlayerokItemDealDirections
    from playerokapi.enums import ItemDealStatuses as PlayerokItemDealStatuses
    from playerokapi.enums import ItemStatuses as PlayerokItemStatuses
except ImportError:  # Playerok — опциональная интеграция для локальных тестов без зависимости.
    PlayerokAccount = None
    PlayerokItemDealDirections = None
    PlayerokItemDealStatuses = None
    PlayerokItemStatuses = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("funpay_bot")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PLUGIN_DOCUMENTATION_PATH = Path(__file__).with_name("PLUGIN_DEVELOPMENT.md")
PLAYEROK_PLUGIN_DOCUMENTATION_PATH = Path(__file__).with_name(
    "PLAYEROK_PLUGIN_DEVELOPMENT.md"
)

AUTO_LOTS_PLUGIN_UUID = "77b095e0-13a1-4e12-9c52-3a7b83a89b11"
ADVANCED_STATS_PLUGIN_UUID = "c55a4072-eab8-4d87-8f17-b111e4b8bb22"
STATUS_PLUGIN_UUID = "b19339bb-8f13-49cb-a4c1-0d3a55e1cc33"
PLUGIN_SETTINGS_CALLBACK_PREFIX = "47"
PLUGIN_CATALOG_PAGE_SIZE = 6
PLUGIN_CATALOG_DESCRIPTION_MIN = 40
PLUGIN_CATALOG_DESCRIPTION_MAX = 2000
PLAYEROK_POLL_SECONDS = 20
PLAYEROK_AUTO_PUBLISH_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ReadyPluginSpec:
    uuid: str
    filename: str
    name: str
    version: str
    description: str
    details: str


READY_PLUGINS = (
    ReadyPluginSpec(
        AUTO_LOTS_PLUGIN_UUID,
        "AutoLotsPlugin.py",
        "AutoLotsPlugin",
        "1.0.0",
        "Массовое управление лотами",
        "Показывает активные и выключенные лоты, массово активирует или "
        "деактивирует обычные и валютные предложения. Обычные лоты можно массово удалить; "
        "валютные предложения при удалении безопасно деактивируются.",
    ),
    ReadyPluginSpec(
        ADVANCED_STATS_PLUGIN_UUID,
        "AdvancedProfileStats.py",
        "Advanced Profile Stats",
        "1.0.0",
        "Расширенная статистика профиля",
        "Считает продажи, закрытые и возвращённые заказы, уникальных покупателей, "
        "выручку и популярные лоты за выбранный период. Дополнительно показывает общий "
        "баланс, доступную к выводу сумму и средства на удержании.",
    ),
    ReadyPluginSpec(
        STATUS_PLUGIN_UUID,
        "StatusPlugin.py",
        "Status Plugin",
        "1.0.0",
        "Статус продавца в чатах FunPay",
        "Позволяет задать собственный текст статуса. Покупатель отправляет в личном чате "
        "FunPay команду #status и мгновенно получает настроенный ответ.",
    ),
)
READY_PLUGIN_BY_UUID = {plugin.uuid: plugin for plugin in READY_PLUGINS}


def ready_plugin_source(plugin: ReadyPluginSpec) -> str:
    """Возвращает валидный однофайловый модуль формата FunPayCardinal."""
    return (
        f"NAME = {plugin.name!r}\n"
        f"VERSION = {plugin.version!r}\n"
        f"DESCRIPTION = {plugin.description!r}\n"
        "CREDITS = 'FunPay aiogram bot'\n"
        "SETTINGS_PAGE = True\n"
        f"UUID = {plugin.uuid!r}\n"
        "BIND_TO_DELETE = None\n"
    )


def plugin_settings_callback_data(plugin: PluginData) -> str | None:
    """Возвращает постоянный callback страницы настроек установленного плагина."""
    if not plugin.settings_page:
        return None
    if plugin.uuid in READY_PLUGIN_BY_UUID:
        return f"builtin_open:{plugin.uuid}"
    return f"{PLUGIN_SETTINGS_CALLBACK_PREFIX}:{plugin.uuid}:0"


def validate_catalog_description(value: str) -> str:
    description = value.strip()
    if not PLUGIN_CATALOG_DESCRIPTION_MIN <= len(description) <= PLUGIN_CATALOG_DESCRIPTION_MAX:
        raise ValueError(
            f"Описание должно содержать от {PLUGIN_CATALOG_DESCRIPTION_MIN} "
            f"до {PLUGIN_CATALOG_DESCRIPTION_MAX} символов."
        )
    return description


def telegram_publisher_name(user: Any) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    return clipped(getattr(user, "full_name", "Пользователь Telegram"), 100)


@dataclass(slots=True)
class Config:
    bot_token: str
    database_url: str
    app_secret: str

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("BOT_TOKEN", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not token:
            raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
        if not database_url:
            raise RuntimeError("Не задана переменная окружения DATABASE_URL")
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url.removeprefix("postgres://")
        return cls(token, database_url, os.getenv("APP_SECRET", token))


class SecretBox:
    """Шифрует чувствительные поля перед сохранением в PostgreSQL."""

    def __init__(self, secret: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode()).decode()


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=10, command_timeout=30)
        await self.execute(
            """
            CREATE TABLE IF NOT EXISTS funpay_users (
                telegram_id BIGINT PRIMARY KEY,
                proxy_enc TEXT,
                golden_key_enc TEXT,
                funpay_id BIGINT,
                funpay_username TEXT,
                account_active BOOLEAN NOT NULL DEFAULT FALSE,
                active_marketplace TEXT NOT NULL DEFAULT 'funpay',
                playerok_proxy_enc TEXT,
                playerok_cookie_enc TEXT,
                playerok_id TEXT,
                playerok_username TEXT,
                playerok_active BOOLEAN NOT NULL DEFAULT FALSE,
                playerok_notify_messages BOOLEAN NOT NULL DEFAULT TRUE,
                playerok_notify_deals BOOLEAN NOT NULL DEFAULT TRUE,
                playerok_notify_reviews BOOLEAN NOT NULL DEFAULT TRUE,
                playerok_notify_system BOOLEAN NOT NULL DEFAULT TRUE,
                playerok_auto_publish_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                playerok_autoreply_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                playerok_autoreply_text TEXT NOT NULL DEFAULT 'Здравствуйте! Спасибо за сообщение. Скоро отвечу.',
                playerok_autoreply_cooldown_minutes INTEGER NOT NULL DEFAULT 30,
                playerok_autoreply_delay_seconds INTEGER NOT NULL DEFAULT 0,
                playerok_autoreply_work_start SMALLINT NOT NULL DEFAULT 0,
                playerok_autoreply_work_end SMALLINT NOT NULL DEFAULT 24,
                playerok_auto_delivery_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                playerok_auto_confirm_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                playerok_notify_delivery BOOLEAN NOT NULL DEFAULT TRUE,
                notify_messages BOOLEAN NOT NULL DEFAULT TRUE,
                notify_new_orders BOOLEAN NOT NULL DEFAULT TRUE,
                notify_order_status BOOLEAN NOT NULL DEFAULT TRUE,
                notify_reviews BOOLEAN NOT NULL DEFAULT TRUE,
                notify_lots_raise BOOLEAN NOT NULL DEFAULT TRUE,
                notify_system BOOLEAN NOT NULL DEFAULT TRUE,
                auto_raise_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                keep_online_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                autoreply_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                autoreply_text TEXT NOT NULL DEFAULT 'Здравствуйте! Спасибо за сообщение. Скоро отвечу.',
                autoreply_cooldown_minutes INTEGER NOT NULL DEFAULT 30,
                autoreply_delay_seconds INTEGER NOT NULL DEFAULT 0,
                autoreply_new_chats_only BOOLEAN NOT NULL DEFAULT FALSE,
                autoreply_work_start SMALLINT NOT NULL DEFAULT 0,
                autoreply_work_end SMALLINT NOT NULL DEFAULT 24,
                review_reply_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                review_reply_1 TEXT NOT NULL DEFAULT 'Спасибо за обратную связь. Мы разберёмся в ситуации.',
                review_reply_2 TEXT NOT NULL DEFAULT 'Спасибо за отзыв. Нам жаль, что заказ вас разочаровал.',
                review_reply_3 TEXT NOT NULL DEFAULT 'Спасибо за отзыв! Учтём ваши замечания.',
                review_reply_4 TEXT NOT NULL DEFAULT 'Спасибо за хорошую оценку и ваш заказ!',
                review_reply_5 TEXT NOT NULL DEFAULT 'Спасибо за отличную оценку! Будем рады видеть вас снова.',
                auto_delivery_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                multi_delivery_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                delivery_auto_restore BOOLEAN NOT NULL DEFAULT TRUE,
                delivery_auto_disable BOOLEAN NOT NULL DEFAULT TRUE,
                notify_delivery BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_reviews BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS active_marketplace TEXT NOT NULL DEFAULT 'funpay';
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_proxy_enc TEXT;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_cookie_enc TEXT;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_id TEXT;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_username TEXT;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_active BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_notify_messages BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_notify_deals BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_notify_reviews BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_notify_system BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_auto_publish_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_autoreply_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_autoreply_text TEXT NOT NULL DEFAULT 'Здравствуйте! Спасибо за сообщение. Скоро отвечу.';
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_autoreply_cooldown_minutes INTEGER NOT NULL DEFAULT 30;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_autoreply_delay_seconds INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_autoreply_work_start SMALLINT NOT NULL DEFAULT 0;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_autoreply_work_end SMALLINT NOT NULL DEFAULT 24;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_auto_delivery_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_auto_confirm_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS playerok_notify_delivery BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS auto_delivery_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS multi_delivery_enabled BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS delivery_auto_restore BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS delivery_auto_disable BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_delivery BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_lots_raise BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_system BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS auto_raise_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS keep_online_enabled BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS autoreply_cooldown_minutes INTEGER NOT NULL DEFAULT 30;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS autoreply_delay_seconds INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS autoreply_new_chats_only BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS autoreply_work_start SMALLINT NOT NULL DEFAULT 0;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS autoreply_work_end SMALLINT NOT NULL DEFAULT 24;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS review_reply_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS review_reply_1 TEXT NOT NULL DEFAULT 'Спасибо за обратную связь. Мы разберёмся в ситуации.';
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS review_reply_2 TEXT NOT NULL DEFAULT 'Спасибо за отзыв. Нам жаль, что заказ вас разочаровал.';
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS review_reply_3 TEXT NOT NULL DEFAULT 'Спасибо за отзыв! Учтём ваши замечания.';
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS review_reply_4 TEXT NOT NULL DEFAULT 'Спасибо за хорошую оценку и ваш заказ!';
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS review_reply_5 TEXT NOT NULL DEFAULT 'Спасибо за отличную оценку! Будем рады видеть вас снова.';

            CREATE TABLE IF NOT EXISTS funpay_autoreply_log (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL,
                last_sent TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS funpay_plugins (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                uuid TEXT NOT NULL,
                filename TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                description TEXT NOT NULL,
                credits TEXT NOT NULL,
                settings_page BOOLEAN NOT NULL DEFAULT FALSE,
                source TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, uuid)
            );

            CREATE TABLE IF NOT EXISTS funpay_plugin_settings (
                telegram_id BIGINT NOT NULL,
                plugin_uuid TEXT NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, plugin_uuid, setting_key),
                FOREIGN KEY (telegram_id, plugin_uuid)
                    REFERENCES funpay_plugins(telegram_id, uuid) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS funpay_plugin_catalog (
                uuid TEXT PRIMARY KEY,
                owner_telegram_id BIGINT REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                publisher_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                short_description TEXT NOT NULL,
                description TEXT NOT NULL,
                credits TEXT NOT NULL,
                source TEXT NOT NULL,
                is_official BOOLEAN NOT NULL DEFAULT FALSE,
                install_count BIGINT NOT NULL DEFAULT 0,
                published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS funpay_plugin_catalog_order_idx
                ON funpay_plugin_catalog (is_official DESC, install_count DESC, updated_at DESC);

            CREATE TABLE IF NOT EXISTS funpay_delivery_rules (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                lot_id BIGINT NOT NULL,
                lot_title TEXT NOT NULL,
                response TEXT NOT NULL,
                products TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                disable_auto_restore BOOLEAN NOT NULL DEFAULT FALSE,
                disable_auto_disable BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (telegram_id, lot_id)
            );

            CREATE TABLE IF NOT EXISTS funpay_delivery_log (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                order_id TEXT NOT NULL,
                rule_id BIGINT REFERENCES funpay_delivery_rules(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, order_id)
            );

            CREATE TABLE IF NOT EXISTS funpay_command_replies (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                trigger TEXT NOT NULL,
                response TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                notify BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (telegram_id, trigger)
            );

            CREATE TABLE IF NOT EXISTS funpay_notification_targets (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                chat_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS playerok_autoreply_log (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL,
                last_sent TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS playerok_command_replies (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                trigger TEXT NOT NULL,
                response TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                notify BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (telegram_id, trigger)
            );

            CREATE TABLE IF NOT EXISTS playerok_delivery_rules (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                item_id TEXT NOT NULL,
                item_title TEXT NOT NULL,
                response TEXT NOT NULL,
                products TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (telegram_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS playerok_delivery_log (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                deal_id TEXT NOT NULL,
                rule_id BIGINT REFERENCES playerok_delivery_rules(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, deal_id)
            );

            CREATE TABLE IF NOT EXISTS playerok_plugins (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                uuid TEXT NOT NULL,
                filename TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                description TEXT NOT NULL,
                credits TEXT NOT NULL,
                settings_page BOOLEAN NOT NULL DEFAULT FALSE,
                source TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, uuid)
            );

            CREATE TABLE IF NOT EXISTS playerok_plugin_settings (
                telegram_id BIGINT NOT NULL,
                plugin_uuid TEXT NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, plugin_uuid, setting_key),
                FOREIGN KEY (telegram_id, plugin_uuid)
                    REFERENCES playerok_plugins(telegram_id, uuid) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS playerok_plugin_catalog (
                uuid TEXT PRIMARY KEY,
                owner_telegram_id BIGINT REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                publisher_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                short_description TEXT NOT NULL,
                description TEXT NOT NULL,
                credits TEXT NOT NULL,
                source TEXT NOT NULL,
                is_official BOOLEAN NOT NULL DEFAULT FALSE,
                install_count BIGINT NOT NULL DEFAULT 0,
                published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS playerok_plugin_catalog_order_idx
                ON playerok_plugin_catalog
                (is_official DESC, install_count DESC, updated_at DESC);
            """
        )
        await self.seed_official_plugins()
        await self.seed_official_playerok_plugins()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def execute(self, query: str, *args: Any) -> str:
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        return await self.pool.execute(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        return await self.pool.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        return await self.pool.fetch(query, *args)

    async def ensure_user(self, telegram_id: int) -> None:
        await self.execute(
            "INSERT INTO funpay_users (telegram_id) VALUES ($1) ON CONFLICT DO NOTHING",
            telegram_id,
        )

    async def get_user(self, telegram_id: int) -> asyncpg.Record | None:
        return await self.fetchrow("SELECT * FROM funpay_users WHERE telegram_id=$1", telegram_id)

    async def save_account(
        self,
        telegram_id: int,
        proxy_enc: str,
        golden_key_enc: str,
        account: Account,
    ) -> None:
        await self.ensure_user(telegram_id)
        await self.execute(
            """
            UPDATE funpay_users
               SET proxy_enc=$2, golden_key_enc=$3, funpay_id=$4, funpay_username=$5,
                   account_active=TRUE, active_marketplace='funpay', updated_at=NOW()
             WHERE telegram_id=$1
            """,
            telegram_id,
            proxy_enc,
            golden_key_enc,
            account.id,
            account.username,
        )

    async def disconnect_account(self, telegram_id: int) -> None:
        await self.execute(
            """
            UPDATE funpay_users
               SET proxy_enc=NULL, golden_key_enc=NULL, funpay_id=NULL, funpay_username=NULL,
                   account_active=FALSE,
                   active_marketplace=CASE WHEN playerok_active THEN 'playerok' ELSE 'funpay' END,
                   updated_at=NOW()
             WHERE telegram_id=$1
            """,
            telegram_id,
        )

    async def save_playerok_account(
        self,
        telegram_id: int,
        proxy_enc: str,
        cookie_enc: str,
        account: Any,
    ) -> None:
        await self.ensure_user(telegram_id)
        await self.execute(
            """
            UPDATE funpay_users
               SET playerok_proxy_enc=$2, playerok_cookie_enc=$3,
                   playerok_id=$4, playerok_username=$5, playerok_active=TRUE,
                   active_marketplace='playerok', updated_at=NOW()
             WHERE telegram_id=$1
            """,
            telegram_id,
            proxy_enc,
            cookie_enc,
            str(account.id),
            account.username,
        )

    async def disconnect_playerok_account(self, telegram_id: int) -> None:
        await self.execute(
            """
            UPDATE funpay_users
               SET playerok_proxy_enc=NULL, playerok_cookie_enc=NULL,
                   playerok_id=NULL, playerok_username=NULL, playerok_active=FALSE,
                   active_marketplace=CASE WHEN account_active THEN 'funpay' ELSE 'playerok' END,
                   updated_at=NOW()
             WHERE telegram_id=$1
            """,
            telegram_id,
        )

    async def set_active_marketplace(self, telegram_id: int, marketplace: str) -> None:
        if marketplace not in {"funpay", "playerok"}:
            raise ValueError("Неизвестная площадка")
        await self.execute(
            "UPDATE funpay_users SET active_marketplace=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            marketplace,
        )

    async def set_flag(self, telegram_id: int, column: str, value: bool) -> None:
        allowed = {
            "notify_messages",
            "notify_new_orders",
            "notify_order_status",
            "notify_reviews",
            "notify_lots_raise",
            "notify_system",
            "auto_raise_enabled",
            "keep_online_enabled",
            "autoreply_enabled",
            "autoreply_new_chats_only",
            "review_reply_enabled",
            "playerok_notify_messages",
            "playerok_notify_deals",
            "playerok_notify_reviews",
            "playerok_notify_system",
            "playerok_auto_publish_enabled",
            "playerok_autoreply_enabled",
            "playerok_auto_delivery_enabled",
            "playerok_auto_confirm_enabled",
            "playerok_notify_delivery",
            "auto_delivery_enabled",
            "multi_delivery_enabled",
            "delivery_auto_restore",
            "delivery_auto_disable",
            "notify_delivery",
        }
        if column not in allowed:
            raise ValueError("Недопустимая настройка")
        await self.execute(
            f"UPDATE funpay_users SET {column}=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            value,
        )

    async def set_autoreply_text(self, telegram_id: int, text: str) -> None:
        await self.execute(
            "UPDATE funpay_users SET autoreply_text=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            text,
        )

    async def set_playerok_autoreply_text(self, telegram_id: int, text: str) -> None:
        await self.execute(
            "UPDATE funpay_users SET playerok_autoreply_text=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            text,
        )

    async def set_integer_setting(self, telegram_id: int, column: str, value: int) -> None:
        allowed = {
            "autoreply_cooldown_minutes": (0, 1440),
            "autoreply_delay_seconds": (0, 300),
            "autoreply_work_start": (0, 23),
            "autoreply_work_end": (1, 24),
            "playerok_autoreply_cooldown_minutes": (0, 1440),
            "playerok_autoreply_delay_seconds": (0, 300),
            "playerok_autoreply_work_start": (0, 23),
            "playerok_autoreply_work_end": (1, 24),
        }
        if column not in allowed or not allowed[column][0] <= value <= allowed[column][1]:
            raise ValueError("Недопустимое значение настройки")
        await self.execute(
            f"UPDATE funpay_users SET {column}=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            value,
        )

    async def set_review_reply(self, telegram_id: int, stars: int, text: str) -> None:
        if stars not in range(1, 6):
            raise ValueError("Оценка должна быть от 1 до 5")
        await self.execute(
            f"UPDATE funpay_users SET review_reply_{stars}=$2, updated_at=NOW() WHERE telegram_id=$1",
            telegram_id,
            text,
        )

    async def claim_autoreply(
        self,
        telegram_id: int,
        chat_id: str,
        cooldown_minutes: int = 30,
        first_only: bool = False,
    ) -> bool:
        if first_only:
            row = await self.fetchrow(
                """
                INSERT INTO funpay_autoreply_log (telegram_id, chat_id, last_sent)
                VALUES ($1, $2, NOW())
                ON CONFLICT DO NOTHING
                RETURNING telegram_id
                """,
                telegram_id,
                chat_id,
            )
            return row is not None
        row = await self.fetchrow(
            """
            INSERT INTO funpay_autoreply_log (telegram_id, chat_id, last_sent)
            VALUES ($1, $2, NOW())
            ON CONFLICT (telegram_id, chat_id) DO UPDATE
               SET last_sent=NOW()
             WHERE funpay_autoreply_log.last_sent < NOW() - make_interval(mins => $3)
            RETURNING telegram_id
            """,
            telegram_id,
            chat_id,
            cooldown_minutes,
        )
        return row is not None

    async def claim_playerok_autoreply(
        self, telegram_id: int, chat_id: str, cooldown_minutes: int = 30
    ) -> bool:
        row = await self.fetchrow(
            """
            INSERT INTO playerok_autoreply_log (telegram_id, chat_id, last_sent)
            VALUES ($1, $2, NOW())
            ON CONFLICT (telegram_id, chat_id) DO UPDATE
               SET last_sent=NOW()
             WHERE playerok_autoreply_log.last_sent < NOW() - make_interval(mins => $3)
            RETURNING telegram_id
            """,
            telegram_id,
            chat_id,
            cooldown_minutes,
        )
        return row is not None

    async def list_plugins(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM funpay_plugins WHERE telegram_id=$1 ORDER BY uploaded_at, name",
            telegram_id,
        )

    async def get_plugin(self, telegram_id: int, uuid: str) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM funpay_plugins WHERE telegram_id=$1 AND uuid=$2",
            telegram_id,
            uuid,
        )

    async def seed_official_plugins(self) -> None:
        for plugin in READY_PLUGINS:
            await self.execute(
                """
                INSERT INTO funpay_plugin_catalog
                    (uuid, owner_telegram_id, publisher_name, filename, name, version,
                     short_description, description, credits, source, is_official)
                VALUES ($1, NULL, 'Команда проекта', $2, $3, $4, $5, $6,
                        'FunPay aiogram bot', $7, TRUE)
                ON CONFLICT (uuid) DO UPDATE
                    SET publisher_name=EXCLUDED.publisher_name,
                        filename=EXCLUDED.filename, name=EXCLUDED.name,
                        version=EXCLUDED.version,
                        short_description=EXCLUDED.short_description,
                        description=EXCLUDED.description, credits=EXCLUDED.credits,
                        source=EXCLUDED.source, is_official=TRUE, updated_at=NOW()
                  WHERE funpay_plugin_catalog.is_official=TRUE
                """,
                plugin.uuid,
                plugin.filename,
                plugin.name,
                plugin.version,
                plugin.description,
                plugin.details,
                ready_plugin_source(plugin),
            )

    async def list_catalog_plugins(
        self, limit: int, offset: int
    ) -> tuple[list[asyncpg.Record], int]:
        rows = await self.fetch(
            """
            SELECT * FROM funpay_plugin_catalog
             ORDER BY is_official DESC, install_count DESC, updated_at DESC, name
             LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        count_row = await self.fetchrow("SELECT COUNT(*) AS count FROM funpay_plugin_catalog")
        return rows, int(count_row["count"]) if count_row else 0

    async def get_catalog_plugin(self, uuid: str) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM funpay_plugin_catalog WHERE uuid=$1",
            uuid,
        )

    async def publish_catalog_plugin(
        self,
        telegram_id: int,
        uuid: str,
        publisher_name: str,
        description: str,
    ) -> bool:
        row = await self.fetchrow(
            """
            INSERT INTO funpay_plugin_catalog
                (uuid, owner_telegram_id, publisher_name, filename, name, version,
                 short_description, description, credits, source, is_official,
                 published_at, updated_at)
            SELECT uuid, telegram_id, $3, filename, name, version, description, $4,
                   credits, source, FALSE, NOW(), NOW()
              FROM funpay_plugins
             WHERE telegram_id=$1 AND uuid=$2
            ON CONFLICT (uuid) DO UPDATE
                SET publisher_name=EXCLUDED.publisher_name,
                    filename=EXCLUDED.filename, name=EXCLUDED.name,
                    version=EXCLUDED.version,
                    short_description=EXCLUDED.short_description,
                    description=EXCLUDED.description, credits=EXCLUDED.credits,
                    source=EXCLUDED.source, updated_at=NOW()
              WHERE funpay_plugin_catalog.owner_telegram_id=$1
                AND funpay_plugin_catalog.is_official=FALSE
            RETURNING uuid
            """,
            telegram_id,
            uuid,
            publisher_name,
            description,
        )
        return row is not None

    async def unpublish_catalog_plugin(self, telegram_id: int, uuid: str) -> bool:
        result = await self.execute(
            """
            DELETE FROM funpay_plugin_catalog
             WHERE uuid=$1 AND owner_telegram_id=$2 AND is_official=FALSE
            """,
            uuid,
            telegram_id,
        )
        return result.endswith(" 1")

    async def increment_catalog_install(self, uuid: str) -> None:
        await self.execute(
            "UPDATE funpay_plugin_catalog SET install_count=install_count+1 WHERE uuid=$1",
            uuid,
        )

    async def upsert_plugin(self, telegram_id: int, plugin: PluginData, source: str) -> None:
        await self.execute(
            """
            INSERT INTO funpay_plugins
                (telegram_id, uuid, filename, name, version, description, credits,
                 settings_page, source, enabled, uploaded_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, NOW())
            ON CONFLICT (telegram_id, uuid) DO UPDATE
                SET filename=EXCLUDED.filename, name=EXCLUDED.name, version=EXCLUDED.version,
                    description=EXCLUDED.description, credits=EXCLUDED.credits,
                    settings_page=EXCLUDED.settings_page, source=EXCLUDED.source,
                    enabled=TRUE, uploaded_at=NOW()
            """,
            telegram_id,
            plugin.uuid,
            plugin.filename,
            plugin.name,
            plugin.version,
            plugin.description,
            plugin.credits,
            plugin.settings_page,
            source,
        )

    async def set_plugin_enabled(self, telegram_id: int, uuid: str, enabled: bool) -> None:
        await self.execute(
            "UPDATE funpay_plugins SET enabled=$3 WHERE telegram_id=$1 AND uuid=$2",
            telegram_id,
            uuid,
            enabled,
        )

    async def delete_plugin(self, telegram_id: int, uuid: str) -> None:
        await self.execute(
            "DELETE FROM funpay_plugins WHERE telegram_id=$1 AND uuid=$2",
            telegram_id,
            uuid,
        )

    async def get_plugin_setting(
        self, telegram_id: int, uuid: str, key: str, default: str = ""
    ) -> str:
        row = await self.fetchrow(
            """
            SELECT setting_value FROM funpay_plugin_settings
             WHERE telegram_id=$1 AND plugin_uuid=$2 AND setting_key=$3
            """,
            telegram_id,
            uuid,
            key,
        )
        return str(row["setting_value"]) if row else default

    async def set_plugin_setting(
        self, telegram_id: int, uuid: str, key: str, value: str
    ) -> None:
        await self.execute(
            """
            INSERT INTO funpay_plugin_settings
                (telegram_id, plugin_uuid, setting_key, setting_value, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (telegram_id, plugin_uuid, setting_key) DO UPDATE
                SET setting_value=EXCLUDED.setting_value, updated_at=NOW()
            """,
            telegram_id,
            uuid,
            key,
            value,
        )

    async def seed_official_playerok_plugins(self) -> None:
        for plugin in PLAYEROK_READY_PLUGINS:
            await self.execute(
                """
                INSERT INTO playerok_plugin_catalog
                    (uuid, owner_telegram_id, publisher_name, filename, name, version,
                     short_description, description, credits, source, is_official)
                VALUES ($1, NULL, 'Команда проекта', $2, $3, $4, $5, $6,
                        'FunPay aiogram bot', $7, TRUE)
                ON CONFLICT (uuid) DO UPDATE
                    SET publisher_name=EXCLUDED.publisher_name,
                        filename=EXCLUDED.filename, name=EXCLUDED.name,
                        version=EXCLUDED.version,
                        short_description=EXCLUDED.short_description,
                        description=EXCLUDED.description, credits=EXCLUDED.credits,
                        source=EXCLUDED.source, is_official=TRUE, updated_at=NOW()
                  WHERE playerok_plugin_catalog.is_official=TRUE
                """,
                plugin.uuid,
                plugin.filename,
                plugin.name,
                plugin.version,
                plugin.description,
                plugin.details,
                playerok_ready_plugin_source(plugin),
            )

    async def list_playerok_plugins(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM playerok_plugins WHERE telegram_id=$1 ORDER BY uploaded_at, name",
            telegram_id,
        )

    async def get_playerok_plugin(
        self, telegram_id: int, uuid: str
    ) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM playerok_plugins WHERE telegram_id=$1 AND uuid=$2",
            telegram_id,
            uuid,
        )

    async def list_playerok_catalog_plugins(
        self, limit: int, offset: int
    ) -> tuple[list[asyncpg.Record], int]:
        rows = await self.fetch(
            """
            SELECT * FROM playerok_plugin_catalog
             ORDER BY is_official DESC, install_count DESC, updated_at DESC, name
             LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        count_row = await self.fetchrow(
            "SELECT COUNT(*) AS count FROM playerok_plugin_catalog"
        )
        return rows, int(count_row["count"]) if count_row else 0

    async def get_playerok_catalog_plugin(self, uuid: str) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM playerok_plugin_catalog WHERE uuid=$1", uuid
        )

    async def publish_playerok_catalog_plugin(
        self,
        telegram_id: int,
        uuid: str,
        publisher_name: str,
        description: str,
    ) -> bool:
        row = await self.fetchrow(
            """
            INSERT INTO playerok_plugin_catalog
                (uuid, owner_telegram_id, publisher_name, filename, name, version,
                 short_description, description, credits, source, is_official,
                 published_at, updated_at)
            SELECT uuid, telegram_id, $3, filename, name, version, description, $4,
                   credits, source, FALSE, NOW(), NOW()
              FROM playerok_plugins
             WHERE telegram_id=$1 AND uuid=$2
            ON CONFLICT (uuid) DO UPDATE
                SET publisher_name=EXCLUDED.publisher_name,
                    filename=EXCLUDED.filename, name=EXCLUDED.name,
                    version=EXCLUDED.version,
                    short_description=EXCLUDED.short_description,
                    description=EXCLUDED.description, credits=EXCLUDED.credits,
                    source=EXCLUDED.source, updated_at=NOW()
              WHERE playerok_plugin_catalog.owner_telegram_id=$1
                AND playerok_plugin_catalog.is_official=FALSE
            RETURNING uuid
            """,
            telegram_id,
            uuid,
            publisher_name,
            description,
        )
        return row is not None

    async def unpublish_playerok_catalog_plugin(
        self, telegram_id: int, uuid: str
    ) -> bool:
        result = await self.execute(
            """
            DELETE FROM playerok_plugin_catalog
             WHERE uuid=$1 AND owner_telegram_id=$2 AND is_official=FALSE
            """,
            uuid,
            telegram_id,
        )
        return result.endswith(" 1")

    async def increment_playerok_catalog_install(self, uuid: str) -> None:
        await self.execute(
            """
            UPDATE playerok_plugin_catalog
               SET install_count=install_count+1
             WHERE uuid=$1
            """,
            uuid,
        )

    async def upsert_playerok_plugin(
        self, telegram_id: int, plugin: PlayerokPluginData, source: str
    ) -> None:
        await self.execute(
            """
            INSERT INTO playerok_plugins
                (telegram_id, uuid, filename, name, version, description, credits,
                 settings_page, source, enabled, uploaded_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, NOW())
            ON CONFLICT (telegram_id, uuid) DO UPDATE
                SET filename=EXCLUDED.filename, name=EXCLUDED.name,
                    version=EXCLUDED.version, description=EXCLUDED.description,
                    credits=EXCLUDED.credits, settings_page=EXCLUDED.settings_page,
                    source=EXCLUDED.source, enabled=TRUE, uploaded_at=NOW()
            """,
            telegram_id,
            plugin.uuid,
            plugin.filename,
            plugin.name,
            plugin.version,
            plugin.description,
            plugin.credits,
            plugin.settings_page,
            source,
        )

    async def set_playerok_plugin_enabled(
        self, telegram_id: int, uuid: str, enabled: bool
    ) -> None:
        await self.execute(
            "UPDATE playerok_plugins SET enabled=$3 WHERE telegram_id=$1 AND uuid=$2",
            telegram_id,
            uuid,
            enabled,
        )

    async def delete_playerok_plugin(self, telegram_id: int, uuid: str) -> None:
        await self.execute(
            "DELETE FROM playerok_plugins WHERE telegram_id=$1 AND uuid=$2",
            telegram_id,
            uuid,
        )

    async def list_playerok_plugin_settings(
        self, telegram_id: int, uuid: str
    ) -> dict[str, str]:
        rows = await self.fetch(
            """
            SELECT setting_key, setting_value FROM playerok_plugin_settings
             WHERE telegram_id=$1 AND plugin_uuid=$2
            """,
            telegram_id,
            uuid,
        )
        return {str(row["setting_key"]): str(row["setting_value"]) for row in rows}

    async def set_playerok_plugin_setting(
        self, telegram_id: int, uuid: str, key: str, value: str
    ) -> None:
        await self.execute(
            """
            INSERT INTO playerok_plugin_settings
                (telegram_id, plugin_uuid, setting_key, setting_value, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (telegram_id, plugin_uuid, setting_key) DO UPDATE
                SET setting_value=EXCLUDED.setting_value, updated_at=NOW()
            """,
            telegram_id,
            uuid,
            key,
            value,
        )

    async def active_users(self) -> list[asyncpg.Record]:
        return await self.fetch(
            """
            SELECT * FROM funpay_users
             WHERE account_active=TRUE AND proxy_enc IS NOT NULL AND golden_key_enc IS NOT NULL
            """
        )

    async def active_playerok_users(self) -> list[asyncpg.Record]:
        return await self.fetch(
            """
            SELECT * FROM funpay_users
             WHERE playerok_active=TRUE
               AND playerok_proxy_enc IS NOT NULL
               AND playerok_cookie_enc IS NOT NULL
            """
        )

    async def list_delivery_rules(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM funpay_delivery_rules WHERE telegram_id=$1 ORDER BY lot_title",
            telegram_id,
        )

    async def get_delivery_rule(self, telegram_id: int, rule_id: int) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM funpay_delivery_rules WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            rule_id,
        )

    async def find_delivery_rule(
        self, telegram_id: int, lot_title: str
    ) -> asyncpg.Record | None:
        return await self.fetchrow(
            """
            SELECT * FROM funpay_delivery_rules
             WHERE telegram_id=$1 AND position(lot_title in $2)>0
             ORDER BY length(lot_title) DESC LIMIT 1
            """,
            telegram_id,
            lot_title,
        )

    async def save_delivery_rule(
        self, telegram_id: int, lot_id: int, lot_title: str, response: str
    ) -> asyncpg.Record:
        return await self.fetchrow(
            """
            INSERT INTO funpay_delivery_rules (telegram_id, lot_id, lot_title, response)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id, lot_id) DO UPDATE
                SET lot_title=EXCLUDED.lot_title, response=EXCLUDED.response,
                    enabled=TRUE, updated_at=NOW()
            RETURNING *
            """,
            telegram_id,
            lot_id,
            lot_title,
            response,
        )

    async def add_delivery_products(
        self, telegram_id: int, rule_id: int, products: list[str]
    ) -> None:
        await self.execute(
            """
            UPDATE funpay_delivery_rules
               SET products=products || $3::TEXT[], updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            rule_id,
            products,
        )

    async def clear_delivery_products(self, telegram_id: int, rule_id: int) -> None:
        await self.execute(
            """
            UPDATE funpay_delivery_rules
               SET products=ARRAY[]::TEXT[], updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            rule_id,
        )

    async def toggle_delivery_rule(self, telegram_id: int, rule_id: int) -> None:
        await self.execute(
            """
            UPDATE funpay_delivery_rules SET enabled=NOT enabled, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            rule_id,
        )

    async def toggle_delivery_rule_option(
        self, telegram_id: int, rule_id: int, column: str
    ) -> None:
        if column not in {"disable_auto_restore", "disable_auto_disable"}:
            raise ValueError("Неизвестная настройка правила автовыдачи")
        await self.execute(
            f"""
            UPDATE funpay_delivery_rules
               SET {column}=NOT {column}, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            rule_id,
        )

    async def delete_delivery_rule(self, telegram_id: int, rule_id: int) -> None:
        await self.execute(
            "DELETE FROM funpay_delivery_rules WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            rule_id,
        )

    async def claim_delivery(
        self, telegram_id: int, order_id: str, lot_title: str, amount: int
    ) -> tuple[asyncpg.Record, list[str], int, str | None] | None:
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        async with self.pool.acquire() as connection:  # noqa: SIM117 - transaction needs connection.
            async with connection.transaction():
                duplicate = await connection.fetchval(
                    "SELECT 1 FROM funpay_delivery_log WHERE telegram_id=$1 AND order_id=$2",
                    telegram_id,
                    order_id,
                )
                if duplicate:
                    return None
                rule = await connection.fetchrow(
                    """
                    SELECT * FROM funpay_delivery_rules
                     WHERE telegram_id=$1 AND enabled=TRUE
                       AND position(lot_title in $2)>0
                     ORDER BY length(lot_title) DESC
                     LIMIT 1 FOR UPDATE
                    """,
                    telegram_id,
                    lot_title,
                )
                if not rule:
                    return None
                stock = list(rule["products"] or [])
                needs_product = "$product" in rule["response"]
                if needs_product and len(stock) < amount:
                    error = f"Недостаточно товаров: нужно {amount}, доступно {len(stock)}"
                    await connection.execute(
                        """
                        INSERT INTO funpay_delivery_log
                            (telegram_id, order_id, rule_id, status, details)
                        VALUES ($1, $2, $3, 'failed', $4)
                        """,
                        telegram_id,
                        order_id,
                        rule["id"],
                        error,
                    )
                    return rule, [], len(stock), error
                products = stock[:amount] if needs_product else []
                remaining = stock[amount:] if needs_product else stock
                await connection.execute(
                    "UPDATE funpay_delivery_rules SET products=$3, updated_at=NOW() WHERE telegram_id=$1 AND id=$2",
                    telegram_id,
                    rule["id"],
                    remaining,
                )
                await connection.execute(
                    """
                    INSERT INTO funpay_delivery_log
                        (telegram_id, order_id, rule_id, status)
                    VALUES ($1, $2, $3, 'processing')
                    """,
                    telegram_id,
                    order_id,
                    rule["id"],
                )
                return rule, products, len(remaining), None

    async def finish_delivery(
        self, telegram_id: int, order_id: str, status: str, details: str = ""
    ) -> None:
        await self.execute(
            """
            UPDATE funpay_delivery_log SET status=$3, details=$4
             WHERE telegram_id=$1 AND order_id=$2
            """,
            telegram_id,
            order_id,
            status,
            details,
        )

    async def restore_delivery_products(
        self, telegram_id: int, rule_id: int, products: list[str]
    ) -> None:
        if products:
            await self.execute(
                """
                UPDATE funpay_delivery_rules
                   SET products=$3::TEXT[] || products, updated_at=NOW()
                 WHERE telegram_id=$1 AND id=$2
                """,
                telegram_id,
                rule_id,
                products,
            )

    async def list_command_replies(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM funpay_command_replies WHERE telegram_id=$1 ORDER BY trigger",
            telegram_id,
        )

    async def get_command_reply(self, telegram_id: int, reply_id: int) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM funpay_command_replies WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            reply_id,
        )

    async def find_command_reply(self, telegram_id: int, trigger: str) -> asyncpg.Record | None:
        return await self.fetchrow(
            """
            SELECT * FROM funpay_command_replies
             WHERE telegram_id=$1 AND trigger=$2 AND enabled=TRUE
            """,
            telegram_id,
            trigger.casefold().strip(),
        )

    async def save_command_reply(
        self, telegram_id: int, trigger: str, response: str
    ) -> asyncpg.Record:
        return await self.fetchrow(
            """
            INSERT INTO funpay_command_replies (telegram_id, trigger, response)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id, trigger) DO UPDATE
                SET response=EXCLUDED.response, enabled=TRUE, updated_at=NOW()
            RETURNING *
            """,
            telegram_id,
            trigger.casefold().strip(),
            response,
        )

    async def toggle_command_reply(self, telegram_id: int, reply_id: int) -> None:
        await self.execute(
            """
            UPDATE funpay_command_replies SET enabled=NOT enabled, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            reply_id,
        )

    async def toggle_command_notification(self, telegram_id: int, reply_id: int) -> None:
        await self.execute(
            """
            UPDATE funpay_command_replies SET notify=NOT notify, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            reply_id,
        )

    async def delete_command_reply(self, telegram_id: int, reply_id: int) -> None:
        await self.execute(
            "DELETE FROM funpay_command_replies WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            reply_id,
        )

    async def list_notification_targets(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM funpay_notification_targets WHERE telegram_id=$1 ORDER BY created_at",
            telegram_id,
        )

    async def save_notification_target(
        self, telegram_id: int, chat_id: int, title: str
    ) -> None:
        await self.execute(
            """
            INSERT INTO funpay_notification_targets (telegram_id, chat_id, title)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id, chat_id) DO UPDATE
                SET title=EXCLUDED.title, enabled=TRUE
            """,
            telegram_id,
            chat_id,
            title,
        )

    async def delete_notification_target(self, telegram_id: int, chat_id: int) -> None:
        await self.execute(
            "DELETE FROM funpay_notification_targets WHERE telegram_id=$1 AND chat_id=$2",
            telegram_id,
            chat_id,
        )

    async def list_playerok_command_replies(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM playerok_command_replies WHERE telegram_id=$1 ORDER BY trigger",
            telegram_id,
        )

    async def get_playerok_command_reply(
        self, telegram_id: int, reply_id: int
    ) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM playerok_command_replies WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            reply_id,
        )

    async def find_playerok_command_reply(
        self, telegram_id: int, trigger: str
    ) -> asyncpg.Record | None:
        return await self.fetchrow(
            """
            SELECT * FROM playerok_command_replies
             WHERE telegram_id=$1 AND trigger=$2 AND enabled=TRUE
            """,
            telegram_id,
            trigger.casefold().strip(),
        )

    async def save_playerok_command_reply(
        self, telegram_id: int, trigger: str, response: str
    ) -> asyncpg.Record:
        return await self.fetchrow(
            """
            INSERT INTO playerok_command_replies (telegram_id, trigger, response)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id, trigger) DO UPDATE
                SET response=EXCLUDED.response, enabled=TRUE, updated_at=NOW()
            RETURNING *
            """,
            telegram_id,
            trigger.casefold().strip(),
            response,
        )

    async def toggle_playerok_command_reply(self, telegram_id: int, reply_id: int) -> None:
        await self.execute(
            """
            UPDATE playerok_command_replies SET enabled=NOT enabled, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            reply_id,
        )

    async def delete_playerok_command_reply(self, telegram_id: int, reply_id: int) -> None:
        await self.execute(
            "DELETE FROM playerok_command_replies WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            reply_id,
        )

    async def list_playerok_delivery_rules(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM playerok_delivery_rules WHERE telegram_id=$1 ORDER BY item_title",
            telegram_id,
        )

    async def get_playerok_delivery_rule(
        self, telegram_id: int, rule_id: int
    ) -> asyncpg.Record | None:
        return await self.fetchrow(
            "SELECT * FROM playerok_delivery_rules WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            rule_id,
        )

    async def save_playerok_delivery_rule(
        self, telegram_id: int, item_id: str, item_title: str, response: str
    ) -> asyncpg.Record:
        return await self.fetchrow(
            """
            INSERT INTO playerok_delivery_rules (telegram_id, item_id, item_title, response)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id, item_id) DO UPDATE
                SET item_title=EXCLUDED.item_title, response=EXCLUDED.response,
                    enabled=TRUE, updated_at=NOW()
            RETURNING *
            """,
            telegram_id,
            item_id,
            item_title,
            response,
        )

    async def add_playerok_delivery_products(
        self, telegram_id: int, rule_id: int, products: list[str]
    ) -> None:
        await self.execute(
            """
            UPDATE playerok_delivery_rules
               SET products=products || $3::TEXT[], updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            rule_id,
            products,
        )

    async def clear_playerok_delivery_products(self, telegram_id: int, rule_id: int) -> None:
        await self.execute(
            """
            UPDATE playerok_delivery_rules
               SET products=ARRAY[]::TEXT[], updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            rule_id,
        )

    async def toggle_playerok_delivery_rule(self, telegram_id: int, rule_id: int) -> None:
        await self.execute(
            """
            UPDATE playerok_delivery_rules SET enabled=NOT enabled, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2
            """,
            telegram_id,
            rule_id,
        )

    async def delete_playerok_delivery_rule(self, telegram_id: int, rule_id: int) -> None:
        await self.execute(
            "DELETE FROM playerok_delivery_rules WHERE telegram_id=$1 AND id=$2",
            telegram_id,
            rule_id,
        )

    async def claim_playerok_delivery(
        self, telegram_id: int, deal_id: str, item_id: str
    ) -> tuple[asyncpg.Record, list[str], int, str | None] | None:
        if not self.pool:
            raise RuntimeError("База данных не подключена")
        async with self.pool.acquire() as connection:  # noqa: SIM117 - transaction needs connection.
            async with connection.transaction():
                if await connection.fetchval(
                    "SELECT 1 FROM playerok_delivery_log WHERE telegram_id=$1 AND deal_id=$2",
                    telegram_id,
                    deal_id,
                ):
                    return None
                rule = await connection.fetchrow(
                    """
                    SELECT * FROM playerok_delivery_rules
                     WHERE telegram_id=$1 AND item_id=$2 AND enabled=TRUE
                     LIMIT 1 FOR UPDATE
                    """,
                    telegram_id,
                    item_id,
                )
                if not rule:
                    return None
                stock = list(rule["products"] or [])
                needs_product = "$product" in rule["response"]
                if needs_product and not stock:
                    error = "Закончились товары для автовыдачи"
                    await connection.execute(
                        """
                        INSERT INTO playerok_delivery_log
                            (telegram_id, deal_id, rule_id, status, details)
                        VALUES ($1, $2, $3, 'failed', $4)
                        """,
                        telegram_id,
                        deal_id,
                        rule["id"],
                        error,
                    )
                    return rule, [], 0, error
                products = stock[:1] if needs_product else []
                remaining = stock[1:] if needs_product else stock
                await connection.execute(
                    "UPDATE playerok_delivery_rules SET products=$3, updated_at=NOW() WHERE telegram_id=$1 AND id=$2",
                    telegram_id,
                    rule["id"],
                    remaining,
                )
                await connection.execute(
                    """
                    INSERT INTO playerok_delivery_log (telegram_id, deal_id, rule_id, status)
                    VALUES ($1, $2, $3, 'processing')
                    """,
                    telegram_id,
                    deal_id,
                    rule["id"],
                )
                return rule, products, len(remaining), None

    async def finish_playerok_delivery(
        self, telegram_id: int, deal_id: str, status: str, details: str = ""
    ) -> None:
        await self.execute(
            """
            UPDATE playerok_delivery_log SET status=$3, details=$4
             WHERE telegram_id=$1 AND deal_id=$2
            """,
            telegram_id,
            deal_id,
            status,
            details,
        )

    async def restore_playerok_delivery_products(
        self, telegram_id: int, rule_id: int, products: list[str]
    ) -> None:
        if products:
            await self.execute(
                """
                UPDATE playerok_delivery_rules
                   SET products=$3::TEXT[] || products, updated_at=NOW()
                 WHERE telegram_id=$1 AND id=$2
                """,
                telegram_id,
                rule_id,
                products,
            )


def normalize_proxy(raw: str) -> str:
    value = raw.strip()
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5", "socks5h"}:
        raise ValueError("Поддерживаются http, https, socks4, socks5 и socks5h")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректный порт прокси") from exc
    if not parsed.hostname or not port:
        raise ValueError("Нужен адрес вида user:password@host:port")
    return value


def proxy_dict(proxy: str) -> dict[str, str]:
    return {"http": proxy, "https": proxy}


def proxy_label(proxy: str) -> str:
    parsed = urlsplit(proxy)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


def playerok_proxy_value(proxy: str) -> str:
    parsed = urlsplit(proxy)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("PlayerokAPI поддерживает IPv4 HTTP/HTTPS-прокси")
    credentials = ""
    if parsed.username:
        credentials = parsed.username
        if parsed.password:
            credentials += f":{parsed.password}"
        credentials += "@"
    return f"{credentials}{parsed.hostname}:{parsed.port}"


def create_playerok_account(cookie: str, proxy: str) -> Any:
    """Создаёт независимый аккаунт, обходя singleton в PlayerokAPI."""
    if PlayerokAccount is None:
        raise RuntimeError("PlayerokAPI не установлен в текущей сборке")
    account = object.__new__(PlayerokAccount)
    kwargs: dict[str, Any] = {
        "user_agent": USER_AGENT,
        "proxy": playerok_proxy_value(proxy),
        "requests_timeout": 20,
    }
    if "=" in cookie:
        kwargs["cookies"] = cookie
    else:
        kwargs["token"] = cookie
    try:
        PlayerokAccount.__init__(account, **kwargs)
    except FileNotFoundError:
        # PlayerokAPI 1.1 не объявляет cacert.pem в package_data. К этому моменту
        # __init__ уже заполнил данные аккаунта, поэтому безопасно завершаем настройку клиентов.
        account._cert_path = certifi.where()
        account._tmp_cert_path = certifi.where()
        account._Account__tls_requests = None
        account._Account__curl_session = None
        account._Account__request_lock = threading.RLock()
        account._refresh_clients()
    return account


def clipped(value: Any, size: int = 700) -> str:
    text = str(value or "")
    return text if len(text) <= size else text[: size - 1] + "…"


def render_template(
    text: str,
    *,
    message: Any | None = None,
    order: Any | None = None,
    review: Any | None = None,
    account: Account | None = None,
    chat_id: int | str | None = None,
    chat_name: str | None = None,
) -> str:
    """Подставляет безопасные текстовые переменные в исходящие сообщения FunPay."""
    now = datetime.now(timezone.utc).astimezone()
    username = chat_name or ""
    message_text = ""
    if message is not None:
        username = message.author or message.chat_name or username
        chat_name = message.chat_name or chat_name
        chat_id = message.chat_id if chat_id is None else chat_id
        message_text = str(message)
    if order is not None:
        username = order.buyer_username or username
        chat_id = order.chat_id if chat_id is None else chat_id
    if review is None and order is not None:
        review = getattr(order, "review", None)
    order_title = ""
    if order is not None:
        order_title = getattr(order, "title", None) or getattr(order, "description", None) or ""
    variables = {
        "$full_date": now.strftime("%d.%m.%Y"),
        "$date": now.strftime("%d.%m.%Y"),
        "$full_time": now.strftime("%H:%M:%S"),
        "$time": now.strftime("%H:%M"),
        "$username": str(username or ""),
        "$chat_name": str(chat_name or username or ""),
        "$chat_id": str(chat_id or ""),
        "$message_text": message_text,
        "$account_name": str(account.username if account else ""),
        "$account_id": str(account.id if account else ""),
        "$order_id": str(order.id if order else ""),
        "$order_link": f"https://funpay.com/orders/{order.id}/" if order else "",
        "$order_title": str(order_title),
        "$stars": str(getattr(review, "stars", "") or ""),
        "$rating": str(getattr(review, "stars", "") or ""),
        "$review_text": str(getattr(review, "text", "") or ""),
        "$review_reply": str(getattr(review, "reply", "") or ""),
    }
    for variable in sorted(variables, key=len, reverse=True):
        text = text.replace(variable, variables[variable])
    return text


def render_playerok_template(
    text: str, account: Any, *, chat: Any | None = None, message: Any | None = None,
    deal: Any | None = None,
) -> str:
    """Подставляет общие переменные без привязки к объектам FunPay."""
    buyer = getattr(message, "user", None) or getattr(deal, "user", None)
    item = getattr(deal, "item", None)
    text = text.replace("$message_text", str(getattr(message, "text", "") or ""))
    text = text.replace("$order_link", "")
    return render_template(
        text,
        account=account,
        chat_id=getattr(chat, "id", None),
        chat_name=getattr(buyer, "username", None) or getattr(chat, "id", None),
        order=SimpleNamespace(
            id=str(getattr(deal, "id", "")),
            buyer_username=getattr(buyer, "username", None),
            chat_id=getattr(chat, "id", None),
            title=getattr(item, "name", None),
        ) if deal else None,
    )


def format_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".")


def within_work_hours(start: int, end: int, hour: int) -> bool:
    if start == 0 and end == 24:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def extract_order_id(value: Any) -> str | None:
    match = re.search(r"#([A-Za-z0-9_-]{4,40})", str(value or ""))
    return match.group(1) if match else None


def normalize_review_reply(value: str) -> str:
    lines = value.strip().splitlines()[:10]
    return "\n".join(lines)[:999].strip()


@dataclass
class SalesStats:
    days: int | None
    total: int = 0
    closed: int = 0
    paid: int = 0
    refunded: int = 0
    buyers: set[int] = field(default_factory=set)
    revenue: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    refunded_sum: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    lot_counts: Counter[str] = field(default_factory=Counter)
    truncated: bool = False


def load_sales_stats(account: Account, days: int | None, max_pages: int = 200) -> SalesStats:
    """Собирает статистику продаж, проходя страницы заказов до начала периода."""
    moscow_now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    since = moscow_now - timedelta(days=days) if days else None
    stats = SalesStats(days)
    cursor = None
    locale = None
    subcategories = None
    for _ in range(max_pages):
        cursor, orders, locale, subcategories = account.get_sales(
            start_from=cursor,
            locale=locale,
            subcategories=subcategories,
        )
        reached_period_start = False
        for order in orders:
            if since and order.date < since:
                reached_period_start = True
                continue
            stats.total += 1
            stats.buyers.add(order.buyer_id)
            currency = str(order.currency)
            if order.status is types.OrderStatuses.CLOSED:
                stats.closed += 1
                stats.revenue[currency] += order.price
                stats.lot_counts[clipped(order.description, 120)] += 1
            elif order.status is types.OrderStatuses.PAID:
                stats.paid += 1
            elif order.status in {
                types.OrderStatuses.REFUNDED,
                types.OrderStatuses.PARTIALLY_REFUNDED,
            }:
                stats.refunded += 1
                stats.refunded_sum[currency] += order.price
        if reached_period_start or not cursor:
            break
    else:
        stats.truncated = bool(cursor)
    return stats


def format_sales_stats(stats: SalesStats) -> str:
    period = "за всё время" if stats.days is None else f"за {stats.days} дн."
    revenue = ", ".join(
        f"{format_money(value)} {html.escape(currency)}"
        for currency, value in sorted(stats.revenue.items())
    ) or "0"
    refunds = ", ".join(
        f"{format_money(value)} {html.escape(currency)}"
        for currency, value in sorted(stats.refunded_sum.items())
    ) or "0"
    top = "\n".join(
        f"{index}. {html.escape(title)} — <b>{count}</b>"
        for index, (title, count) in enumerate(stats.lot_counts.most_common(3), 1)
    ) or "нет закрытых заказов"
    note = "\n\n⚠️ Достигнут лимит 200 страниц заказов." if stats.truncated else ""
    return (
        f"📊 <b>Статистика {period}</b>\n\n"
        f"Всего заказов: <b>{stats.total}</b>\n"
        f"Закрыто: <b>{stats.closed}</b>\n"
        f"Ожидают выполнения: <b>{stats.paid}</b>\n"
        f"Возвратов: <b>{stats.refunded}</b> на {refunds}\n"
        f"Уникальных покупателей: <b>{len(stats.buyers)}</b>\n"
        f"Выручка по закрытым: <b>{revenue}</b>\n\n"
        f"🏆 <b>Популярные лоты</b>\n{top}{note}"
    )


@dataclass
class LotBulkResult:
    common_total: int = 0
    currency_total: int = 0
    changed: int = 0
    errors: list[str] = field(default_factory=list)


def load_lot_inventory(account: Account) -> tuple[Any, list[Any], list[Any]]:
    profile = account.get_user(account.id)
    lots = profile.get_lots()
    common = [
        lot
        for lot in lots
        if lot.subcategory.type is types.SubCategoryTypes.COMMON
    ]
    currency = [
        lot
        for lot in lots
        if lot.subcategory.type is types.SubCategoryTypes.CURRENCY
    ]
    return profile, common, currency


def apply_bulk_lot_action(account: Account, action: str) -> LotBulkResult:
    """Массово меняет обычные и валютные предложения одного аккаунта."""
    if action not in {"activate", "deactivate", "delete"}:
        raise ValueError("Неизвестное действие с лотами")
    _, common, currency = load_lot_inventory(account)
    result = LotBulkResult(len(common), len(currency))
    for lot in common:
        try:
            if action == "delete":
                account.delete_lot(int(lot.id))
            else:
                fields = account.get_lot_fields(int(lot.id))
                fields.active = action == "activate"
                account.save_lot(fields.renew_fields())
            result.changed += 1
        except Exception as exc:  # noqa: BLE001 - API операций с лотами выбрасывает разные исключения.
            result.errors.append(f"{lot.id}: {clipped(exc, 120)}")

    subcategories = {lot.subcategory.id: lot.subcategory for lot in currency}.values()
    for subcategory in subcategories:
        try:
            fields = account.get_chip_fields(subcategory.id)
            for offer in fields.chip_offers.values():
                offer.active = action == "activate" if action != "delete" else False
            account.save_chip(fields.renew_fields())
            result.changed += len(fields.chip_offers)
        except Exception as exc:  # noqa: BLE001 - API валютных лотов выбрасывает разные исключения.
            result.errors.append(f"валюта {subcategory.id}: {clipped(exc, 120)}")
    return result


def order_status_label(status: types.OrderStatuses) -> str:
    return {
        types.OrderStatuses.PAID: "оплачен, ожидает выполнения",
        types.OrderStatuses.CLOSED: "закрыт",
        types.OrderStatuses.REFUNDED: "возврат",
        types.OrderStatuses.PARTIALLY_REFUNDED: "частичный возврат",
        types.OrderStatuses.UNPAID: "не оплачен",
    }.get(status, status.name.lower())


def format_order(order: types.Order) -> str:
    """Формирует карточку заказа без выдачи сохранённых секретов товара."""
    title = order.title or (order.subcategory.name if order.subcategory else "—")
    details = [
        "📦 <b>Заказ FunPay</b>",
        f"ID: <code>{html.escape(str(order.id))}</code>",
        f"Статус: <b>{html.escape(order_status_label(order.status))}</b>",
        f"Товар: {html.escape(clipped(title, 1000))}",
        f"Количество: <b>{order.amount}</b>",
        f"Сумма: <b>{format_money(order.sum)} {html.escape(str(order.currency))}</b>",
        f"Покупатель: {html.escape(order.buyer_username or '—')} (<code>{order.buyer_id}</code>)",
        f"Продавец: {html.escape(order.seller_username or '—')} (<code>{order.seller_id}</code>)",
    ]
    if order.server:
        details.append(f"Сервер: {html.escape(order.server.name or '—')}")
    if order.side:
        details.append(f"Сторона: {html.escape(order.side.name or '—')}")
    if order.player:
        details.append(f"Персонаж: {html.escape(order.player)}")
    details.append(f"Чат: <code>{html.escape(str(order.chat_id))}</code>")
    return "\n".join(details)


def format_chat_history(chat: Any, account_id: int) -> list[str]:
    """Формирует всю доступную историю чата в валидные Telegram HTML-блоки."""
    title = f"💬 <b>Чат с {html.escape(chat.name or '—')}</b> · <code>{chat.id}</code>"
    blocks: list[str] = []
    if chat.looking_link:
        blocks.append(
            f"👀 <b>Покупатель смотрит:</b> "
            f"<a href=\"{html.escape(chat.looking_link, quote=True)}\">"
            f"{html.escape(chat.looking_text or 'лот')}</a>"
        )
    for item in chat.messages:
        incoming = False
        if item.author_id == 0:
            icon, author = "⚙️", "FunPay"
        elif item.author_id == account_id or item.by_bot or item.by_vertex:
            icon, author = "🟢", item.author or "Вы"
        else:
            icon, author = "🔵", item.author or chat.name or "Покупатель"
            incoming = True
        body = item.text or (f'<a href="{html.escape(item.image_link, quote=True)}">Изображение</a>' if item.image_link else "[без текста]")
        if item.text:
            body = html.escape(clipped(body, 2600))
        message_block = (
            f"<pre>{body}</pre>"
            if incoming and item.text
            else f"<blockquote>{body}</blockquote>"
        )
        blocks.append(f"{icon} <b>{html.escape(author)}</b>\n{message_block}")
    if not blocks:
        blocks.append("Сообщений пока нет.")

    chunks: list[str] = []
    current = title
    for block in blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > 3800 and current != title:
            chunks.append(current)
            current = block
        else:
            current = candidate
    chunks.append(current)
    return chunks


def load_full_chat(account: Account, chat_id: int, max_messages: int = 2000) -> tuple[types.Chat, bool]:
    """Загружает доступную историю назад по страницам FunPay."""
    chat = account.get_chat(chat_id, True)
    messages = list(chat.messages)
    seen_ids = {item.id for item in messages if item.id is not None}
    truncated = False
    while seen_ids and len(messages) < max_messages:
        cursor = min(seen_ids)
        older = account.get_chat_history(chat_id, cursor, chat.name)
        new_items = [item for item in older if item.id is not None and item.id not in seen_ids]
        if not new_items:
            break
        messages.extend(new_items)
        seen_ids.update(item.id for item in new_items)
        if min(item.id for item in new_items) >= cursor:
            break
    if len(messages) >= max_messages:
        messages = sorted(messages, key=lambda item: item.id or 0)[-max_messages:]
        truncated = True
    else:
        messages.sort(key=lambda item: item.id or 0)
    chat.messages = messages
    return chat, truncated


def load_detailed_balance(account: Account) -> types.Balance:
    """Получает общий и доступный к выводу баланс во всех валютах."""
    profile = account.get_user(account.id)
    lots = profile.get_common_lots()
    if lots:
        try:
            return account.get_balance(lots[0].id)
        except Exception:
            logger.debug("Не удалось получить баланс через собственный лот", exc_info=True)
    subcategories = account.get_sorted_subcategories()[types.SubCategoryTypes.COMMON]
    for subcategory_id in subcategories:
        try:
            public_lots = account.get_subcategory_public_lots(types.SubCategoryTypes.COMMON, subcategory_id)
            if public_lots:
                return account.get_balance(public_lots[0].id)
        except Exception:
            logger.debug(
                "Не удалось получить баланс через подкатегорию %s",
                subcategory_id,
                exc_info=True,
            )
            continue
    raise RuntimeError("Не найден лот, через который FunPay отдаёт подробный баланс")


@dataclass
class AccountRuntime:
    telegram_id: int
    account: Account
    runner: Runner
    keep_online_enabled: bool = True
    auto_raise_enabled: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    raise_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_raise_at: datetime | None = None
    next_raise_at: float = 0
    last_raise_summary: str = "ещё не запускалось"
    raise_schedule: dict[int, float] = field(default_factory=dict)


@dataclass
class PlayerokRuntime:
    telegram_id: int
    account: Any
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    publish_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    message_ids: dict[str, str] = field(default_factory=dict)
    deal_statuses: dict[str, str] = field(default_factory=dict)
    review_ids: set[str] = field(default_factory=set)
    initialized: bool = False
    next_auto_publish_at: float = 0
    poll_failures: int = 0


class RuntimeManager:
    def __init__(self, bot: Bot, db: Database, secrets: SecretBox):
        self.bot = bot
        self.db = db
        self.secrets = secrets
        self.runtimes: dict[int, AccountRuntime] = {}
        self.playerok_runtimes: dict[int, PlayerokRuntime] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.plugins = PluginManager(db, bot)
        self.playerok_plugins = PlayerokPluginManager(db, bot)

    async def start_saved(self) -> None:
        self.loop = asyncio.get_running_loop()
        for row in await self.db.active_users():
            user_id = int(row["telegram_id"])
            try:
                await self.start(user_id, row=row)
                if row["notify_system"]:
                    await self.safe_notify(user_id, "🟢 FunPay Runner восстановлен после запуска бота.")
            except Exception:
                logger.exception("Не удалось запустить FunPay-аккаунт пользователя %s", user_id)
                if row["notify_system"]:
                    await self.safe_notify(
                        user_id,
                        "⚠️ Не удалось восстановить подключение к FunPay. "
                        "Нажмите «Переподключить» или обновите данные.",
                    )
        for row in await self.db.active_playerok_users():
            user_id = int(row["telegram_id"])
            try:
                await self.start_playerok(user_id, row=row)
                if row["playerok_notify_system"]:
                    await self.safe_notify(
                        user_id,
                        "🟢 Слежение за аккаунтом восстановлено после запуска бота.",
                        marketplace="playerok",
                    )
            except Exception:
                logger.exception("Не удалось запустить Playerok-аккаунт пользователя %s", user_id)
                if row["playerok_notify_system"]:
                    await self.safe_notify(
                        user_id,
                        "⚠️ Не удалось восстановить Playerok. Обновите cookie или прокси.",
                        marketplace="playerok",
                    )

    async def start_playerok(
        self,
        telegram_id: int,
        row: asyncpg.Record | None = None,
        account: Any | None = None,
    ) -> PlayerokRuntime:
        await self.stop_playerok(telegram_id)
        row = row or await self.db.get_user(telegram_id)
        if not row or not row["playerok_proxy_enc"] or not row["playerok_cookie_enc"]:
            raise RuntimeError("Playerok не настроен")
        if account is None:
            proxy = self.secrets.decrypt(row["playerok_proxy_enc"])
            cookie = self.secrets.decrypt(row["playerok_cookie_enc"])
            account = create_playerok_account(cookie, proxy)
            await asyncio.wait_for(asyncio.to_thread(account.get), timeout=50)
        runtime = PlayerokRuntime(telegram_id=telegram_id, account=account)
        self.playerok_runtimes[telegram_id] = runtime
        await self.playerok_plugins.load_runtime(telegram_id, runtime)
        runtime.task = asyncio.create_task(self._playerok_poll_loop(runtime))
        logger.info("Playerok runtime запущен: telegram=%s playerok=%s", telegram_id, account.id)
        return runtime

    async def publish_playerok_drafts(
        self, telegram_id: int
    ) -> tuple[int, int, list[str]]:
        runtime = self.playerok_runtimes.get(telegram_id)
        if not runtime or PlayerokItemStatuses is None:
            raise RuntimeError("Playerok не подключён")
        async with runtime.publish_lock:
            page = await asyncio.to_thread(
                runtime.account.get_my_items,
                statuses=[PlayerokItemStatuses.DRAFT],
                count=24,
            )
            drafts = list(getattr(page, "items", []) or [])
            published = 0
            errors: list[str] = []
            for item in drafts:
                try:
                    statuses = await asyncio.to_thread(
                        runtime.account.get_item_priority_statuses,
                        item.id,
                        item.price,
                    )
                    free_status = next(
                        (status for status in statuses if int(status.price or 0) == 0),
                        None,
                    )
                    if not free_status:
                        raise RuntimeError("нет бесплатного статуса публикации")
                    await asyncio.to_thread(
                        runtime.account.publish_item,
                        item.id,
                        free_status.id,
                    )
                    published += 1
                except Exception as exc:
                    logger.exception("Playerok: не опубликован предмет %s", item.id)
                    errors.append(f"{clipped(getattr(item, 'name', item.id), 80)}: {clipped(exc, 120)}")
            return published, len(drafts), errors

    async def _process_playerok_message(
        self, runtime: PlayerokRuntime, row: asyncpg.Record, chat: Any, message: Any
    ) -> None:
        sender = getattr(message, "user", None)
        if str(getattr(sender, "id", "")) == str(runtime.account.id):
            return
        text = (getattr(message, "text", "") or "").strip()
        if not text:
            return
        command = text.casefold()
        command_rule = await self.db.find_playerok_command_reply(
            runtime.telegram_id, command
        )
        if command_rule:
            response = render_playerok_template(
                command_rule["response"], runtime.account, chat=chat, message=message
            )
            try:
                await asyncio.to_thread(runtime.account.send_message, str(chat.id), response)
            except Exception:
                logger.exception("Playerok: не отправлен ответ на команду %s", command)
            else:
                if command_rule["notify"]:
                    await self.safe_notify(
                        runtime.telegram_id,
                        "⌨️ <b>Сработала команда</b>\n\n"
                        f"Команда: <code>{html.escape(command)}</code>\n"
                        f"Покупатель: <b>{html.escape(clipped(getattr(sender, 'username', '—'), 120))}</b>\n"
                        f"Чат: <code>{html.escape(str(chat.id))}</code>",
                        marketplace="playerok",
                    )
            return
        hour = datetime.now().astimezone().hour
        if not (
            row["playerok_autoreply_enabled"]
            and within_work_hours(
                row["playerok_autoreply_work_start"],
                row["playerok_autoreply_work_end"],
                hour,
            )
            and await self.db.claim_playerok_autoreply(
                runtime.telegram_id,
                str(chat.id),
                row["playerok_autoreply_cooldown_minutes"],
            )
        ):
            return
        delay = int(row["playerok_autoreply_delay_seconds"])
        if delay:
            await asyncio.sleep(delay)
        if self.get_playerok(runtime.telegram_id) is not runtime or runtime.stop_event.is_set():
            return
        response = render_playerok_template(
            row["playerok_autoreply_text"], runtime.account, chat=chat, message=message
        )
        try:
            await asyncio.to_thread(runtime.account.send_message, str(chat.id), response)
        except Exception:
            logger.exception("Playerok: автоответ не отправлен в чат %s", chat.id)

    async def _process_playerok_delivery(
        self, runtime: PlayerokRuntime, row: asyncpg.Record, deal: Any
    ) -> None:
        if not row["playerok_auto_delivery_enabled"]:
            return
        status = getattr(getattr(deal, "status", None), "name", "")
        if status not in {"PENDING", "PAID"}:
            return
        item = getattr(deal, "item", None)
        chat = getattr(deal, "chat", None)
        item_id = str(getattr(item, "id", "") or "")
        chat_id = str(getattr(chat, "id", chat) or "")
        if not item_id or not chat_id:
            logger.warning("Playerok: у сделки %s нет товара или чата", getattr(deal, "id", "—"))
            return
        claim = await self.db.claim_playerok_delivery(
            runtime.telegram_id, str(deal.id), item_id
        )
        if not claim:
            return
        rule, products, remaining, error = claim
        if error:
            if row["playerok_notify_delivery"]:
                await self.safe_notify(
                    runtime.telegram_id,
                    "❌ <b>Ошибка автовыдачи</b>\n\n"
                    f"Сделка: <code>{html.escape(str(deal.id))}</code>\n"
                    f"Объявление: <b>{html.escape(clipped(rule['item_title'], 700))}</b>\n"
                    f"Причина: <code>{html.escape(error)}</code>",
                    marketplace="playerok",
                )
            return
        delivery_text = render_playerok_template(
            rule["response"], runtime.account, chat=chat, deal=deal
        ).replace("$product", "\n".join(products))
        try:
            await asyncio.to_thread(runtime.account.send_message, chat_id, delivery_text)
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous transport errors.
            await self.db.restore_playerok_delivery_products(
                runtime.telegram_id, rule["id"], products
            )
            await self.db.finish_playerok_delivery(
                runtime.telegram_id, str(deal.id), "failed", clipped(exc, 600)
            )
            if row["playerok_notify_delivery"]:
                await self.safe_notify(
                    runtime.telegram_id,
                    "❌ <b>Автовыдача не отправлена</b>\n\n"
                    f"Сделка: <code>{html.escape(str(deal.id))}</code>\n"
                    f"Причина: <code>{html.escape(clipped(exc, 600))}</code>",
                    marketplace="playerok",
                )
            return

        completion = "не запрашивалось"
        if row["playerok_auto_confirm_enabled"]:
            if PlayerokItemDealStatuses is None:
                completion = "не выполнено: библиотека не загрузила статусы"
            else:
                try:
                    await asyncio.to_thread(
                        runtime.account.update_deal,
                        str(deal.id),
                        PlayerokItemDealStatuses.SENT,
                    )
                    completion = "заказ отмечен выполненным"
                except Exception as exc:
                    completion = f"не удалось отметить выполненным: {clipped(exc, 240)}"
                    logger.exception("Playerok: не обновлён статус сделки %s", deal.id)
        await self.db.finish_playerok_delivery(
            runtime.telegram_id, str(deal.id), "sent", delivery_text
        )
        if row["playerok_notify_delivery"]:
            await self.safe_notify(
                runtime.telegram_id,
                "✅ <b>Автовыдача выполнена</b>\n\n"
                f"Сделка: <code>{html.escape(str(deal.id))}</code>\n"
                f"Объявление: <b>{html.escape(clipped(rule['item_title'], 700))}</b>\n"
                f"Остаток: <b>{remaining}</b>\n"
                f"Статус: <b>{html.escape(completion)}</b>\n\n"
                f"<pre>{html.escape(clipped(delivery_text, 1200))}</pre>",
                marketplace="playerok",
            )

    async def _playerok_snapshot(self, runtime: PlayerokRuntime) -> tuple[Any, Any, Any]:
        deals_call = {
            "count": 24,
            "direction": PlayerokItemDealDirections.OUT,
        }
        return await asyncio.gather(
            asyncio.to_thread(runtime.account.get_chats, count=24),
            asyncio.to_thread(runtime.account.get_deals, **deals_call),
            asyncio.to_thread(runtime.account.get_my_reviews, count=24),
        )

    async def _handle_playerok_snapshot(
        self, runtime: PlayerokRuntime, row: asyncpg.Record, snapshot: tuple[Any, Any, Any]
    ) -> None:
        chats_page, deals_page, reviews_page = snapshot
        chats = list(getattr(chats_page, "chats", []) or [])
        deals = list(getattr(deals_page, "deals", []) or [])
        reviews = list(getattr(reviews_page, "reviews", []) or [])
        message_ids = {
            str(chat.id): str(chat.last_message.id)
            for chat in chats
            if getattr(chat, "last_message", None)
        }
        deal_statuses = {
            str(deal.id): getattr(getattr(deal, "status", None), "name", "UNKNOWN")
            for deal in deals
        }
        review_ids = {str(review.id) for review in reviews}
        if not runtime.initialized:
            runtime.message_ids = message_ids
            runtime.deal_statuses = deal_statuses
            runtime.review_ids = review_ids
            runtime.initialized = True
            return

        for chat in chats:
            message = getattr(chat, "last_message", None)
            if message and runtime.message_ids.get(str(chat.id)) != str(message.id):
                await self._process_playerok_message(runtime, row, chat, message)
                await self.playerok_plugins.dispatch(
                    runtime.telegram_id, "BIND_TO_NEW_MESSAGE", chat, message
                )

        if row["playerok_notify_messages"]:
            for chat in chats:
                message = getattr(chat, "last_message", None)
                if not message or runtime.message_ids.get(str(chat.id)) == str(message.id):
                    continue
                sender = getattr(message, "user", None)
                if str(getattr(sender, "id", "")) == str(runtime.account.id):
                    continue
                await self.safe_notify(
                    runtime.telegram_id,
                    "💬 <b>Новое сообщение</b>\n\n"
                    f"👤 От: <b>{html.escape(clipped(getattr(sender, 'username', '—'), 120))}</b>\n"
                    f"🆔 Чат: <code>{html.escape(str(chat.id))}</code>\n\n"
                    f"<pre>{html.escape(clipped(getattr(message, 'text', None) or '[изображение]', 1400))}</pre>",
                    marketplace="playerok",
                    reply_markup=keyboard([[("↩️ Ответить", f"po_reply:{chat.id}"), ("💬 Чат", f"po_chat_full:{chat.id}:0")]]),
                )

        for deal in deals:
            if runtime.deal_statuses.get(str(deal.id)) is None:
                await self._process_playerok_delivery(runtime, row, deal)
            if runtime.deal_statuses.get(str(deal.id)) != deal_statuses[str(deal.id)]:
                await self.playerok_plugins.dispatch(
                    runtime.telegram_id,
                    "BIND_TO_DEAL_CHANGED",
                    deal,
                    runtime.deal_statuses.get(str(deal.id)),
                )

        if row["playerok_notify_deals"]:
            for deal in deals:
                deal_id = str(deal.id)
                status = deal_statuses[deal_id]
                previous = runtime.deal_statuses.get(deal_id)
                if previous == status:
                    continue
                item = getattr(deal, "item", None)
                buyer = getattr(deal, "user", None)
                title = "Новая сделка" if previous is None else "Статус сделки изменён"
                await self.safe_notify(
                    runtime.telegram_id,
                    f"📦 <b>{title}</b>\n\n"
                    f"🆔 Сделка: <code>{html.escape(deal_id)}</code>\n"
                    f"📌 Статус: <b>{html.escape(status)}</b>\n"
                    f"👤 Покупатель: <b>{html.escape(clipped(getattr(buyer, 'username', '—'), 120))}</b>\n"
                    f"🏷 Объявление: <b>{html.escape(clipped(getattr(item, 'name', '—'), 700))}</b>\n"
                    f"💰 Цена: <b>{format_money(getattr(item, 'price', 0))} ₽</b>",
                    marketplace="playerok",
                    reply_markup=keyboard([[("📦 Сделка", f"po_deal:{deal_id}")]]),
                )

        if row["playerok_notify_reviews"]:
            for review in reviews:
                if str(review.id) in runtime.review_ids:
                    continue
                creator = getattr(review, "creator", None)
                deal = getattr(review, "deal", None)
                item = getattr(deal, "item", None)
                rating = int(getattr(review, "rating", 0) or 0)
                await self.safe_notify(
                    runtime.telegram_id,
                    "⭐ <b>Новый отзыв</b>\n\n"
                    f"👤 Автор: <b>{html.escape(clipped(getattr(creator, 'username', '—'), 120))}</b>\n"
                    f"🌟 Оценка: <b>{'⭐' * rating} ({rating}/5)</b>\n"
                    f"🏷 Объявление: <b>{html.escape(clipped(getattr(item, 'name', '—'), 500))}</b>\n\n"
                    f"<pre>{html.escape(clipped(getattr(review, 'text', None) or 'Без комментария', 1200))}</pre>",
                    marketplace="playerok",
                    reply_markup=keyboard([[("📦 Сделка", f"po_deal:{getattr(deal, 'id', '')}")]]) if deal else None,
                )

        for review in reviews:
            if str(review.id) not in runtime.review_ids:
                await self.playerok_plugins.dispatch(
                    runtime.telegram_id, "BIND_TO_NEW_REVIEW", review
                )

        runtime.message_ids = message_ids
        runtime.deal_statuses = deal_statuses
        runtime.review_ids = review_ids

    async def _playerok_poll_loop(self, runtime: PlayerokRuntime) -> None:
        while not runtime.stop_event.is_set():
            try:
                row = await self.db.get_user(runtime.telegram_id)
                if not row or not row["playerok_active"]:
                    return
                snapshot = await self._playerok_snapshot(runtime)
                await self._handle_playerok_snapshot(runtime, row, snapshot)
                await self.playerok_plugins.dispatch(
                    runtime.telegram_id, "BIND_TO_TICK"
                )
                now = asyncio.get_running_loop().time()
                if row["playerok_auto_publish_enabled"] and now >= runtime.next_auto_publish_at:
                    published, total, errors = await self.publish_playerok_drafts(runtime.telegram_id)
                    runtime.next_auto_publish_at = now + PLAYEROK_AUTO_PUBLISH_SECONDS
                    if published or errors:
                        await self.safe_notify(
                            runtime.telegram_id,
                            f"📢 Автопубликация черновиков: <b>{published}/{total}</b> опубликовано"
                            + (f"\nОшибок: <b>{len(errors)}</b>" if errors else ""),
                            marketplace="playerok",
                        )
                runtime.poll_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.poll_failures += 1
                logger.exception("Ошибка Playerok polling для %s", runtime.telegram_id)
                if runtime.poll_failures in {1, 15}:
                    row = await self.db.get_user(runtime.telegram_id)
                    if row and row["playerok_notify_system"]:
                        await self.safe_notify(
                            runtime.telegram_id,
                            f"⚠️ Ошибка проверки Playerok: {html.escape(clipped(exc, 400))}",
                            marketplace="playerok",
                        )
            try:
                await asyncio.wait_for(
                    runtime.stop_event.wait(), timeout=PLAYEROK_POLL_SECONDS
                )
            except TimeoutError:
                pass

    async def start(
        self,
        telegram_id: int,
        row: asyncpg.Record | None = None,
        account: Account | None = None,
    ) -> AccountRuntime:
        self.loop = asyncio.get_running_loop()
        await self.stop(telegram_id)
        row = row or await self.db.get_user(telegram_id)
        if not row or not row["proxy_enc"] or not row["golden_key_enc"]:
            raise RuntimeError("Аккаунт не настроен")
        if account is None:
            proxy = self.secrets.decrypt(row["proxy_enc"])
            golden_key = self.secrets.decrypt(row["golden_key_enc"])
            account = Account(
                golden_key,
                user_agent=USER_AGENT,
                requests_timeout=20,
                proxy=proxy_dict(proxy),
                locale="ru",
            )
            await asyncio.to_thread(account.get)
        runner = Runner(account)
        runtime = AccountRuntime(
            telegram_id,
            account,
            runner,
            keep_online_enabled=bool(row["keep_online_enabled"]),
            auto_raise_enabled=bool(row["auto_raise_enabled"]),
        )
        self.runtimes[telegram_id] = runtime
        await self.plugins.load_runtime(telegram_id, runtime)
        runtime.tasks = [
            asyncio.create_task(asyncio.to_thread(runner.loop, runtime.stop_event)),
            asyncio.create_task(asyncio.to_thread(self._listen, runtime)),
            asyncio.create_task(self._auto_raise_loop(runtime)),
        ]
        logger.info("FunPay runtime запущен: telegram=%s funpay=%s", telegram_id, account.id)
        return runtime

    def _listen(self, runtime: AccountRuntime) -> None:
        assert self.loop is not None
        try:
            for event in runtime.runner.listen(
                requests_delay=4.0,
                ignore_exceptions=True,
                stop_event=runtime.stop_event,
                refresh_interval=2700 if runtime.keep_online_enabled else float("inf"),
            ):
                if runtime.stop_event.is_set():
                    break
                future = asyncio.run_coroutine_threadsafe(
                    self.handle_event(runtime, event), self.loop
                )
                try:
                    future.result(timeout=30)
                except Exception:
                    logger.exception("Ошибка обработки события FunPay")
        except Exception:
            logger.exception("Runner пользователя %s остановился", runtime.telegram_id)

    async def _auto_raise_loop(self, runtime: AccountRuntime) -> None:
        while not runtime.stop_event.is_set():
            try:
                row = await self.db.get_user(runtime.telegram_id)
                enabled = bool(row and row["account_active"] and row["auto_raise_enabled"])
                if enabled and asyncio.get_running_loop().time() >= runtime.next_raise_at:
                    await self.raise_lots_now(runtime.telegram_id, notify=True, force=False)
            except Exception:
                logger.exception("Ошибка цикла автоподнятия для %s", runtime.telegram_id)
            await asyncio.sleep(5)

    async def raise_lots_now(self, telegram_id: int, notify: bool = False, force: bool = True) -> str:
        runtime = self.get(telegram_id)
        if not runtime:
            raise RuntimeError("FunPay Runner не запущен")
        async with runtime.raise_lock:
            profile = await asyncio.to_thread(runtime.account.get_user, runtime.account.id)
            categories: dict[int, Any] = {}
            for subcategory in profile.get_sorted_lots(2):
                if subcategory.type is types.SubCategoryTypes.COMMON:
                    categories[subcategory.category.id] = subcategory.category
            if not categories:
                raise RuntimeError("В профиле нет обычных лотов для поднятия")

            results: list[str] = []
            waits: list[int] = []
            now = asyncio.get_running_loop().time()
            plugin_runtime = self.plugins.runtimes.get(telegram_id)
            for category_id, category in categories.items():
                category_name = category.name
                scheduled = runtime.raise_schedule.get(category_id, 0)
                if not force and scheduled > now:
                    waits.append(max(int(scheduled - now), 1))
                    continue
                try:
                    if plugin_runtime:
                        await self.plugins.dispatch(
                            telegram_id,
                            "BIND_TO_PRE_LOTS_RAISE",
                            plugin_runtime.adapter,
                            category,
                        )
                    wait = await asyncio.to_thread(runtime.account.raise_lots, category_id)
                    wait = max(int(wait or 3600), 60)
                    waits.append(wait)
                    runtime.raise_schedule[category_id] = now + wait
                    results.append(f"✅ {category_name}")
                    if plugin_runtime:
                        await self.plugins.dispatch(
                            telegram_id,
                            "BIND_TO_POST_LOTS_RAISE",
                            plugin_runtime.adapter,
                            category,
                            f"Подождите {wait} сек.",
                        )
                except fp_exceptions.RaiseError as exc:
                    wait = int(exc.wait_time or 3600)
                    wait = max(wait, 60)
                    waits.append(wait)
                    runtime.raise_schedule[category_id] = now + wait
                    results.append(
                        f"⏳ {category_name}: {clipped(exc.error_message or 'ещё рано', 160)}"
                    )
                except Exception as exc:
                    logger.exception("Не удалось поднять категорию %s", category_id)
                    waits.append(300)
                    runtime.raise_schedule[category_id] = now + 300
                    results.append(f"❌ {category_name}: {clipped(exc, 160)}")
                await asyncio.sleep(1)

            wait_seconds = min(waits, default=3600)
            runtime.next_raise_at = asyncio.get_running_loop().time() + wait_seconds
            runtime.last_raise_at = datetime.now(timezone.utc).astimezone()
            runtime.last_raise_summary = "\n".join(results) if results else "Категории ожидают своего таймера FunPay"
            text = "🆙 <b>Поднятие лотов</b>\n" + html.escape(runtime.last_raise_summary)
            row = await self.db.get_user(telegram_id)
            if notify and row and row["notify_lots_raise"]:
                await self.safe_notify(telegram_id, text)
            return text

    async def stop(self, telegram_id: int) -> None:
        runtime = self.runtimes.pop(telegram_id, None)
        if not runtime:
            return
        await self.plugins.stop_runtime(telegram_id)
        runtime.stop_event.set()
        for task in runtime.background_tasks:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *runtime.tasks,
                    *runtime.background_tasks,
                    return_exceptions=True,
                ),
                timeout=12,
            )
        except TimeoutError:
            logger.warning("Не все задачи runtime %s завершились вовремя", telegram_id)

    async def stop_playerok(self, telegram_id: int) -> None:
        runtime = self.playerok_runtimes.pop(telegram_id, None)
        if not runtime:
            return
        await self.playerok_plugins.stop_runtime(telegram_id)
        runtime.stop_event.set()
        if runtime.task:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)

    async def close(self) -> None:
        for telegram_id in list(self.runtimes):
            await self.stop(telegram_id)
        for telegram_id in list(self.playerok_runtimes):
            await self.stop_playerok(telegram_id)

    def get(self, telegram_id: int) -> AccountRuntime | None:
        return self.runtimes.get(telegram_id)

    def get_playerok(self, telegram_id: int) -> PlayerokRuntime | None:
        return self.playerok_runtimes.get(telegram_id)

    async def safe_notify(
        self,
        telegram_id: int,
        text: str,
        *,
        marketplace: str = "funpay",
        **kwargs: Any,
    ) -> None:
        label = "🟣 <b>FunPay</b>" if marketplace == "funpay" else "🔵 <b>Playerok</b>"
        body = f"{label}\n{text}"
        try:
            await self.bot.send_message(telegram_id, body, **kwargs)
        except Exception:
            logger.exception("Не удалось отправить Telegram-уведомление пользователю %s", telegram_id)
        try:
            targets = await self.db.list_notification_targets(telegram_id)
        except Exception:
            logger.exception("Не удалось получить дополнительные чаты уведомлений")
            return
        for target in targets:
            if not target["enabled"] or int(target["chat_id"]) == telegram_id:
                continue
            try:
                # Кнопки уведомления предназначены владельцу аккаунта. В группе
                # они позволяли бы участнику вызвать callback от своего Telegram ID.
                target_kwargs = {
                    key: value for key, value in kwargs.items() if key != "reply_markup"
                }
                await self.bot.send_message(
                    int(target["chat_id"]), body, **target_kwargs
                )
            except Exception:
                logger.warning(
                    "Не отправлено уведомление в дополнительный чат %s",
                    target["chat_id"],
                    exc_info=True,
                )

    async def _send_autoreply(
        self, runtime: AccountRuntime, message: Any, row: asyncpg.Record
    ) -> None:
        delay = int(row["autoreply_delay_seconds"])
        if delay:
            await asyncio.sleep(delay)
        if self.get(runtime.telegram_id) is not runtime or runtime.stop_event.is_set():
            return
        try:
            await asyncio.to_thread(
                runtime.account.send_message,
                message.chat_id,
                render_template(
                    row["autoreply_text"],
                    message=message,
                    account=runtime.account,
                ),
                message.chat_name,
            )
        except Exception:
            logger.exception("Автоответ не отправлен в чат %s", message.chat_id)
            if row["notify_system"]:
                await self.safe_notify(
                    runtime.telegram_id,
                    f"⚠️ Не удалось отправить автоответ в чат <code>{message.chat_id}</code>.",
                )

    async def _process_review(
        self, runtime: AccountRuntime, message: Any, row: asyncpg.Record
    ) -> None:
        order_id = extract_order_id(message)
        order = None
        if order_id:
            try:
                order = await asyncio.to_thread(runtime.account.get_order, order_id)
            except Exception:
                logger.exception("Не удалось получить заказ для отзыва %s", order_id)
        review = getattr(order, "review", None)
        title = (
            getattr(order, "title", None)
            or getattr(order, "description", None)
            or "не удалось определить"
        )
        stars = int(getattr(review, "stars", 0) or 0)
        comment = getattr(review, "text", None) or "без комментария"
        reply_text = None
        if (
            order
            and review
            and stars in range(1, 6)
            and order.seller_id == runtime.account.id
            and row["review_reply_enabled"]
        ):
            template = row[f"review_reply_{stars}"]
            reply_text = normalize_review_reply(
                render_template(
                    template,
                    order=order,
                    review=review,
                    account=runtime.account,
                )
            )
            if reply_text:
                try:
                    await asyncio.to_thread(
                        runtime.account.send_review, order.id, reply_text
                    )
                except Exception:
                    logger.exception("Не удалось ответить на отзыв заказа %s", order.id)
                    reply_text = None

        if row["notify_reviews"]:
            rating = f"{'⭐' * stars} ({stars}/5)" if stars else "удалён или не определён"
            review_event_title = {
                types.MessageTypes.NEW_FEEDBACK: "Новый отзыв",
                types.MessageTypes.FEEDBACK_CHANGED: "Отзыв изменён",
                types.MessageTypes.FEEDBACK_DELETED: "Отзыв удалён",
            }.get(message.type, "Событие отзыва")
            text = (
                f"⭐ <b>{review_event_title}</b>\n\n"
                f"📦 Заказ: <code>#{html.escape(order_id or '—')}</code>\n"
                f"🏷 Лот: <b>{html.escape(clipped(title, 900))}</b>\n"
                f"🌟 Оценка: <b>{rating}</b>\n\n"
                "💬 <b>Комментарий покупателя</b>\n"
                f"<pre>{html.escape(clipped(comment, 1500))}</pre>"
            )
            if reply_text:
                text += (
                    "\n🤖 <b>Автоответ отправлен</b>\n"
                    f"<pre>{html.escape(reply_text)}</pre>"
                )
            elif row["review_reply_enabled"]:
                text += "\n⚠️ Автоответ не отправлен: отзыв удалён, уже отвечен или данные заказа недоступны."
            buttons = []
            if order_id:
                buttons.append(
                    [("📦 Заказ", f"order_view:{order_id}"), ("✍️ Ответить", f"review_manual:{order_id}")]
                )
            if str(message.chat_id).isdigit():
                buttons.append([("💬 Открыть чат", f"chat_full:{message.chat_id}:0")])
            await self.safe_notify(
                runtime.telegram_id,
                text,
                reply_markup=keyboard(buttons) if buttons else None,
            )

    async def _process_command_reply(
        self, runtime: AccountRuntime, message: Any
    ) -> bool:
        command = (message.text or "").casefold().strip()
        if not command:
            return False
        rule = await self.db.find_command_reply(runtime.telegram_id, command)
        if not rule:
            return False
        response = render_template(
            rule["response"],
            message=message,
            account=runtime.account,
        )
        try:
            await asyncio.to_thread(
                runtime.account.send_message,
                message.chat_id,
                response,
                message.chat_name,
            )
        except Exception:
            logger.exception("Не отправлен ответ на команду %s", command)
            return False
        if rule["notify"]:
            await self.safe_notify(
                runtime.telegram_id,
                "⌨️ <b>Сработала команда</b>\n\n"
                f"Команда: <code>{html.escape(command)}</code>\n"
                f"Покупатель: <b>{html.escape(message.author or message.chat_name or '—')}</b>\n"
                f"Чат: <code>{html.escape(str(message.chat_id))}</code>",
            )
        return True

    async def _set_delivery_lot_state(
        self, runtime: AccountRuntime, rule: asyncpg.Record, active: bool
    ) -> bool:
        try:
            fields = await asyncio.to_thread(runtime.account.get_lot_fields, rule["lot_id"])
            if bool(fields.active) == active:
                return False
            fields.active = active
            await asyncio.to_thread(runtime.account.save_lot, fields)
            return True
        except Exception:
            logger.exception("Не изменено состояние лота автовыдачи %s", rule["lot_id"])
            return False

    async def _process_delivery(
        self, runtime: AccountRuntime, event: Any, row: asyncpg.Record
    ) -> None:
        order = event.order
        rule = await self.db.find_delivery_rule(
            runtime.telegram_id, order.description or ""
        )
        if not rule:
            return
        event.delivery_rule_id = rule["id"]
        event.delivered = False
        event.delivery_text = None
        event.goods_delivered = 0
        event.goods_left = len(rule["products"] or [])
        event.error = 0
        event.error_text = None

        remaining = len(rule["products"] or [])
        if row["auto_delivery_enabled"] and rule["enabled"]:
            amount = max(int(order.amount or 1), 1) if row["multi_delivery_enabled"] else 1
            claim = await self.db.claim_delivery(
                runtime.telegram_id,
                order.id,
                order.description or "",
                amount,
            )
            if claim:
                claimed_rule, products, remaining, error = claim
                event.goods_left = remaining
                plugin_runtime = self.plugins.runtimes.get(runtime.telegram_id)
                if plugin_runtime:
                    await self.plugins.dispatch(
                        runtime.telegram_id,
                        "BIND_TO_PRE_DELIVERY",
                        plugin_runtime.adapter,
                        event,
                    )
                if error:
                    event.error = 1
                    event.error_text = error
                else:
                    delivery_text = render_template(
                        claimed_rule["response"],
                        order=order,
                        account=runtime.account,
                    ).replace("$product", "\n".join(products))
                    try:
                        result = await asyncio.to_thread(
                            runtime.account.send_message,
                            order.chat_id,
                            delivery_text,
                            order.buyer_username,
                        )
                        if not result:
                            raise RuntimeError("FunPay не подтвердил отправку сообщения")
                    except Exception as exc:  # noqa: BLE001 - FunPay may raise several request errors.
                        await self.db.restore_delivery_products(
                            runtime.telegram_id, claimed_rule["id"], products
                        )
                        await self.db.finish_delivery(
                            runtime.telegram_id, order.id, "failed", clipped(exc, 600)
                        )
                        remaining += len(products)
                        event.goods_left = remaining
                        event.error = 1
                        event.error_text = clipped(exc, 600)
                    else:
                        await self.db.finish_delivery(
                            runtime.telegram_id, order.id, "sent", delivery_text
                        )
                        event.delivered = True
                        event.delivery_text = delivery_text
                        event.goods_delivered = amount
                if plugin_runtime:
                    await self.plugins.dispatch(
                        runtime.telegram_id,
                        "BIND_TO_POST_DELIVERY",
                        plugin_runtime.adapter,
                        event,
                    )
                if row["notify_delivery"]:
                    if event.delivered:
                        await self.safe_notify(
                            runtime.telegram_id,
                            "✅ <b>Автовыдача выполнена</b>\n\n"
                            f"Заказ: <code>#{html.escape(order.id)}</code>\n"
                            f"Лот: <b>{html.escape(clipped(order.description, 700))}</b>\n"
                            f"Выдано: <b>{event.goods_delivered}</b> · осталось: <b>{remaining}</b>\n\n"
                            f"<pre>{html.escape(clipped(event.delivery_text, 1400))}</pre>",
                        )
                    elif event.error:
                        await self.safe_notify(
                            runtime.telegram_id,
                            "❌ <b>Ошибка автовыдачи</b>\n\n"
                            f"Заказ: <code>#{html.escape(order.id)}</code>\n"
                            f"Лот: <b>{html.escape(clipped(order.description, 700))}</b>\n"
                            f"Причина: <code>{html.escape(clipped(event.error_text, 800))}</code>",
                        )

        has_stock_source = "$product" in rule["response"]
        should_disable = (
            rule["enabled"]
            and row["delivery_auto_disable"]
            and not rule["disable_auto_disable"]
            and has_stock_source
            and remaining == 0
        )
        should_restore = (
            rule["enabled"]
            and row["delivery_auto_restore"]
            and not rule["disable_auto_restore"]
            and not should_disable
        )
        changed = False
        if should_disable:
            changed = await self._set_delivery_lot_state(runtime, rule, False)
        elif should_restore:
            changed = await self._set_delivery_lot_state(runtime, rule, True)
        if changed and row["notify_delivery"]:
            await self.safe_notify(
                runtime.telegram_id,
                ("🔴 Лот деактивирован: закончились товары.\n" if should_disable else "🟢 Лот автоматически восстановлен.\n")
                + f"<b>{html.escape(clipped(rule['lot_title'], 1000))}</b>",
            )

    async def handle_event(self, runtime: AccountRuntime, event: Any) -> None:
        row = await self.db.get_user(runtime.telegram_id)
        if not row or not row["account_active"]:
            return
        await self.plugins.dispatch_event(runtime.telegram_id, event)
        if isinstance(event, events.NewMessageEvent):
            message = event.message
            review_types = {
                types.MessageTypes.NEW_FEEDBACK,
                types.MessageTypes.FEEDBACK_CHANGED,
                types.MessageTypes.FEEDBACK_DELETED,
                types.MessageTypes.NEW_FEEDBACK_ANSWER,
                types.MessageTypes.FEEDBACK_ANSWER_CHANGED,
                types.MessageTypes.FEEDBACK_ANSWER_DELETED,
            }
            if message.type in review_types:
                if message.type in {
                    types.MessageTypes.NEW_FEEDBACK,
                    types.MessageTypes.FEEDBACK_CHANGED,
                    types.MessageTypes.FEEDBACK_DELETED,
                }:
                    await self._process_review(runtime, message, row)
                return
            incoming = (
                message.author_id not in {0, runtime.account.id}
                and not message.by_bot
                and not message.by_vertex
            )
            if not incoming:
                return
            chat_id = str(message.chat_id)
            chat_name = html.escape(message.chat_name or "Неизвестный пользователь")
            body = html.escape(clipped(message.text or "[изображение]", 1200))
            if row["notify_messages"]:
                await self.safe_notify(
                    runtime.telegram_id,
                    "💬 <b>Новое сообщение</b>\n\n"
                    f"👤 От: <b>{chat_name}</b>\n"
                    f"🆔 Чат: <code>{html.escape(chat_id)}</code>\n\n"
                    f"<pre>{body}</pre>",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="↩️ Ответить", callback_data=f"reply:{chat_id}"),
                            InlineKeyboardButton(text="💬 Весь чат", callback_data=f"chat_full:{chat_id}:0"),
                        ],
                        [InlineKeyboardButton(text="🌐 FunPay", url=f"https://funpay.com/chat/?node={chat_id}")],
                    ]),
                )
            if (
                (message.text or "").strip().casefold() == "#status"
                and self.plugins.is_enabled(runtime.telegram_id, STATUS_PLUGIN_UUID)
            ):
                status_text = await self.db.get_plugin_setting(
                    runtime.telegram_id,
                    STATUS_PLUGIN_UUID,
                    "status_text",
                    "🟢 Продавец на связи. Можете оформлять заказ.",
                )
                await asyncio.to_thread(
                    runtime.account.send_message,
                    message.chat_id,
                    render_template(
                        status_text,
                        message=message,
                        account=runtime.account,
                    ),
                    message.chat_name,
                )
                return
            if await self._process_command_reply(runtime, message):
                return
            hour = datetime.now().astimezone().hour
            if (
                row["autoreply_enabled"]
                and within_work_hours(
                    row["autoreply_work_start"], row["autoreply_work_end"], hour
                )
                and await self.db.claim_autoreply(
                    runtime.telegram_id,
                    chat_id,
                    row["autoreply_cooldown_minutes"],
                    row["autoreply_new_chats_only"],
                )
            ):
                task = asyncio.create_task(self._send_autoreply(runtime, message, row))
                runtime.background_tasks.add(task)
                task.add_done_callback(runtime.background_tasks.discard)
        elif isinstance(event, events.NewOrderEvent):
            order = event.order
            await self._process_delivery(runtime, event, row)
            if not row["notify_new_orders"]:
                return
            buttons = [[
                InlineKeyboardButton(text="📦 Подробности", callback_data=f"order_view:{order.id}"),
                InlineKeyboardButton(text="🌐 FunPay", url=f"https://funpay.com/orders/{order.id}/"),
            ]]
            if str(order.chat_id).isdigit():
                buttons.append([
                    InlineKeyboardButton(
                        text="↩️ Ответить", callback_data=f"order_reply:{order.id}"
                    ),
                    InlineKeyboardButton(text="💬 Чат", callback_data=f"chat_full:{order.chat_id}:0"),
                ])
            buttons.append([InlineKeyboardButton(text="💸 Вернуть деньги", callback_data=f"refund_ask:{order.id}")])
            await self.safe_notify(
                runtime.telegram_id,
                "🛒 <b>Новый заказ</b>\n\n"
                f"🆔 Заказ: <code>#{html.escape(order.id)}</code>\n"
                f"👤 Покупатель: <b>{html.escape(order.buyer_username or '—')}</b>\n"
                f"💰 Сумма: <b>{format_money(order.price)} {html.escape(str(order.currency))}</b>\n\n"
                "🏷 <b>Лот</b>\n"
                f"<blockquote>{html.escape(clipped(order.description, 1400))}</blockquote>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        elif isinstance(event, events.OrderStatusChangedEvent) and row["notify_order_status"]:
            order = event.order
            await self.safe_notify(
                runtime.telegram_id,
                "📦 <b>Статус заказа изменён</b>\n\n"
                f"🆔 Заказ: <code>#{html.escape(order.id)}</code>\n"
                f"📌 Новый статус: <b>{html.escape(order_status_label(order.status))}</b>\n\n"
                "🏷 <b>Лот</b>\n"
                f"<blockquote>{html.escape(clipped(order.description or '—', 1200))}</blockquote>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📦 Подробности", callback_data=f"order_view:{order.id}"),
                    InlineKeyboardButton(text="🌐 FunPay", url=f"https://funpay.com/orders/{order.id}/"),
                ]]),
            )


class ConnectState(StatesGroup):
    proxy = State()
    golden_key = State()


class PlayerokConnectState(StatesGroup):
    proxy = State()
    cookie = State()


class PlayerokChatState(StatesGroup):
    text = State()
    image = State()


class PlayerokAutoReplyState(StatesGroup):
    text = State()
    delay = State()
    cooldown = State()
    hours = State()


class PlayerokCommandState(StatesGroup):
    trigger = State()
    response = State()


class PlayerokDeliveryState(StatesGroup):
    response = State()
    products = State()


class PlayerokItemCreateState(StatesGroup):
    game_search = State()
    name = State()
    price = State()
    description = State()
    field_value = State()


class PlayerokPluginState(StatesGroup):
    file = State()
    setting_value = State()


class PlayerokCatalogPublishState(StatesGroup):
    description = State()


class AutoReplyState(StatesGroup):
    text = State()
    cooldown = State()
    delay = State()
    hours = State()
    review_text = State()


class DeliveryRuleState(StatesGroup):
    response = State()
    products = State()


class CommandReplyState(StatesGroup):
    trigger = State()
    response = State()


class NotificationTargetState(StatesGroup):
    chat_id = State()


class ReviewReplyState(StatesGroup):
    text = State()


class SendMessageState(StatesGroup):
    chat_id = State()
    text = State()


class UploadImageState(StatesGroup):
    chat_id = State()
    chat_file = State()
    lot_id = State()
    lot_file = State()


class OrderState(StatesGroup):
    order_id = State()


class PluginState(StatesGroup):
    file = State()


class CatalogPublishState(StatesGroup):
    description = State()


class StatusPluginState(StatesGroup):
    text = State()


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_keyboard(marketplace: str = "funpay") -> InlineKeyboardMarkup:
    switch = [
        (
            "🔄 Площадка: FunPay" if marketplace == "funpay" else "🔄 Площадка: Playerok",
            "marketplace_switch",
        )
    ]
    if marketplace == "playerok":
        return keyboard([
            switch,
            [("👤 Профиль", "po_profile"), ("💰 Баланс", "po_balance")],
            [("💬 Чаты", "po_chats"), ("📦 Сделки", "po_deals")],
            [("📢 Объявления", "po_items"), ("➕ Создать", "po_item_create")],
            [("🤖 Автоответчик", "po_autoreply"), ("📤 Автовыдача", "po_delivery")],
            [("🧩 Плагины", "po_plugins"), ("🔔 Уведомления", "po_notifications")],
            [("⚙️ Аккаунт Playerok", "po_account")],
        ])
    return keyboard([
        switch,
        [("👤 Подробный профиль", "profile"), ("💰 Баланс", "balance")],
        [("🔔 Уведомления", "notifications"), ("🤖 Автоответчик", "autoreply")],
        [("💬 Последние чаты", "chats"), ("📦 Заказ по ID", "order_lookup")],
        [("📤 Автовыдача", "delivery"), ("⌨️ Команды", "command_replies")],
        [("🆙 Автоподнятие", "auto_raise"), ("🧩 Плагины", "plugins")],
        [("⚙️ Аккаунт", "account")],
    ])


def conversation_actions_keyboard(chat_id: int | str) -> InlineKeyboardMarkup:
    return keyboard([
        [
            ("✍️ Написать ещё", f"reply:{chat_id}"),
            ("💬 Перейти к диалогу", f"chat_full:{chat_id}:0"),
        ],
        [("📷 Отправить фото", f"image_chat:{chat_id}")],
    ])


def bool_icon(value: bool) -> str:
    return "✅" if value else "❌"


def build_router(db: Database, manager: RuntimeManager, secrets: SecretBox) -> Router:
    router = Router()
    # Учетные данные принимаются только в личной переписке с ботом.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def show_main(target: Message, user_id: int, text: str = "Выберите действие:") -> None:
        row = await db.get_user(user_id)
        if not row or not (row["account_active"] or row["playerok_active"]):
            await target.answer(
                "Выберите площадку, которую хотите настроить первой:",
                reply_markup=keyboard([
                    [("🟣 Настроить FunPay", "connect_funpay")],
                    [("🔵 Настроить Playerok", "connect_playerok")],
                ]),
            )
            return
        marketplace = row["active_marketplace"]
        if marketplace == "playerok" and not row["playerok_active"]:
            marketplace = "funpay"
            await db.set_active_marketplace(user_id, marketplace)
        elif marketplace == "funpay" and not row["account_active"]:
            marketplace = "playerok"
            await db.set_active_marketplace(user_id, marketplace)
        if marketplace == "playerok":
            online = "🟢" if manager.get_playerok(user_id) else "🔴"
            username = row["playerok_username"] or "Playerok"
            label = "🔵 Playerok"
        else:
            online = "🟢" if manager.get(user_id) else "🔴"
            username = row["funpay_username"] or "FunPay"
            label = "🟣 FunPay"
        await target.answer(
            f"{label} · {online} <b>{html.escape(username)}</b>\n{text}",
            reply_markup=main_keyboard(marketplace),
        )

    async def require_runtime(target: Message, user_id: int) -> AccountRuntime | None:
        runtime = manager.get(user_id)
        if runtime:
            return runtime
        row = await db.get_user(user_id)
        if not row or not row["account_active"]:
            await target.answer("Сначала подключите аккаунт FunPay через /start.")
            return None
        await target.answer("Подключение неактивно. Откройте «Аккаунт» → «Переподключить».")
        return None

    async def require_playerok_runtime(
        target: Message, user_id: int
    ) -> PlayerokRuntime | None:
        runtime = manager.get_playerok(user_id)
        if runtime:
            return runtime
        row = await db.get_user(user_id)
        if not row or not row["playerok_active"]:
            await target.answer(
                "Сначала подключите Playerok.",
                reply_markup=keyboard([[("🔵 Подключить Playerok", "connect_playerok")]]),
            )
            return None
        await target.answer("Playerok неактивен. Откройте настройки аккаунта и переподключитесь.")
        return None

    async def begin_connect(target: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(ConnectState.proxy)
        await target.answer(
            "1/2. Отправьте прокси. Поддерживаемые форматы:\n"
            "<code>http://user:password@host:port</code>\n"
            "<code>socks5://user:password@host:port</code>\n\n"
            "Сообщение будет удалено после обработки. Для отмены: /cancel"
        )

    async def begin_playerok_connect(target: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(PlayerokConnectState.proxy)
        await target.answer(
            "🔵 <b>Playerok · шаг 1/2</b>\n\n"
            "Отправьте IPv4 HTTP/HTTPS-прокси, желательно тот же IP, с которого получены cookie:\n"
            "<code>http://user:password@host:port</code>\n\n"
            "Сообщение будет удалено после обработки. Для отмены: /cancel"
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await db.ensure_user(message.from_user.id)
        row = await db.get_user(message.from_user.id)
        if row and (row["account_active"] or row["playerok_active"]):
            if row["account_active"] and not manager.get(message.from_user.id):
                try:
                    await manager.start(message.from_user.id, row=row)
                except Exception:
                    logger.exception("Ручной запуск FunPay не удался")
            if row["playerok_active"] and not manager.get_playerok(message.from_user.id):
                try:
                    await manager.start_playerok(message.from_user.id, row=row)
                except Exception:
                    logger.exception("Ручной запуск Playerok не удался")
            await show_main(message, message.from_user.id)
        else:
            await show_main(message, message.from_user.id)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_main(message, message.from_user.id, "Текущее действие отменено.")

    @router.callback_query(F.data.in_({"connect", "connect_funpay"}))
    async def connect_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await begin_connect(callback.message, state)

    @router.callback_query(F.data == "connect_playerok")
    async def connect_playerok_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await begin_playerok_connect(callback.message, state)

    @router.message(ConnectState.proxy, F.text)
    async def accept_proxy(message: Message, state: FSMContext) -> None:
        try:
            proxy = normalize_proxy(message.text)
        except ValueError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}. Попробуйте ещё раз или /cancel.")
            return
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await state.update_data(proxy=proxy)
        await state.set_state(ConnectState.golden_key)
        await message.answer(
            "2/2. Теперь отправьте <b>golden_key</b> из cookie FunPay. "
            "После проверки это сообщение тоже будет удалено."
        )

    @router.message(ConnectState.golden_key, F.text)
    async def accept_golden_key(message: Message, state: FSMContext) -> None:
        golden_key = message.text.strip()
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        if len(golden_key) < 16 or len(golden_key) > 512:
            await message.answer("❌ golden_key выглядит некорректно. Отправьте его повторно или /cancel.")
            return
        data = await state.get_data()
        proxy = data.get("proxy")
        if not proxy:
            await state.clear()
            await begin_connect(message, state)
            return
        wait_message = await message.answer("⏳ Проверяю прокси и авторизацию FunPay…")
        account = Account(
            golden_key,
            user_agent=USER_AGENT,
            requests_timeout=20,
            proxy=proxy_dict(proxy),
            locale="ru",
        )
        try:
            await asyncio.wait_for(asyncio.to_thread(account.get), timeout=45)
        except Exception as exc:  # noqa: BLE001 - FunPay/requests raises several unrelated network exceptions.
            logger.warning("Проверка аккаунта не пройдена: %s", type(exc).__name__)
            await wait_message.edit_text(
                "❌ FunPay не принял данные. Проверьте доступность прокси и актуальность golden_key.\n"
                "Отправьте golden_key повторно либо начните заново через /cancel и /start."
            )
            return
        await db.save_account(
            message.from_user.id,
            secrets.encrypt(proxy),
            secrets.encrypt(golden_key),
            account,
        )
        row = await db.get_user(message.from_user.id)
        try:
            await manager.start(message.from_user.id, row=row, account=account)
        except Exception:
            logger.exception("Не удалось запустить runtime после успешной авторизации")
            await wait_message.edit_text("⚠️ Аккаунт сохранён, но слежение не запустилось. Попробуйте переподключить.")
            await state.clear()
            return
        await state.clear()
        await wait_message.edit_text(
            f"✅ Подключён FunPay-аккаунт <b>{html.escape(account.username or '—')}</b> "
            f"(<code>{account.id}</code>)."
        )
        await show_main(message, message.from_user.id)

    @router.message(PlayerokConnectState.proxy, F.text)
    async def accept_playerok_proxy(message: Message, state: FSMContext) -> None:
        try:
            proxy = normalize_proxy(message.text)
            playerok_proxy_value(proxy)
        except ValueError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}. Попробуйте ещё раз или /cancel.")
            return
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await state.update_data(playerok_proxy=proxy)
        await state.set_state(PlayerokConnectState.cookie)
        await message.answer(
            "🔵 <b>Playerok · шаг 2/2</b>\n\n"
            "Отправьте полный заголовок cookie из браузера, например:\n"
            "<code>__ddg5_=...; token=...</code>\n\n"
            "Можно отправить только значение <code>token</code>, но при включённой защите Playerok "
            "понадобится полный набор cookie. Сообщение будет удалено."
        )

    @router.message(PlayerokConnectState.cookie, F.text)
    async def accept_playerok_cookie(message: Message, state: FSMContext) -> None:
        cookie = message.text.strip()
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        if not 16 <= len(cookie) <= 4096:
            await message.answer("❌ Cookie выглядит некорректно. Отправьте его снова или /cancel.")
            return
        data = await state.get_data()
        proxy = data.get("playerok_proxy")
        if not proxy:
            await begin_playerok_connect(message, state)
            return
        wait_message = await message.answer("⏳ Проверяю прокси и cookie Playerok…")
        try:
            account = create_playerok_account(cookie, proxy)
            await asyncio.wait_for(asyncio.to_thread(account.get), timeout=50)
        except Exception as exc:
            logger.warning("Проверка Playerok не пройдена", exc_info=True)
            await wait_message.edit_text(
                "❌ Playerok не принял данные. Проверьте, что cookie актуальны, прокси имеет тот же IP "
                f"и защита не запросила новую __ddg5_.\n\nОшибка: <code>{html.escape(clipped(exc, 500))}</code>"
            )
            return
        await db.save_playerok_account(
            message.from_user.id,
            secrets.encrypt(proxy),
            secrets.encrypt(cookie),
            account,
        )
        row = await db.get_user(message.from_user.id)
        try:
            await manager.start_playerok(message.from_user.id, row=row, account=account)
        except Exception:
            logger.exception("Playerok сохранён, но polling не запустился")
            await wait_message.edit_text(
                "⚠️ Playerok сохранён, но слежение не запустилось. Попробуйте переподключить."
            )
            await state.clear()
            return
        await state.clear()
        await wait_message.edit_text(
            f"✅ Подключён Playerok-аккаунт <b>{html.escape(account.username or '—')}</b> "
            f"(<code>{html.escape(str(account.id))}</code>)."
        )
        await show_main(message, message.from_user.id)

    @router.callback_query(F.data == "marketplace_switch")
    async def marketplace_switch(callback: CallbackQuery) -> None:
        row = await db.get_user(callback.from_user.id)
        if not row:
            await callback.answer("Сначала выберите площадку", show_alert=True)
            return
        destination = "playerok" if row["active_marketplace"] == "funpay" else "funpay"
        configured = row["playerok_active"] if destination == "playerok" else row["account_active"]
        if not configured:
            await callback.answer("Эта площадка ещё не настроена", show_alert=True)
            await callback.message.answer(
                f"Подключите {'Playerok' if destination == 'playerok' else 'FunPay'}, чтобы переключиться.",
                reply_markup=keyboard([[(
                    f"Настроить {'Playerok' if destination == 'playerok' else 'FunPay'}",
                    "connect_playerok" if destination == "playerok" else "connect_funpay",
                )]]),
            )
            return
        await db.set_active_marketplace(callback.from_user.id, destination)
        await callback.answer("Площадка переключена")
        await show_main(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_profile")
    async def playerok_profile(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю…")
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            account = await asyncio.to_thread(runtime.account.get)
            profile = account.profile
        except Exception as exc:
            logger.exception("Не удалось получить профиль Playerok")
            await callback.message.answer(f"❌ Playerok не отдал профиль: {html.escape(clipped(exc, 400))}")
            return
        stats = getattr(profile, "stats", None)
        item_stats = getattr(stats, "items", None)
        deal_stats = getattr(stats, "deals", None)
        outgoing = getattr(deal_stats, "outgoing", None)
        await callback.message.answer(
            "🔵 <b>Профиль Playerok</b>\n\n"
            f"👤 Пользователь: <b>{html.escape(account.username or '—')}</b>\n"
            f"🆔 ID: <code>{html.escape(str(account.id))}</code>\n"
            f"⭐ Рейтинг: <b>{getattr(profile, 'rating', '—')}</b> · отзывов: <b>{getattr(profile, 'reviews_count', 0)}</b>\n"
            f"📢 Объявлений: <b>{getattr(item_stats, 'total', 0)}</b> · завершено: <b>{getattr(item_stats, 'finished', 0)}</b>\n"
            f"📦 Продаж: <b>{getattr(outgoing, 'total', 0)}</b> · завершено: <b>{getattr(outgoing, 'finished', 0)}</b>\n"
            f"✅ Верификация: {bool_icon(bool(getattr(profile, 'is_verified', False)))}\n"
            f"📢 Можно публиковать: {bool_icon(bool(account.can_publish_items))}\n"
            f"🚫 Блокировка: {'❌ есть' if account.is_blocked else '✅ нет'}",
            reply_markup=keyboard([[("🔄 Обновить", "po_profile"), ("⬅️ Меню", "menu")]]),
        )

    @router.callback_query(F.data == "po_balance")
    async def playerok_balance(callback: CallbackQuery) -> None:
        await callback.answer("Проверяю баланс…")
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            account = await asyncio.to_thread(runtime.account.get)
            balance = account.profile.balance
        except Exception as exc:
            logger.exception("Не удалось получить баланс Playerok")
            await callback.message.answer(f"❌ Баланс недоступен: {html.escape(clipped(exc, 400))}")
            return
        await callback.message.answer(
            "🔵 <b>Баланс Playerok</b>\n\n"
            f"💰 Всего: <b>{format_money(balance.value)} ₽</b>\n"
            f"✅ Доступно: <b>{format_money(balance.available)} ₽</b>\n"
            f"🏦 Можно вывести: <b>{format_money(balance.withdrawable)} ₽</b>\n"
            f"🧊 Заморожено: <b>{format_money(balance.frozen)} ₽</b>\n"
            f"⏳ Ожидаемый доход: <b>{format_money(balance.pending_income)} ₽</b>",
            reply_markup=keyboard([[("🔄 Обновить", "po_balance"), ("⬅️ Меню", "menu")]]),
        )

    async def show_playerok_chat_carousel(target: Message, user_id: int, index: int) -> None:
        runtime = await require_playerok_runtime(target, user_id)
        if not runtime:
            return
        try:
            page = await asyncio.to_thread(runtime.account.get_chats, count=24)
            chats = list(getattr(page, "chats", []) or [])
        except Exception as exc:
            logger.exception("Не удалось получить чаты Playerok")
            await target.answer(f"❌ Чаты недоступны: {html.escape(clipped(exc, 400))}")
            return
        if not chats:
            await target.answer("Чатов Playerok пока нет.", reply_markup=keyboard([[("⬅️ Меню", "menu")]]))
            return
        index %= len(chats)
        chat = chats[index]
        message = getattr(chat, "last_message", None)
        sender = getattr(message, "user", None)
        author = getattr(sender, "username", None) or "—"
        text = (
            f"💬 <b>{html.escape(clipped(author, 160))}</b>\n"
            f"Чат: <code>{html.escape(str(chat.id))}</code> · непрочитано: <b>{getattr(chat, 'unread_messages_counter', 0)}</b>\n"
            f"Позиция: <b>{index + 1}/{len(chats)}</b>\n\n"
            f"<pre>{html.escape(clipped(getattr(message, 'text', None) or '[изображение / событие]', 1700))}</pre>"
        )
        markup = keyboard([
            [("⬅️", f"po_chat_view:{(index - 1) % len(chats)}"), (f"{index + 1}/{len(chats)}", "noop"), ("➡️", f"po_chat_view:{(index + 1) % len(chats)}")],
            [("📖 Открыть диалог", f"po_chat_full:{chat.id}:{index}")],
            [("↩️ Ответить", f"po_reply:{chat.id}"), ("📷 Фото", f"po_image:{chat.id}")],
            [("✓ Прочитать", f"po_chat_read:{chat.id}"), ("⬅️ Меню", "menu")],
        ])
        try:
            await target.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.answer(text, reply_markup=markup)

    @router.callback_query(F.data == "po_chats")
    async def playerok_chats(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю…")
        await show_playerok_chat_carousel(callback.message, callback.from_user.id, 0)

    @router.callback_query(F.data.startswith("po_chat_view:"))
    async def playerok_chat_view(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_chat_carousel(
            callback.message, callback.from_user.id, int(callback.data.split(":", 1)[1])
        )

    @router.callback_query(F.data.startswith("po_chat_full:"))
    async def playerok_chat_full(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю историю…")
        _, chat_id, raw_index = callback.data.split(":", 2)
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            messages: list[Any] = []
            cursor = None
            for _ in range(10):  # Защитный предел: до 240 сообщений.
                page = await asyncio.to_thread(
                    runtime.account.get_chat_messages, chat_id, count=24, after_cursor=cursor
                )
                batch = list(getattr(page, "messages", []) or [])
                messages.extend(batch)
                page_info = getattr(page, "page_info", None)
                cursor = getattr(page_info, "end_cursor", None)
                if not batch or not getattr(page_info, "has_next_page", False) or not cursor:
                    break
        except Exception as exc:
            logger.exception("Не удалось загрузить чат Playerok %s", chat_id)
            await callback.message.answer(f"❌ История недоступна: {html.escape(clipped(exc, 400))}")
            return
        lines = [f"💬 <b>Чат Playerok</b> · <code>{html.escape(chat_id)}</code>\n"]
        for item in reversed(messages):
            sender = getattr(item, "user", None)
            author = getattr(sender, "username", None) or "Система"
            own = str(getattr(sender, "id", "")) == str(runtime.account.id)
            prefix = "Вы" if own else author
            lines.append(
                f"<b>{html.escape(clipped(prefix, 100))}</b>\n"
                f"<pre>{html.escape(clipped(getattr(item, 'text', None) or '[изображение / событие]', 1200))}</pre>"
            )
        text = "\n".join(lines)
        for part in [text[i:i + 3500] for i in range(0, len(text), 3500)] or ["Сообщений нет."]:
            await callback.message.answer(part)
        await callback.message.answer(
            "Показано до 240 последних сообщений — это защитный предел для длинных диалогов.",
            reply_markup=keyboard([
                [("↩️ Ответить", f"po_reply:{chat_id}"), ("📷 Фото", f"po_image:{chat_id}")],
                [("⬅️ К чатам", f"po_chat_view:{raw_index}")],
            ]),
        )

    @router.callback_query(F.data.startswith("po_chat_read:"))
    async def playerok_chat_read(callback: CallbackQuery) -> None:
        chat_id = callback.data.split(":", 1)[1]
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            await asyncio.to_thread(runtime.account.mark_chat_as_read, chat_id)
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous errors.
            await callback.answer(f"Не удалось: {clipped(exc, 120)}", show_alert=True)
            return
        await callback.answer("Чат отмечен прочитанным")

    @router.callback_query(F.data.startswith("po_reply:"))
    async def playerok_reply(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_playerok_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.set_state(PlayerokChatState.text)
        await state.update_data(playerok_chat_id=callback.data.split(":", 1)[1])
        await callback.message.answer("Введите ответ до 4000 символов или /cancel.")

    @router.message(PlayerokChatState.text, F.text)
    async def playerok_reply_text(message: Message, state: FSMContext) -> None:
        text = message.text.strip()
        if not 1 <= len(text) <= 4000:
            await message.answer("Сообщение должно быть от 1 до 4000 символов.")
            return
        runtime = await require_playerok_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        chat_id = str((await state.get_data()).get("playerok_chat_id") or "")
        if not chat_id:
            await state.clear()
            return
        try:
            await asyncio.to_thread(runtime.account.send_message, chat_id, text)
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous errors.
            await message.answer(f"❌ Playerok не отправил сообщение: {html.escape(clipped(exc, 400))}")
            return
        await state.clear()
        await message.answer(
            "✅ Сообщение отправлено.",
            reply_markup=keyboard([[("✍️ Написать ещё", f"po_reply:{chat_id}"), ("💬 Открыть чат", f"po_chat_full:{chat_id}:0")]]),
        )

    @router.callback_query(F.data.startswith("po_image:"))
    async def playerok_image(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_playerok_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.set_state(PlayerokChatState.image)
        await state.update_data(playerok_chat_id=callback.data.split(":", 1)[1])
        await callback.message.answer("Отправьте фото или изображение-файл до 20 МБ. Для отмены: /cancel")

    @router.message(PlayerokChatState.image)
    async def playerok_image_send(message: Message, state: FSMContext) -> None:
        runtime = await require_playerok_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        image = await download_telegram_image(message)
        if image is None:
            return
        chat_id = str((await state.get_data()).get("playerok_chat_id") or "")
        try:
            await asyncio.to_thread(runtime.account.send_message, chat_id, None, [image])
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous errors.
            await message.answer(f"❌ Не удалось отправить изображение: {html.escape(clipped(exc, 400))}")
            return
        await state.clear()
        await message.answer("✅ Изображение отправлено.", reply_markup=keyboard([[("💬 Чат", f"po_chat_full:{chat_id}:0")]]))

    async def show_playerok_deals(target: Message, user_id: int) -> None:
        runtime = await require_playerok_runtime(target, user_id)
        if not runtime:
            return
        try:
            page = await asyncio.to_thread(
                runtime.account.get_deals, direction=PlayerokItemDealDirections.OUT, count=24
            )
            deals = list(getattr(page, "deals", []) or [])
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI may fail at the request/parser layer.
            await target.answer(f"❌ Сделки недоступны: {html.escape(clipped(exc, 400))}")
            return
        rows = [[(
            f"{getattr(getattr(deal, 'status', None), 'name', '—')} · {clipped(getattr(getattr(deal, 'item', None), 'name', '—'), 26)}",
            f"po_deal:{deal.id}",
        )] for deal in deals]
        rows.append([("⬅️ Меню", "menu")])
        await target.answer(
            f"📦 <b>Продажи Playerok</b>\n\nПоказано: <b>{len(deals)}</b>.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "po_deals")
    async def playerok_deals(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю…")
        await show_playerok_deals(callback.message, callback.from_user.id)

    async def show_playerok_deal(target: Message, user_id: int, deal_id: str) -> None:
        runtime = await require_playerok_runtime(target, user_id)
        if not runtime:
            return
        try:
            deal = await asyncio.to_thread(runtime.account.get_deal, deal_id)
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous errors.
            await target.answer(f"❌ Сделка недоступна: {html.escape(clipped(exc, 400))}")
            return
        item = getattr(deal, "item", None)
        buyer = getattr(deal, "user", None)
        chat = getattr(deal, "chat", None)
        status = getattr(getattr(deal, "status", None), "name", "—")
        rows = []
        if status in {"PENDING", "PAID"}:
            rows.append([("✅ Отметить выполненным", f"po_deal_sent_ask:{deal_id}")])
        if getattr(chat, "id", None):
            rows.append([("💬 Открыть чат", f"po_chat_full:{chat.id}:0")])
        rows.append([("⬅️ Сделки", "po_deals")])
        await target.answer(
            "📦 <b>Сделка Playerok</b>\n\n"
            f"ID: <code>{html.escape(str(deal.id))}</code>\n"
            f"Статус: <b>{html.escape(status)}</b>\n"
            f"Покупатель: <b>{html.escape(clipped(getattr(buyer, 'username', '—'), 160))}</b>\n"
            f"Объявление: <b>{html.escape(clipped(getattr(item, 'name', '—'), 700))}</b>\n"
            f"Цена: <b>{format_money(getattr(item, 'price', 0))} ₽</b>\n"
            f"Комментарий: <pre>{html.escape(clipped(getattr(deal, 'comment_from_buyer', None) or '—', 1200))}</pre>",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("po_deal:"))
    async def playerok_deal(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_deal(callback.message, callback.from_user.id, callback.data.split(":", 1)[1])

    @router.callback_query(F.data.startswith("po_deal_sent_ask:"))
    async def playerok_deal_sent_ask(callback: CallbackQuery) -> None:
        deal_id = callback.data.split(":", 1)[1]
        await callback.answer()
        await callback.message.answer(
            "Отметить сделку выполненной? Это действие меняет статус сделки на Playerok.",
            reply_markup=keyboard([[("Да, выполнить", f"po_deal_sent:{deal_id}"), ("Отмена", f"po_deal:{deal_id}")]]),
        )

    @router.callback_query(F.data.startswith("po_deal_sent:"))
    async def playerok_deal_sent(callback: CallbackQuery) -> None:
        deal_id = callback.data.split(":", 1)[1]
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime or PlayerokItemDealStatuses is None:
            return
        try:
            await asyncio.to_thread(runtime.account.update_deal, deal_id, PlayerokItemDealStatuses.SENT)
        except Exception as exc:  # noqa: BLE001 - Playerok may reject a status transition.
            await callback.answer(f"Не удалось: {clipped(exc, 120)}", show_alert=True)
            return
        await callback.answer("Сделка отмечена выполненной")
        await show_playerok_deal(callback.message, callback.from_user.id, deal_id)

    async def show_playerok_autoreply(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        await target.answer(
            "🤖 <b>Автоответчик Playerok</b>\n\n"
            f"Рабочее время: <b>{row['playerok_autoreply_work_start']:02d}:00–{row['playerok_autoreply_work_end']:02d}:00</b>\n"
            f"Задержка: <b>{row['playerok_autoreply_delay_seconds']} сек.</b> · повтор: <b>{row['playerok_autoreply_cooldown_minutes']} мин.</b>\n\n"
            f"Текст:\n<pre>{html.escape(clipped(row['playerok_autoreply_text'], 1200))}</pre>",
            reply_markup=keyboard([
                [(f"{bool_icon(row['playerok_autoreply_enabled'])} Включён", "po_toggle:playerok_autoreply_enabled")],
                [("✏️ Текст", "po_ar_text"), ("⌨️ Команды", "po_commands")],
                [("⏱ Задержка", "po_ar_delay"), ("🔁 Интервал", "po_ar_cooldown")],
                [("🕒 Рабочее время", "po_ar_hours")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "po_autoreply")
    async def playerok_autoreply(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_autoreply(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_ar_text")
    async def playerok_autoreply_text(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokAutoReplyState.text)
        await callback.message.answer("Отправьте текст автоответа до 1500 символов или /cancel.")

    @router.message(PlayerokAutoReplyState.text, F.text)
    async def playerok_autoreply_text_save(message: Message, state: FSMContext) -> None:
        text = message.text.strip()
        if not 1 <= len(text) <= 1500:
            await message.answer("Текст должен быть от 1 до 1500 символов.")
            return
        await db.set_playerok_autoreply_text(message.from_user.id, text)
        await state.clear()
        await show_playerok_autoreply(message, message.from_user.id)

    @router.callback_query(F.data.in_({"po_ar_delay", "po_ar_cooldown"}))
    async def playerok_autoreply_number(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        is_delay = callback.data == "po_ar_delay"
        await state.set_state(PlayerokAutoReplyState.delay if is_delay else PlayerokAutoReplyState.cooldown)
        await callback.message.answer(
            "Введите задержку от 0 до 300 секунд." if is_delay else "Введите интервал от 0 до 1440 минут."
        )

    @router.message(PlayerokAutoReplyState.delay, F.text)
    async def playerok_autoreply_delay_save(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not value.isdigit() or not 0 <= int(value) <= 300:
            await message.answer("Введите целое число от 0 до 300.")
            return
        await db.set_integer_setting(message.from_user.id, "playerok_autoreply_delay_seconds", int(value))
        await state.clear()
        await show_playerok_autoreply(message, message.from_user.id)

    @router.message(PlayerokAutoReplyState.cooldown, F.text)
    async def playerok_autoreply_cooldown_save(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not value.isdigit() or not 0 <= int(value) <= 1440:
            await message.answer("Введите целое число от 0 до 1440.")
            return
        await db.set_integer_setting(message.from_user.id, "playerok_autoreply_cooldown_minutes", int(value))
        await state.clear()
        await show_playerok_autoreply(message, message.from_user.id)

    @router.callback_query(F.data == "po_ar_hours")
    async def playerok_autoreply_hours(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokAutoReplyState.hours)
        await callback.message.answer("Введите рабочее время в формате <code>9-22</code> или <code>0-24</code>.")

    @router.message(PlayerokAutoReplyState.hours, F.text)
    async def playerok_autoreply_hours_save(message: Message, state: FSMContext) -> None:
        match = re.fullmatch(r"\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*", message.text)
        if not match:
            await message.answer("Используйте формат 9-22 или 0-24.")
            return
        start, end = map(int, match.groups())
        if not 0 <= start <= 23 or not 1 <= end <= 24 or start == end:
            await message.answer("Начало: 0–23, окончание: 1–24; значения не должны совпадать.")
            return
        await db.set_integer_setting(message.from_user.id, "playerok_autoreply_work_start", start)
        await db.set_integer_setting(message.from_user.id, "playerok_autoreply_work_end", end)
        await state.clear()
        await show_playerok_autoreply(message, message.from_user.id)

    async def show_playerok_commands(target: Message, user_id: int) -> None:
        items = await db.list_playerok_command_replies(user_id)
        rows = [[(
            f"{'✅' if item['enabled'] else '❌'} {clipped(item['trigger'], 35)}",
            f"po_command:{item['id']}",
        )] for item in items[:50]]
        rows.extend([[("➕ Добавить", "po_command_add")], [("⬅️ Автоответчик", "po_autoreply")]])
        await target.answer(
            "⌨️ <b>Команды Playerok</b>\n\n"
            "Ответ отправляется, когда сообщение покупателя полностью совпадает с командой без учёта регистра.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "po_commands")
    async def playerok_commands(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_commands(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_command_add")
    async def playerok_command_add(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokCommandState.trigger)
        await callback.message.answer("Отправьте команду до 100 символов, например <code>#status</code>.")

    @router.message(PlayerokCommandState.trigger, F.text)
    async def playerok_command_trigger(message: Message, state: FSMContext) -> None:
        trigger = message.text.casefold().strip()
        if not 1 <= len(trigger) <= 100 or "\n" in trigger:
            await message.answer("Команда должна быть одной строкой от 1 до 100 символов.")
            return
        await state.update_data(playerok_command_trigger=trigger)
        await state.set_state(PlayerokCommandState.response)
        await message.answer("Теперь отправьте ответ до 3000 символов.")

    @router.message(PlayerokCommandState.response, F.text)
    async def playerok_command_response(message: Message, state: FSMContext) -> None:
        response = message.text.strip()
        trigger = (await state.get_data()).get("playerok_command_trigger")
        if not trigger or not 1 <= len(response) <= 3000:
            await message.answer("Ответ должен быть от 1 до 3000 символов.")
            return
        item = await db.save_playerok_command_reply(message.from_user.id, trigger, response)
        await state.clear()
        await show_playerok_command(message, message.from_user.id, int(item["id"]))

    async def show_playerok_command(target: Message, user_id: int, reply_id: int) -> None:
        item = await db.get_playerok_command_reply(user_id, reply_id)
        if not item:
            await target.answer("Команда не найдена.")
            return
        await target.answer(
            f"⌨️ <b>{html.escape(item['trigger'])}</b>\n\n"
            f"Состояние: {bool_icon(item['enabled'])}\n\n<pre>{html.escape(clipped(item['response'], 2200))}</pre>",
            reply_markup=keyboard([
                [("Выключить" if item["enabled"] else "Включить", f"po_command_toggle:{reply_id}")],
                [("🗑 Удалить", f"po_command_delete_ask:{reply_id}")],
                [("⬅️ Команды", "po_commands")],
            ]),
        )

    @router.callback_query(F.data.startswith("po_command:"))
    async def playerok_command(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_command(callback.message, callback.from_user.id, int(callback.data.split(":", 1)[1]))

    @router.callback_query(F.data.startswith("po_command_toggle:"))
    async def playerok_command_toggle(callback: CallbackQuery) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        await db.toggle_playerok_command_reply(callback.from_user.id, reply_id)
        await callback.answer("Сохранено")
        await show_playerok_command(callback.message, callback.from_user.id, reply_id)

    @router.callback_query(F.data.startswith("po_command_delete_ask:"))
    async def playerok_command_delete_ask(callback: CallbackQuery) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer(
            "Удалить команду без возможности восстановления?",
            reply_markup=keyboard([[("Да, удалить", f"po_command_delete:{reply_id}"), ("Отмена", f"po_command:{reply_id}")]]),
        )

    @router.callback_query(F.data.startswith("po_command_delete:"))
    async def playerok_command_delete(callback: CallbackQuery) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        await db.delete_playerok_command_reply(callback.from_user.id, reply_id)
        await callback.answer("Команда удалена")
        await show_playerok_commands(callback.message, callback.from_user.id)

    async def show_playerok_delivery(target: Message, user_id: int) -> None:
        runtime = await require_playerok_runtime(target, user_id)
        if not runtime:
            return
        row = await db.get_user(user_id)
        rules = await db.list_playerok_delivery_rules(user_id)
        rows = [[(
            f"{'✅' if rule['enabled'] else '❌'} {clipped(rule['item_title'], 28)} · {len(rule['products'])}",
            f"po_delivery_rule:{rule['id']}",
        )] for rule in rules[:30]]
        rows.extend([
            [("➕ Добавить из объявлений", "po_delivery_add")],
            [(f"{bool_icon(row['playerok_auto_delivery_enabled'])} Автовыдача", "po_toggle:playerok_auto_delivery_enabled")],
            [(f"{bool_icon(row['playerok_auto_confirm_enabled'])} Отмечать выполненным", "po_toggle:playerok_auto_confirm_enabled")],
            [(f"{bool_icon(row['playerok_notify_delivery'])} Уведомлять", "po_toggle:playerok_notify_delivery")],
            [("⬅️ Меню", "menu")],
        ])
        await target.answer(
            "📤 <b>Автовыдача Playerok</b>\n\n"
            "Правило привязано к одному объявлению. В шаблоне используйте <code>$product</code>, "
            "чтобы выдать одну строку запаса. Включение «Отмечать выполненным» переводит сделку в "
            "<code>SENT</code> только после успешной выдачи.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "po_delivery")
    async def playerok_delivery(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_delivery(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_delivery_add")
    async def playerok_delivery_add(callback: CallbackQuery) -> None:
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime or PlayerokItemStatuses is None:
            return
        await callback.answer("Получаю объявления…")
        try:
            page = await asyncio.to_thread(
                runtime.account.get_my_items, statuses=list(PlayerokItemStatuses), count=24
            )
            items = list(getattr(page, "items", []) or [])
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI may fail at the request/parser layer.
            await callback.message.answer(f"❌ Не удалось загрузить объявления: {html.escape(clipped(exc, 400))}")
            return
        rows = [[(clipped(item.name, 38), f"po_delivery_pick:{item.id}")] for item in items]
        rows.append([("⬅️ Автовыдача", "po_delivery")])
        await callback.message.answer("Выберите объявление для автовыдачи:", reply_markup=keyboard(rows))

    @router.callback_query(F.data.startswith("po_delivery_pick:"))
    async def playerok_delivery_pick(callback: CallbackQuery, state: FSMContext) -> None:
        item_id = callback.data.split(":", 1)[1]
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            item = await asyncio.to_thread(runtime.account.get_item, id=item_id)
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous errors.
            await callback.answer(f"Не удалось: {clipped(exc, 120)}", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokDeliveryState.response)
        await state.update_data(playerok_delivery_item_id=item_id, playerok_delivery_item_title=item.name)
        await callback.message.answer(
            f"Объявление: <b>{html.escape(clipped(item.name, 800))}</b>\n\n"
            "Отправьте шаблон выдачи до 3000 символов. Используйте <code>$product</code> для строк из запаса."
        )

    @router.message(PlayerokDeliveryState.response, F.text)
    async def playerok_delivery_response(message: Message, state: FSMContext) -> None:
        response = message.text.strip()
        data = await state.get_data()
        if not 1 <= len(response) <= 3000:
            await message.answer("Шаблон должен быть от 1 до 3000 символов.")
            return
        rule_id = data.get("playerok_delivery_rule_id")
        if rule_id:
            rule = await db.get_playerok_delivery_rule(message.from_user.id, int(rule_id))
            if not rule:
                await state.clear()
                await message.answer("Правило не найдено.")
                return
            saved = await db.save_playerok_delivery_rule(message.from_user.id, rule["item_id"], rule["item_title"], response)
        else:
            item_id = data.get("playerok_delivery_item_id")
            title = data.get("playerok_delivery_item_title")
            if not item_id or not title:
                await state.clear()
                await message.answer("Сессия настройки истекла.")
                return
            saved = await db.save_playerok_delivery_rule(message.from_user.id, item_id, title, response)
        await state.clear()
        await show_playerok_delivery_rule(message, message.from_user.id, int(saved["id"]))

    async def show_playerok_delivery_rule(target: Message, user_id: int, rule_id: int) -> None:
        rule = await db.get_playerok_delivery_rule(user_id, rule_id)
        if not rule:
            await target.answer("Правило не найдено.")
            return
        stock = list(rule["products"] or [])
        await target.answer(
            f"📦 <b>{html.escape(clipped(rule['item_title'], 900))}</b>\n\n"
            f"ID объявления: <code>{html.escape(rule['item_id'])}</code>\n"
            f"Состояние: {bool_icon(rule['enabled'])} · запас: <b>{len(stock)}</b>\n"
            f"Шаблон:\n<pre>{html.escape(clipped(rule['response'], 1600))}</pre>"
            + (f"\nПервые товары:\n<pre>{html.escape(chr(10).join(stock[:5]))}</pre>" if stock else ""),
            reply_markup=keyboard([
                [("➕ Добавить товары", f"po_delivery_stock:{rule_id}"), ("✏️ Шаблон", f"po_delivery_edit:{rule_id}")],
                [("🧹 Очистить запас", f"po_delivery_clear_ask:{rule_id}")],
                [("Выключить" if rule["enabled"] else "Включить", f"po_delivery_toggle:{rule_id}")],
                [("🗑 Удалить", f"po_delivery_delete_ask:{rule_id}")],
                [("⬅️ Автовыдача", "po_delivery")],
            ]),
        )

    @router.callback_query(F.data.startswith("po_delivery_rule:"))
    async def playerok_delivery_rule(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_delivery_rule(callback.message, callback.from_user.id, int(callback.data.split(":", 1)[1]))

    @router.callback_query(F.data.startswith("po_delivery_stock:"))
    async def playerok_delivery_stock(callback: CallbackQuery, state: FSMContext) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        if not await db.get_playerok_delivery_rule(callback.from_user.id, rule_id):
            await callback.answer("Правило не найдено", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokDeliveryState.products)
        await state.update_data(playerok_delivery_rule_id=rule_id)
        await callback.message.answer("Отправьте до 500 товаров: один ключ или товар на строку.")

    @router.message(PlayerokDeliveryState.products, F.text)
    async def playerok_delivery_stock_save(message: Message, state: FSMContext) -> None:
        products = [line.strip() for line in message.text.splitlines() if line.strip()]
        if not products or len(products) > 500 or any(len(product) > 1000 for product in products):
            await message.answer("Нужно от 1 до 500 непустых строк до 1000 символов каждая.")
            return
        rule_id = int((await state.get_data()).get("playerok_delivery_rule_id") or 0)
        await db.add_playerok_delivery_products(message.from_user.id, rule_id, products)
        await state.clear()
        await show_playerok_delivery_rule(message, message.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("po_delivery_edit:"))
    async def playerok_delivery_edit(callback: CallbackQuery, state: FSMContext) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        rule = await db.get_playerok_delivery_rule(callback.from_user.id, rule_id)
        if not rule:
            await callback.answer("Правило не найдено", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokDeliveryState.response)
        await state.update_data(playerok_delivery_rule_id=rule_id)
        await callback.message.answer(f"Отправьте новый шаблон. Сейчас:\n<pre>{html.escape(clipped(rule['response'], 1600))}</pre>")

    @router.callback_query(F.data.startswith("po_delivery_toggle:"))
    async def playerok_delivery_toggle(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.toggle_playerok_delivery_rule(callback.from_user.id, rule_id)
        await callback.answer("Сохранено")
        await show_playerok_delivery_rule(callback.message, callback.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("po_delivery_clear_ask:"))
    async def playerok_delivery_clear_ask(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer("Очистить весь запас?", reply_markup=keyboard([[("Да, очистить", f"po_delivery_clear:{rule_id}"), ("Отмена", f"po_delivery_rule:{rule_id}")]]))

    @router.callback_query(F.data.startswith("po_delivery_clear:"))
    async def playerok_delivery_clear(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.clear_playerok_delivery_products(callback.from_user.id, rule_id)
        await callback.answer("Запас очищен")
        await show_playerok_delivery_rule(callback.message, callback.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("po_delivery_delete_ask:"))
    async def playerok_delivery_delete_ask(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer("Удалить правило и запас?", reply_markup=keyboard([[("Да, удалить", f"po_delivery_delete:{rule_id}"), ("Отмена", f"po_delivery_rule:{rule_id}")]]))

    @router.callback_query(F.data.startswith("po_delivery_delete:"))
    async def playerok_delivery_delete(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.delete_playerok_delivery_rule(callback.from_user.id, rule_id)
        await callback.answer("Правило удалено")
        await show_playerok_delivery(callback.message, callback.from_user.id)

    async def show_playerok_item_options(target: Message, state: FSMContext, runtime: PlayerokRuntime) -> None:
        data = await state.get_data()
        category = await asyncio.to_thread(runtime.account.get_game_category, id=data["playerok_category_id"])
        options = list(getattr(category, "options", []) or [])[:40]
        selected = set(data.get("playerok_option_ids", []))
        rows = [[(
            f"{'✅' if str(option.id) in selected else '⬜'} {clipped(getattr(option, 'label', option.value), 30)}",
            f"po_new_option:{option.id}",
        )] for option in options]
        rows.append([("Продолжить", "po_new_options_done")])
        await target.answer(
            "Выберите нужные опции категории. Если для объявления опции не требуются — сразу нажмите «Продолжить». "
            "Playerok проверит обязательные ограничения при создании черновика.",
            reply_markup=keyboard(rows),
        )

    async def ask_playerok_item_field(target: Message, state: FSMContext) -> None:
        data = await state.get_data()
        fields = data.get("playerok_item_fields", [])
        index = int(data.get("playerok_item_field_index", 0))
        if index >= len(fields):
            await target.answer(
                "🧾 <b>Проверьте черновик объявления</b>\n\n"
                f"Название: <b>{html.escape(data['playerok_item_name'])}</b>\n"
                f"Цена: <b>{data['playerok_item_price']} ₽</b>\n"
                f"Описание: <pre>{html.escape(clipped(data['playerok_item_description'], 1200))}</pre>\n"
                f"Опций: <b>{len(data.get('playerok_option_ids', []))}</b> · полей: <b>{len(fields)}</b>\n\n"
                "После подтверждения будет создан черновик. Публикация остаётся отдельным действием.",
                reply_markup=keyboard([[("✅ Создать черновик", "po_new_create"), ("Отмена", "po_items")]]),
            )
            return
        field = fields[index]
        await state.set_state(PlayerokItemCreateState.field_value)
        await target.answer(
            f"Поле <b>{html.escape(field['label'])}</b> ({index + 1}/{len(fields)}).\n"
            "Отправьте значение до 1000 символов. Для отмены: /cancel"
        )

    @router.callback_query(F.data == "po_item_create")
    async def playerok_item_create(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_playerok_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.set_state(PlayerokItemCreateState.game_search)
        await callback.message.answer("Введите название игры или приложения Playerok для поиска.")

    @router.message(PlayerokItemCreateState.game_search, F.text)
    async def playerok_item_game_search(message: Message, state: FSMContext) -> None:
        query = message.text.strip()
        if not 2 <= len(query) <= 100:
            await message.answer("Введите от 2 до 100 символов названия.")
            return
        runtime = await require_playerok_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        try:
            page = await asyncio.to_thread(runtime.account.get_games, name=query, count=24)
            games = list(getattr(page, "games", []) or [])
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI may fail at the request/parser layer.
            await message.answer(f"❌ Не удалось найти игру: {html.escape(clipped(exc, 400))}")
            return
        if not games:
            await message.answer("Ничего не найдено. Попробуйте другое название.")
            return
        await message.answer(
            "Выберите игру или приложение:",
            reply_markup=keyboard([[(clipped(game.name, 48), f"po_new_game:{game.id}")] for game in games]),
        )

    @router.callback_query(F.data.startswith("po_new_game:"))
    async def playerok_item_game_pick(callback: CallbackQuery, state: FSMContext) -> None:
        game_id = callback.data.split(":", 1)[1]
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            game = await asyncio.to_thread(runtime.account.get_game, id=game_id)
            categories = list(getattr(game, "categories", []) or [])
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous errors.
            await callback.answer(f"Не удалось: {clipped(exc, 120)}", show_alert=True)
            return
        if not categories:
            await callback.answer("У игры нет доступных категорий", show_alert=True)
            return
        await callback.answer()
        await state.update_data(playerok_game_id=game_id)
        await callback.message.answer(
            "Выберите категорию:",
            reply_markup=keyboard([[(clipped(category.name, 48), f"po_new_category:{category.id}")] for category in categories[:40]]),
        )

    @router.callback_query(F.data.startswith("po_new_category:"))
    async def playerok_item_category_pick(callback: CallbackQuery, state: FSMContext) -> None:
        category_id = callback.data.split(":", 1)[1]
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            page = await asyncio.to_thread(runtime.account.get_game_category_obtaining_types, category_id)
            obtaining_types = list(getattr(page, "obtaining_types", []) or [])
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI exposes heterogeneous errors.
            await callback.answer(f"Не удалось: {clipped(exc, 120)}", show_alert=True)
            return
        if not obtaining_types:
            await callback.answer("Для категории нет способов получения", show_alert=True)
            return
        await callback.answer()
        await state.update_data(playerok_category_id=category_id)
        await callback.message.answer(
            "Выберите способ получения:",
            reply_markup=keyboard([[(clipped(item.name, 48), f"po_new_obtain:{item.id}")] for item in obtaining_types[:40]]),
        )

    @router.callback_query(F.data.startswith("po_new_obtain:"))
    async def playerok_item_obtaining_pick(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.update_data(playerok_obtaining_type_id=callback.data.split(":", 1)[1], playerok_option_ids=[])
        await state.set_state(PlayerokItemCreateState.name)
        await callback.message.answer("Введите название объявления: от 3 до 120 символов.")

    @router.message(PlayerokItemCreateState.name, F.text)
    async def playerok_item_name(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not 3 <= len(value) <= 120:
            await message.answer("Название должно быть от 3 до 120 символов.")
            return
        await state.update_data(playerok_item_name=value)
        await state.set_state(PlayerokItemCreateState.price)
        await message.answer("Введите цену в рублях: целое число от 1 до 10 000 000.")

    @router.message(PlayerokItemCreateState.price, F.text)
    async def playerok_item_price(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not value.isdigit() or not 1 <= int(value) <= 10_000_000:
            await message.answer("Введите целую цену от 1 до 10 000 000.")
            return
        await state.update_data(playerok_item_price=int(value))
        await state.set_state(PlayerokItemCreateState.description)
        await message.answer("Введите описание объявления: от 1 до 3000 символов.")

    @router.message(PlayerokItemCreateState.description, F.text)
    async def playerok_item_description(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not 1 <= len(value) <= 3000:
            await message.answer("Описание должно быть от 1 до 3000 символов.")
            return
        runtime = await require_playerok_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        await state.update_data(playerok_item_description=value)
        try:
            await show_playerok_item_options(message, state, runtime)
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI may fail at the request/parser layer.
            await message.answer(f"❌ Не удалось загрузить опции: {html.escape(clipped(exc, 400))}")

    @router.callback_query(F.data.startswith("po_new_option:"))
    async def playerok_item_option_toggle(callback: CallbackQuery, state: FSMContext) -> None:
        option_id = callback.data.split(":", 1)[1]
        data = await state.get_data()
        selected = set(data.get("playerok_option_ids", []))
        if option_id in selected:
            selected.remove(option_id)
        else:
            selected.add(option_id)
        await state.update_data(playerok_option_ids=list(selected))
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        await callback.answer("Выбрано" if option_id in selected else "Снято")
        await show_playerok_item_options(callback.message, state, runtime)

    @router.callback_query(F.data == "po_new_options_done")
    async def playerok_item_options_done(callback: CallbackQuery, state: FSMContext) -> None:
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        data = await state.get_data()
        try:
            page = await asyncio.to_thread(
                runtime.account.get_game_category_data_fields,
                data["playerok_category_id"],
                data["playerok_obtaining_type_id"],
            )
            fields = [
                {"id": str(field.id), "label": str(field.label)}
                for field in list(getattr(page, "data_fields", []) or [])
                if getattr(field, "required", False)
                and getattr(getattr(field, "type", None), "name", "") == "ITEM_DATA"
            ]
        except Exception as exc:  # noqa: BLE001 - PlayerokAPI may fail at the request/parser layer.
            await callback.answer(f"Не удалось: {clipped(exc, 120)}", show_alert=True)
            return
        await callback.answer()
        await state.update_data(playerok_item_fields=fields, playerok_item_field_index=0, playerok_item_field_values={})
        await ask_playerok_item_field(callback.message, state)

    @router.message(PlayerokItemCreateState.field_value, F.text)
    async def playerok_item_field_value(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not 1 <= len(value) <= 1000:
            await message.answer("Введите значение от 1 до 1000 символов.")
            return
        data = await state.get_data()
        index = int(data.get("playerok_item_field_index", 0))
        fields = data.get("playerok_item_fields", [])
        if index >= len(fields):
            await state.clear()
            return
        values = dict(data.get("playerok_item_field_values", {}))
        values[fields[index]["id"]] = value
        await state.update_data(playerok_item_field_values=values, playerok_item_field_index=index + 1)
        await ask_playerok_item_field(message, state)

    @router.callback_query(F.data == "po_new_create")
    async def playerok_item_create_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        data = await state.get_data()
        required = {"playerok_category_id", "playerok_obtaining_type_id", "playerok_item_name", "playerok_item_price", "playerok_item_description"}
        if not required <= set(data):
            await callback.answer("Сессия создания истекла", show_alert=True)
            await state.clear()
            return
        await callback.answer("Создаю черновик…")
        try:
            category = await asyncio.to_thread(runtime.account.get_game_category, id=data["playerok_category_id"])
            fields_page = await asyncio.to_thread(
                runtime.account.get_game_category_data_fields,
                data["playerok_category_id"],
                data["playerok_obtaining_type_id"],
            )
            values = data.get("playerok_item_field_values", {})
            fields = []
            for field in list(getattr(fields_page, "data_fields", []) or []):
                if str(field.id) in values:
                    field.value = values[str(field.id)]
                    fields.append(field)
            selected = set(data.get("playerok_option_ids", []))
            options = [option for option in list(getattr(category, "options", []) or []) if str(option.id) in selected]
            item = await asyncio.to_thread(
                runtime.account.create_item,
                data["playerok_category_id"],
                data["playerok_obtaining_type_id"],
                data["playerok_item_name"],
                data["playerok_item_price"],
                data["playerok_item_description"],
                options,
                fields,
            )
        except Exception as exc:
            logger.exception("Playerok: не создан черновик")
            await callback.message.answer(
                "❌ Playerok не создал черновик. Обычно не хватает обязательной опции или поля категории.\n\n"
                f"<code>{html.escape(clipped(exc, 700))}</code>"
            )
            return
        await state.clear()
        await callback.message.answer(
            f"✅ Создан черновик <b>{html.escape(clipped(getattr(item, 'name', data['playerok_item_name']), 700))}</b>\n"
            f"ID: <code>{html.escape(str(item.id))}</code>",
            reply_markup=keyboard([[("📢 Объявления", "po_items"), ("➕ Создать ещё", "po_item_create")]]),
        )

    async def show_playerok_items(target: Message, user_id: int) -> None:
        runtime = await require_playerok_runtime(target, user_id)
        if not runtime or PlayerokItemStatuses is None:
            return
        row = await db.get_user(user_id)
        try:
            page = await asyncio.to_thread(
                runtime.account.get_my_items,
                statuses=list(PlayerokItemStatuses),
                count=24,
            )
        except Exception as exc:
            logger.exception("Не удалось получить объявления Playerok")
            await target.answer(f"❌ Объявления недоступны: {html.escape(clipped(exc, 400))}")
            return
        items = list(getattr(page, "items", []) or [])
        statuses = Counter(
            getattr(getattr(item, "status", None), "name", "UNKNOWN") for item in items
        )
        preview = "\n".join(
            f"• <b>{html.escape(clipped(item.name, 55))}</b> — {format_money(item.price)} ₽ "
            f"(<code>{html.escape(getattr(item.status, 'name', '—'))}</code>)"
            for item in items[:10]
        ) or "Объявлений пока нет."
        await target.answer(
            "📢 <b>Объявления Playerok</b>\n\n"
            f"Найдено на первой странице: <b>{len(items)}</b> из <b>{getattr(page, 'total_count', len(items))}</b>\n"
            f"Черновиков: <b>{statuses.get('DRAFT', 0)}</b> · активных: <b>{statuses.get('APPROVED', 0)}</b>\n\n"
            f"{preview}\n\n"
            "Автовыставление каждые 5 минут публикует до 24 черновиков с бесплатным приоритетом. "
            "Создавать универсальные объявления автоматически нельзя: обязательные поля различаются по категориям.",
            reply_markup=keyboard([
                [(f"{bool_icon(row['playerok_auto_publish_enabled'])} Автовыставление", "po_auto_publish_toggle")],
                [("📤 Выставить черновики сейчас", "po_publish_drafts")],
                [("🔄 Обновить", "po_items"), ("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "po_items")
    async def playerok_items(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю…")
        await show_playerok_items(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_auto_publish_toggle")
    async def playerok_auto_publish_toggle(callback: CallbackQuery) -> None:
        row = await db.get_user(callback.from_user.id)
        if not row or not row["playerok_active"]:
            await callback.answer("Playerok не подключён", show_alert=True)
            return
        enabled = not row["playerok_auto_publish_enabled"]
        await db.set_flag(callback.from_user.id, "playerok_auto_publish_enabled", enabled)
        runtime = manager.get_playerok(callback.from_user.id)
        if runtime and enabled:
            runtime.next_auto_publish_at = 0
        await callback.answer("Автовыставление включено" if enabled else "Автовыставление выключено")
        await show_playerok_items(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_publish_drafts")
    async def playerok_publish_drafts(callback: CallbackQuery) -> None:
        await callback.answer("Публикую…")
        try:
            published, total, errors = await manager.publish_playerok_drafts(callback.from_user.id)
        except Exception as exc:  # noqa: BLE001 - API raises heterogeneous network errors.
            await callback.message.answer(f"❌ Публикация не выполнена: {html.escape(clipped(exc, 500))}")
            return
        error_text = "\n".join(f"• {html.escape(error)}" for error in errors[:8])
        await callback.message.answer(
            f"✅ Опубликовано черновиков: <b>{published}/{total}</b>"
            + (f"\n\nОшибки:\n{error_text}" if errors else ""),
            reply_markup=keyboard([[('⬅️ Объявления', 'po_items')]]),
        )

    async def show_playerok_notifications(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        await target.answer(
            "🔵 <b>Уведомления Playerok</b>\n\n"
            "Проверка выполняется каждые 20 секунд; все сообщения помечаются площадкой.",
            reply_markup=keyboard([
                [(f"{bool_icon(row['playerok_notify_messages'])} Сообщения", "po_toggle:playerok_notify_messages")],
                [(f"{bool_icon(row['playerok_notify_deals'])} Сделки и статусы", "po_toggle:playerok_notify_deals")],
                [(f"{bool_icon(row['playerok_notify_reviews'])} Отзывы", "po_toggle:playerok_notify_reviews")],
                [(f"{bool_icon(row['playerok_notify_delivery'])} Автовыдача", "po_toggle:playerok_notify_delivery")],
                [(f"{bool_icon(row['playerok_notify_system'])} Ошибки подключения", "po_toggle:playerok_notify_system")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "po_notifications")
    async def playerok_notifications(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_notifications(callback.message, callback.from_user.id)

    @router.callback_query(F.data.startswith("po_toggle:"))
    async def playerok_toggle(callback: CallbackQuery) -> None:
        column = callback.data.split(":", 1)[1]
        allowed = {
            "playerok_notify_messages",
            "playerok_notify_deals",
            "playerok_notify_reviews",
            "playerok_notify_system",
            "playerok_notify_delivery",
            "playerok_autoreply_enabled",
            "playerok_auto_delivery_enabled",
            "playerok_auto_confirm_enabled",
        }
        row = await db.get_user(callback.from_user.id)
        if not row or column not in allowed:
            await callback.answer("Неизвестная настройка", show_alert=True)
            return
        await db.set_flag(callback.from_user.id, column, not row[column])
        await callback.answer("Сохранено")
        if column == "playerok_autoreply_enabled":
            await show_playerok_autoreply(callback.message, callback.from_user.id)
        elif column in {"playerok_auto_delivery_enabled", "playerok_auto_confirm_enabled"}:
            await show_playerok_delivery(callback.message, callback.from_user.id)
        else:
            await show_playerok_notifications(callback.message, callback.from_user.id)

    async def show_playerok_account(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        if not row or not row["playerok_active"]:
            await target.answer(
                "Playerok не подключён.",
                reply_markup=keyboard([[("🔵 Подключить", "connect_playerok")]]),
            )
            return
        try:
            proxy = proxy_label(secrets.decrypt(row["playerok_proxy_enc"]))
        except (InvalidToken, ValueError, TypeError):
            proxy = "не удалось расшифровать"
        status = "🟢 работает" if manager.get_playerok(user_id) else "🔴 остановлен"
        await target.answer(
            "⚙️ <b>Аккаунт Playerok</b>\n\n"
            f"Пользователь: <b>{html.escape(row['playerok_username'] or '—')}</b>\n"
            f"ID: <code>{html.escape(str(row['playerok_id'] or '—'))}</code>\n"
            f"Прокси: <code>{html.escape(proxy)}</code>\n"
            f"Слежение: {status}",
            reply_markup=keyboard([
                [("🔄 Переподключить", "po_reconnect"), ("🍪 Изменить cookie", "connect_playerok")],
                [("🗑 Отключить Playerok", "po_disconnect_ask")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "po_account")
    async def playerok_account(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_account(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_reconnect")
    async def playerok_reconnect(callback: CallbackQuery) -> None:
        await callback.answer("Переподключаю…")
        try:
            await manager.start_playerok(callback.from_user.id)
        except Exception as exc:
            logger.exception("Переподключение Playerok не удалось")
            await callback.message.answer(f"❌ Не удалось: {html.escape(clipped(exc, 500))}")
            return
        await show_playerok_account(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "po_disconnect_ask")
    async def playerok_disconnect_ask(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "Удалить сохранённые cookie и прокси Playerok и остановить уведомления?",
            reply_markup=keyboard([[("Да, отключить", "po_disconnect"), ("Нет", "po_account")]]),
        )

    @router.callback_query(F.data == "po_disconnect")
    async def playerok_disconnect(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await manager.stop_playerok(callback.from_user.id)
        await db.disconnect_playerok_account(callback.from_user.id)
        await state.clear()
        await callback.message.answer("Playerok отключён, cookie и прокси удалены.")
        await show_main(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "menu")
    async def menu_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_main(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "profile")
    async def profile(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю…")
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            await asyncio.to_thread(runtime.account.get, True)
            profile_obj = await asyncio.to_thread(runtime.account.get_user, runtime.account.id)
            lots = profile_obj.get_lots()
        except Exception:
            logger.exception("Не удалось получить профиль")
            await callback.message.answer("❌ Не удалось загрузить профиль FunPay.")
            return
        common_lots = [lot for lot in lots if lot.subcategory.type is types.SubCategoryTypes.COMMON]
        currency_lots = [lot for lot in lots if lot.subcategory.type is types.SubCategoryTypes.CURRENCY]
        auto_delivery = sum(bool(lot.auto) for lot in lots)
        subcategories = {lot.subcategory.id for lot in lots}
        categories = {lot.subcategory.category.id for lot in lots}
        buttons = [
            [InlineKeyboardButton(text="📊 Статистика", callback_data="statistics")],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="profile"),
                InlineKeyboardButton(
                    text="🌐 Профиль FunPay",
                    url=f"https://funpay.com/users/{profile_obj.id}/",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")],
        ]
        if profile_obj.profile_photo:
            buttons.insert(
                2,
                [
                    InlineKeyboardButton(
                        text="🖼 Аватар профиля", url=profile_obj.profile_photo
                    )
                ],
            )
        await callback.message.answer(
            "👤 <b>Подробный профиль</b>\n"
            f"Ник: <b>{html.escape(profile_obj.username)}</b>\n"
            f"ID: <code>{profile_obj.id}</code>\n"
            f"Онлайн: {bool_icon(profile_obj.online)}\n"
            f"Заблокирован: {'да' if profile_obj.banned else 'нет'}\n"
            f"Активных продаж: <b>{runtime.account.active_sales}</b>\n"
            f"Активных покупок: <b>{runtime.account.active_purchases}</b>\n"
            f"Опубликовано лотов: <b>{len(lots)}</b>\n"
            f"　Обычных: <b>{len(common_lots)}</b>\n"
            f"　Валютных: <b>{len(currency_lots)}</b>\n"
            f"　С автовыдачей FunPay: <b>{auto_delivery}</b>\n"
            f"Разделов: <b>{len(subcategories)}</b> · игр: <b>{len(categories)}</b>\n"
            f"Runner: {'🟢 работает' if manager.get(callback.from_user.id) else '🔴 остановлен'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    @router.callback_query(F.data == "statistics")
    async def statistics(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "📊 <b>Период статистики продаж</b>\n"
            "Выручка считается только по закрытым заказам.",
            reply_markup=keyboard([
                [("24 часа", "stats:1"), ("7 дней", "stats:7"), ("30 дней", "stats:30")],
                [("90 дней", "stats:90"), ("Год", "stats:365"), ("Всё время", "stats:all")],
                [("⬅️ Профиль", "profile")],
            ]),
        )

    @router.callback_query(F.data.startswith("stats:"))
    async def statistics_period(callback: CallbackQuery) -> None:
        await callback.answer("Собираю заказы…")
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        raw_period = callback.data.split(":", 1)[1]
        days = None if raw_period == "all" else int(raw_period)
        try:
            stats = await asyncio.to_thread(load_sales_stats, runtime.account, days)
        except Exception:
            logger.exception("Не удалось собрать статистику продаж")
            await callback.message.answer("❌ FunPay не отдал статистику продаж.")
            return
        await callback.message.answer(
            format_sales_stats(stats),
            reply_markup=keyboard([
                [("📅 Другой период", "statistics"), ("⬅️ Профиль", "profile")],
            ]),
        )

    @router.callback_query(F.data == "balance")
    async def balance(callback: CallbackQuery) -> None:
        await callback.answer("Проверяю…")
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            await asyncio.to_thread(runtime.account.get, True)
            detailed = await asyncio.to_thread(load_detailed_balance, runtime.account)
        except Exception:
            logger.exception("Не удалось обновить баланс")
            await callback.message.answer("❌ Не удалось проверить баланс FunPay.")
            return
        await callback.message.answer(
            "💰 <b>Баланс FunPay</b>\n\n"
            f"🇷🇺 Всего: <b>{format_money(detailed.total_rub)} ₽</b>\n"
            f"　Можно вывести: <b>{format_money(detailed.available_rub)} ₽</b>\n"
            f"　Ожидает разблокировки: {format_money(detailed.total_rub - detailed.available_rub)} ₽\n\n"
            f"🇺🇸 Всего: <b>{format_money(detailed.total_usd)} $</b>\n"
            f"　Можно вывести: <b>{format_money(detailed.available_usd)} $</b>\n"
            f"　Ожидает разблокировки: {format_money(detailed.total_usd - detailed.available_usd)} $\n\n"
            f"🇪🇺 Всего: <b>{format_money(detailed.total_eur)} €</b>\n"
            f"　Можно вывести: <b>{format_money(detailed.available_eur)} €</b>\n"
            f"　Ожидает разблокировки: {format_money(detailed.total_eur - detailed.available_eur)} €\n\n"
            f"Активных продаж: {runtime.account.active_sales}\n"
            f"Активных покупок: {runtime.account.active_purchases}",
            reply_markup=keyboard([[("🔄 Обновить", "balance"), ("⬅️ Меню", "menu")]]),
        )

    async def show_notifications(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        await target.answer(
            "🔔 <b>Настройки уведомлений</b>",
            reply_markup=keyboard([
                [(f"{bool_icon(row['notify_messages'])} Сообщения", "toggle:notify_messages")],
                [(f"{bool_icon(row['notify_new_orders'])} Новые заказы", "toggle:notify_new_orders")],
                [(f"{bool_icon(row['notify_order_status'])} Статусы заказов", "toggle:notify_order_status")],
                [(f"{bool_icon(row['notify_reviews'])} Отзывы", "toggle:notify_reviews")],
                [(f"{bool_icon(row['notify_lots_raise'])} Поднятие лотов", "toggle:notify_lots_raise")],
                [(f"{bool_icon(row['notify_delivery'])} Автовыдача", "toggle:notify_delivery")],
                [("📡 Дополнительные чаты", "notification_targets")],
                [(f"{bool_icon(row['notify_system'])} Запуск и ошибки", "toggle:notify_system")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "notifications")
    async def notifications(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_notifications(callback.message, callback.from_user.id)

    @router.callback_query(F.data.startswith("toggle:"))
    async def toggle(callback: CallbackQuery) -> None:
        column = callback.data.split(":", 1)[1]
        row = await db.get_user(callback.from_user.id)
        allowed_columns = {
            "notify_messages",
            "notify_new_orders",
            "notify_order_status",
            "notify_reviews",
            "notify_lots_raise",
            "notify_system",
            "auto_raise_enabled",
            "keep_online_enabled",
            "autoreply_enabled",
            "autoreply_new_chats_only",
            "review_reply_enabled",
            "auto_delivery_enabled",
            "multi_delivery_enabled",
            "delivery_auto_restore",
            "delivery_auto_disable",
            "notify_delivery",
        }
        if not row or column not in allowed_columns:
            await callback.answer("Неизвестная настройка", show_alert=True)
            return
        await db.set_flag(callback.from_user.id, column, not row[column])
        await callback.answer("Сохранено")
        if column == "keep_online_enabled":
            try:
                await manager.start(callback.from_user.id)
            except Exception:
                logger.exception("Не удалось применить настройку поддержания сессии")
            await show_account(callback.message, callback.from_user.id)
        elif column == "auto_raise_enabled":
            runtime = manager.get(callback.from_user.id)
            if runtime:
                runtime.auto_raise_enabled = not row[column]
            await show_auto_raise(callback.message, callback.from_user.id)
        elif column in {"autoreply_enabled", "autoreply_new_chats_only"}:
            await show_autoreply(callback.message, callback.from_user.id)
        elif column == "review_reply_enabled":
            await show_review_replies(callback.message, callback.from_user.id)
        elif column in {
            "auto_delivery_enabled",
            "multi_delivery_enabled",
            "delivery_auto_restore",
            "delivery_auto_disable",
            "notify_delivery",
        }:
            await show_delivery(callback.message, callback.from_user.id)
        else:
            await show_notifications(callback.message, callback.from_user.id)

    async def show_notification_targets(target: Message, user_id: int) -> None:
        targets = await db.list_notification_targets(user_id)
        rows = [
            [(
                f"🗑 {clipped(item['title'], 28)} · {item['chat_id']}",
                f"notification_target_delete:{item['chat_id']}",
            )]
            for item in targets
        ]
        rows.extend([
            [("➕ Добавить чат", "notification_target_add")],
            [("⬅️ Уведомления", "notifications")],
        ])
        await target.answer(
            "📡 <b>Дополнительные чаты уведомлений</b>\n\n"
            "Бот отправляет копии всех помеченных FunPay/Playerok-уведомлений владельцу и в эти чаты. "
            "Перед добавлением включите бота в группу или канал и выдайте право отправлять сообщения.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "notification_targets")
    async def notification_targets(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_notification_targets(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "notification_target_add")
    async def notification_target_add(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(NotificationTargetState.chat_id)
        await callback.message.answer(
            "Отправьте числовой ID группы или канала, например <code>-1001234567890</code>. "
            "Бот должен уже состоять в этом чате. Для отмены: /cancel"
        )

    @router.message(NotificationTargetState.chat_id, F.text)
    async def notification_target_save(message: Message, state: FSMContext) -> None:
        try:
            chat_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Нужен числовой ID чата.")
            return
        try:
            chat = await message.bot.get_chat(chat_id)
            member = await message.bot.get_chat_member(chat_id, message.from_user.id)
            member_status = getattr(member.status, "value", str(member.status))
            if member_status not in {"administrator", "creator"}:
                await message.answer(
                    "❌ Добавлять группу или канал может только его создатель или администратор."
                )
                return
            title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or str(chat_id)
            await message.bot.send_message(
                chat_id,
                "✅ Этот чат подключён для уведомлений FunPay/Playerok.",
            )
        except Exception as exc:  # noqa: BLE001 - Telegram returns several API exception types.
            await message.answer(
                f"❌ Бот не может писать в этот чат: {html.escape(clipped(exc, 400))}"
            )
            return
        await db.save_notification_target(message.from_user.id, chat_id, clipped(title, 120))
        await state.clear()
        await message.answer("✅ Дополнительный чат сохранён.")
        await show_notification_targets(message, message.from_user.id)

    @router.callback_query(F.data.startswith("notification_target_delete:"))
    async def notification_target_delete_ask(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer(
            f"Удалить дополнительный чат <code>{chat_id}</code>?",
            reply_markup=keyboard([
                [("Да, удалить", f"notification_target_delete_do:{chat_id}")],
                [("Отмена", "notification_targets")],
            ]),
        )

    @router.callback_query(F.data.startswith("notification_target_delete_do:"))
    async def notification_target_delete(callback: CallbackQuery) -> None:
        chat_id = int(callback.data.split(":", 1)[1])
        await db.delete_notification_target(callback.from_user.id, chat_id)
        await callback.answer("Чат удалён")
        await show_notification_targets(callback.message, callback.from_user.id)

    async def show_delivery(target: Message, user_id: int) -> None:
        if not await require_runtime(target, user_id):
            return
        row = await db.get_user(user_id)
        rules = await db.list_delivery_rules(user_id)
        rows = [
            [(
                f"{'✅' if rule['enabled'] else '❌'} {clipped(rule['lot_title'], 28)} · {len(rule['products'])}",
                f"delivery_rule:{rule['id']}",
            )]
            for rule in rules[:30]
        ]
        rows.extend([
            [("➕ Добавить из лотов", "delivery_add")],
            [(f"{bool_icon(row['auto_delivery_enabled'])} Автовыдача", "toggle:auto_delivery_enabled")],
            [(f"{bool_icon(row['multi_delivery_enabled'])} Выдавать количество заказа", "toggle:multi_delivery_enabled")],
            [(f"{bool_icon(row['delivery_auto_restore'])} Автовосстановление", "toggle:delivery_auto_restore")],
            [(f"{bool_icon(row['delivery_auto_disable'])} Выключать без товара", "toggle:delivery_auto_disable")],
            [(f"{bool_icon(row['notify_delivery'])} Уведомлять о выдаче", "toggle:notify_delivery")],
            [("⬅️ Меню", "menu")],
        ])
        await target.answer(
            "📤 <b>Автовыдача Cardinal</b>\n\n"
            f"Правил: <b>{len(rules)}</b>. Число справа — остаток штучных товаров. "
            "Если в шаблоне нет <code>$product</code>, ответ считается безлимитным.\n\n"
            "Переменные заказа: $order_id, $order_title, $username, $chat_id, $date, $time. "
            "При $product одна строка запаса выдаётся за каждую купленную единицу.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "delivery")
    async def delivery(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_delivery(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "delivery_add")
    async def delivery_add(callback: CallbackQuery) -> None:
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            await callback.answer()
            return
        await callback.answer("Получаю лоты…")
        try:
            profile = await asyncio.to_thread(runtime.account.get_user, runtime.account.id)
            lots = [
                lot
                for lot in profile.get_lots()
                if lot.subcategory.type is types.SubCategoryTypes.COMMON and lot.description
            ]
        except Exception:
            logger.exception("Не загружены лоты для автовыдачи")
            await callback.message.answer("❌ FunPay не отдал список обычных лотов.")
            return
        rows = [
            [(clipped(lot.description, 35), f"delivery_pick:{lot.id}")]
            for lot in lots[:40]
        ]
        rows.append([("⬅️ Автовыдача", "delivery")])
        await callback.message.answer(
            "Выберите лот. Показаны первые 40 обычных лотов:",
            reply_markup=keyboard(rows),
        )

    async def resolve_funpay_lot(runtime: AccountRuntime, lot_id: int) -> Any | None:
        profile = await asyncio.to_thread(runtime.account.get_user, runtime.account.id)
        return next((lot for lot in profile.get_lots() if int(lot.id) == lot_id), None)

    @router.callback_query(F.data.startswith("delivery_pick:"))
    async def delivery_pick(callback: CallbackQuery, state: FSMContext) -> None:
        lot_id = int(callback.data.split(":", 1)[1])
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            await callback.answer()
            return
        try:
            lot = await resolve_funpay_lot(runtime, lot_id)
        except Exception:  # noqa: BLE001 - stale lots can fail in several FunPay API layers.
            lot = None
        if not lot or not lot.description:
            await callback.answer("Лот больше не найден", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(DeliveryRuleState.response)
        await state.update_data(delivery_lot_id=lot_id, delivery_lot_title=lot.description)
        await callback.message.answer(
            f"Лот: <b>{html.escape(clipped(lot.description, 1000))}</b>\n\n"
            "Отправьте текст выдачи до 3000 символов. Добавьте <code>$product</code> в место, "
            "куда должны подставляться строки из запаса. Для отмены: /cancel"
        )

    @router.message(DeliveryRuleState.response, F.text)
    async def delivery_response_save(message: Message, state: FSMContext) -> None:
        response = message.text.strip()
        if not 1 <= len(response) <= 3000:
            await message.answer("Текст должен содержать от 1 до 3000 символов.")
            return
        data = await state.get_data()
        existing_rule_id = data.get("delivery_rule_id")
        if existing_rule_id:
            existing = await db.get_delivery_rule(
                message.from_user.id, int(existing_rule_id)
            )
            if not existing:
                await state.clear()
                await message.answer("Правило больше не найдено.")
                return
            rule = await db.save_delivery_rule(
                message.from_user.id,
                existing["lot_id"],
                existing["lot_title"],
                response,
            )
            await state.clear()
            await message.answer("✅ Шаблон выдачи обновлён.")
            await show_delivery_rule(message, message.from_user.id, int(rule["id"]))
            return
        lot_id = data.get("delivery_lot_id")
        lot_title = data.get("delivery_lot_title")
        if not lot_id or not lot_title:
            await state.clear()
            await message.answer("Сессия настройки истекла.")
            return
        rule = await db.save_delivery_rule(message.from_user.id, lot_id, lot_title, response)
        await state.clear()
        await message.answer("✅ Правило автовыдачи сохранено.")
        await show_delivery_rule(message, message.from_user.id, int(rule["id"]))

    async def show_delivery_rule(target: Message, user_id: int, rule_id: int) -> None:
        rule = await db.get_delivery_rule(user_id, rule_id)
        if not rule:
            await target.answer("Правило не найдено.")
            return
        stock = list(rule["products"] or [])
        preview = "\n".join(f"• {html.escape(clipped(item, 100))}" for item in stock[:5])
        await target.answer(
            f"📦 <b>{html.escape(clipped(rule['lot_title'], 1000))}</b>\n\n"
            f"Лот ID: <code>{rule['lot_id']}</code>\n"
            f"Состояние: {bool_icon(rule['enabled'])}\n"
            f"Запас: <b>{len(stock)}</b>\n"
            f"Шаблон:\n<pre>{html.escape(clipped(rule['response'], 1500))}</pre>"
            + (f"\nПервые товары:\n{preview}" if preview else ""),
            reply_markup=keyboard([
                [("➕ Добавить товары", f"delivery_stock:{rule_id}")],
                [("✏️ Изменить шаблон", f"delivery_edit:{rule_id}")],
                [("🧹 Очистить запас", f"delivery_clear_ask:{rule_id}")],
                [("Выключить" if rule["enabled"] else "Включить", f"delivery_toggle:{rule_id}")],
                [(
                    f"{bool_icon(not rule['disable_auto_restore'])} Восстанавливать этот лот",
                    f"delivery_rule_restore:{rule_id}",
                )],
                [(
                    f"{bool_icon(not rule['disable_auto_disable'])} Выключать без товара",
                    f"delivery_rule_disable:{rule_id}",
                )],
                [("🗑 Удалить", f"delivery_delete_ask:{rule_id}")],
                [("⬅️ Автовыдача", "delivery")],
            ]),
        )

    @router.callback_query(F.data.startswith("delivery_rule:"))
    async def delivery_rule(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_delivery_rule(
            callback.message, callback.from_user.id, int(callback.data.split(":", 1)[1])
        )

    @router.callback_query(F.data.startswith("delivery_stock:"))
    async def delivery_stock(callback: CallbackQuery, state: FSMContext) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        if not await db.get_delivery_rule(callback.from_user.id, rule_id):
            await callback.answer("Правило не найдено", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(DeliveryRuleState.products)
        await state.update_data(delivery_rule_id=rule_id)
        await callback.message.answer(
            "Отправьте товары текстом: один товар или ключ на строку. До 500 строк за раз. "
            "Последовательность сохраняется. Для отмены: /cancel"
        )

    @router.callback_query(F.data.startswith("delivery_edit:"))
    async def delivery_edit(callback: CallbackQuery, state: FSMContext) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        rule = await db.get_delivery_rule(callback.from_user.id, rule_id)
        if not rule:
            await callback.answer("Правило не найдено", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(DeliveryRuleState.response)
        await state.update_data(delivery_rule_id=rule_id)
        await callback.message.answer(
            "Отправьте новый шаблон выдачи до 3000 символов. "
            "Для штучного товара используйте <code>$product</code>. Для отмены: /cancel\n\n"
            f"Сейчас:\n<pre>{html.escape(clipped(rule['response'], 1800))}</pre>"
        )

    @router.message(DeliveryRuleState.products, F.text)
    async def delivery_stock_save(message: Message, state: FSMContext) -> None:
        products = [line.strip() for line in message.text.splitlines() if line.strip()]
        if not products or len(products) > 500 or any(len(item) > 1000 for item in products):
            await message.answer("Нужно от 1 до 500 непустых строк, каждая не длиннее 1000 символов.")
            return
        data = await state.get_data()
        rule_id = int(data.get("delivery_rule_id") or 0)
        await db.add_delivery_products(message.from_user.id, rule_id, products)
        await state.clear()
        await message.answer(f"✅ Добавлено товаров: <b>{len(products)}</b>.")
        await show_delivery_rule(message, message.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("delivery_toggle:"))
    async def delivery_toggle(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.toggle_delivery_rule(callback.from_user.id, rule_id)
        await callback.answer("Сохранено")
        await show_delivery_rule(callback.message, callback.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("delivery_rule_restore:"))
    async def delivery_rule_restore(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.toggle_delivery_rule_option(
            callback.from_user.id, rule_id, "disable_auto_restore"
        )
        await callback.answer("Сохранено")
        await show_delivery_rule(callback.message, callback.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("delivery_rule_disable:"))
    async def delivery_rule_disable(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.toggle_delivery_rule_option(
            callback.from_user.id, rule_id, "disable_auto_disable"
        )
        await callback.answer("Сохранено")
        await show_delivery_rule(callback.message, callback.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("delivery_clear_ask:"))
    async def delivery_clear_ask(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer(
            "Удалить весь запас товаров без возможности восстановления?",
            reply_markup=keyboard([
                [("Да, очистить", f"delivery_clear:{rule_id}")],
                [("Отмена", f"delivery_rule:{rule_id}")],
            ]),
        )

    @router.callback_query(F.data.startswith("delivery_clear:"))
    async def delivery_clear(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.clear_delivery_products(callback.from_user.id, rule_id)
        await callback.answer("Запас очищен")
        await show_delivery_rule(callback.message, callback.from_user.id, rule_id)

    @router.callback_query(F.data.startswith("delivery_delete_ask:"))
    async def delivery_delete_ask(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer(
            "Удалить правило и весь его запас?",
            reply_markup=keyboard([
                [("Да, удалить", f"delivery_delete:{rule_id}")],
                [("Отмена", f"delivery_rule:{rule_id}")],
            ]),
        )

    @router.callback_query(F.data.startswith("delivery_delete:"))
    async def delivery_delete(callback: CallbackQuery) -> None:
        rule_id = int(callback.data.split(":", 1)[1])
        await db.delete_delivery_rule(callback.from_user.id, rule_id)
        await callback.answer("Правило удалено")
        await show_delivery(callback.message, callback.from_user.id)

    async def show_command_replies(target: Message, user_id: int) -> None:
        if not await require_runtime(target, user_id):
            return
        replies = await db.list_command_replies(user_id)
        rows = [
            [(
                f"{'✅' if item['enabled'] else '❌'} {clipped(item['trigger'], 35)}",
                f"command_reply:{item['id']}",
            )]
            for item in replies[:50]
        ]
        rows.extend([
            [("➕ Добавить команду", "command_reply_add")],
            [("⬅️ Меню", "menu")],
        ])
        await target.answer(
            "⌨️ <b>Ответы на команды Cardinal</b>\n\n"
            "Команда сравнивается со всем сообщением без учёта регистра и пробелов по краям. "
            "Она имеет приоритет над обычным автоответчиком.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "command_replies")
    async def command_replies(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_command_replies(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "command_reply_add")
    async def command_reply_add(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(CommandReplyState.trigger)
        await callback.message.answer(
            "Отправьте команду покупателя, например <code>!наличие</code> или <code>#help</code>. "
            "До 100 символов, одной строкой. Для отмены: /cancel"
        )

    @router.message(CommandReplyState.trigger, F.text)
    async def command_reply_trigger(message: Message, state: FSMContext) -> None:
        trigger = message.text.casefold().strip()
        if not 1 <= len(trigger) <= 100 or "\n" in trigger:
            await message.answer("Команда должна быть одной строкой от 1 до 100 символов.")
            return
        await state.update_data(command_trigger=trigger)
        await state.set_state(CommandReplyState.response)
        await message.answer(
            "Теперь отправьте ответ до 3000 символов. Можно использовать переменные автоответчика."
        )

    @router.message(CommandReplyState.response, F.text)
    async def command_reply_response(message: Message, state: FSMContext) -> None:
        response = message.text.strip()
        if not 1 <= len(response) <= 3000:
            await message.answer("Ответ должен содержать от 1 до 3000 символов.")
            return
        data = await state.get_data()
        trigger = data.get("command_trigger")
        if not trigger:
            await state.clear()
            await message.answer("Сессия настройки истекла.")
            return
        item = await db.save_command_reply(message.from_user.id, trigger, response)
        await state.clear()
        await message.answer("✅ Команда сохранена.")
        await show_command_reply(message, message.from_user.id, int(item["id"]))

    async def show_command_reply(target: Message, user_id: int, reply_id: int) -> None:
        item = await db.get_command_reply(user_id, reply_id)
        if not item:
            await target.answer("Команда не найдена.")
            return
        await target.answer(
            "⌨️ <b>Команда</b>\n\n"
            f"Триггер: <code>{html.escape(item['trigger'])}</code>\n"
            f"Состояние: {bool_icon(item['enabled'])}\n"
            f"Уведомление о срабатывании: {bool_icon(item['notify'])}\n\n"
            f"Ответ:\n<pre>{html.escape(clipped(item['response'], 2000))}</pre>",
            reply_markup=keyboard([
                [("Выключить" if item["enabled"] else "Включить", f"command_toggle:{reply_id}")],
                [("✏️ Изменить ответ", f"command_edit:{reply_id}")],
                [("🔔 Переключить уведомление", f"command_notify:{reply_id}")],
                [("🗑 Удалить", f"command_delete_ask:{reply_id}")],
                [("⬅️ Команды", "command_replies")],
            ]),
        )

    @router.callback_query(F.data.startswith("command_reply:"))
    async def command_reply(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_command_reply(
            callback.message, callback.from_user.id, int(callback.data.split(":", 1)[1])
        )

    @router.callback_query(F.data.startswith("command_toggle:"))
    async def command_toggle(callback: CallbackQuery) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        await db.toggle_command_reply(callback.from_user.id, reply_id)
        await callback.answer("Сохранено")
        await show_command_reply(callback.message, callback.from_user.id, reply_id)

    @router.callback_query(F.data.startswith("command_edit:"))
    async def command_edit(callback: CallbackQuery, state: FSMContext) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        item = await db.get_command_reply(callback.from_user.id, reply_id)
        if not item:
            await callback.answer("Команда не найдена", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(CommandReplyState.response)
        await state.update_data(command_trigger=item["trigger"])
        await callback.message.answer(
            "Отправьте новый ответ до 3000 символов. Для отмены: /cancel\n\n"
            f"Сейчас:\n<pre>{html.escape(clipped(item['response'], 1800))}</pre>"
        )

    @router.callback_query(F.data.startswith("command_notify:"))
    async def command_notify(callback: CallbackQuery) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        await db.toggle_command_notification(callback.from_user.id, reply_id)
        await callback.answer("Сохранено")
        await show_command_reply(callback.message, callback.from_user.id, reply_id)

    @router.callback_query(F.data.startswith("command_delete_ask:"))
    async def command_delete_ask(callback: CallbackQuery) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.answer(
            "Удалить эту команду без возможности восстановления?",
            reply_markup=keyboard([
                [("Да, удалить", f"command_delete:{reply_id}")],
                [("Отмена", f"command_reply:{reply_id}")],
            ]),
        )

    @router.callback_query(F.data.startswith("command_delete:"))
    async def command_delete(callback: CallbackQuery) -> None:
        reply_id = int(callback.data.split(":", 1)[1])
        await db.delete_command_reply(callback.from_user.id, reply_id)
        await callback.answer("Команда удалена")
        await show_command_replies(callback.message, callback.from_user.id)

    async def show_autoreply(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        await target.answer(
            "🤖 <b>Автоответчик</b>\n"
            f"Рабочее время: <b>{row['autoreply_work_start']:02d}:00–{row['autoreply_work_end']:02d}:00</b> "
            "по времени сервера\n"
            f"Задержка перед ответом: <b>{row['autoreply_delay_seconds']} сек.</b>\n"
            f"Повтор в одном чате: <b>{row['autoreply_cooldown_minutes']} мин.</b>\n"
            f"Только первый контакт: {bool_icon(row['autoreply_new_chats_only'])}\n\n"
            f"Текст: <i>{html.escape(clipped(row['autoreply_text'], 1000))}</i>",
            reply_markup=keyboard([
                [(f"{bool_icon(row['autoreply_enabled'])} Включён", "toggle:autoreply_enabled")],
                [("✏️ Изменить текст", "autoreply_text")],
                [("⏱ Задержка", "autoreply_delay"), ("🔁 Интервал", "autoreply_cooldown")],
                [("🕒 Рабочее время", "autoreply_hours")],
                [(f"{bool_icon(row['autoreply_new_chats_only'])} Только новым", "toggle:autoreply_new_chats_only")],
                [("⭐ Ответы на отзывы", "review_replies")],
                [("👁 Предпросмотр", "autoreply_preview")],
                [("🧩 Доступные переменные", "variables")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "autoreply")
    async def autoreply(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_autoreply(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "autoreply_text")
    async def autoreply_text(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(AutoReplyState.text)
        await callback.message.answer("Отправьте новый текст автоответа (до 1500 символов) или /cancel.")

    @router.callback_query(F.data == "autoreply_cooldown")
    async def autoreply_cooldown(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(AutoReplyState.cooldown)
        await callback.message.answer(
            "Введите интервал повторного автоответа от 0 до 1440 минут. 0 — отвечать на каждое сообщение."
        )

    @router.message(AutoReplyState.cooldown, F.text)
    async def save_autoreply_cooldown(message: Message, state: FSMContext) -> None:
        if not message.text.strip().isdigit() or not 0 <= int(message.text) <= 1440:
            await message.answer("Введите целое число от 0 до 1440.")
            return
        await db.set_integer_setting(
            message.from_user.id, "autoreply_cooldown_minutes", int(message.text)
        )
        await state.clear()
        await show_autoreply(message, message.from_user.id)

    @router.callback_query(F.data == "autoreply_delay")
    async def autoreply_delay(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(AutoReplyState.delay)
        await callback.message.answer("Введите задержку от 0 до 300 секунд перед автоответом.")

    @router.message(AutoReplyState.delay, F.text)
    async def save_autoreply_delay(message: Message, state: FSMContext) -> None:
        if not message.text.strip().isdigit() or not 0 <= int(message.text) <= 300:
            await message.answer("Введите целое число от 0 до 300.")
            return
        await db.set_integer_setting(
            message.from_user.id, "autoreply_delay_seconds", int(message.text)
        )
        await state.clear()
        await show_autoreply(message, message.from_user.id)

    @router.callback_query(F.data == "autoreply_hours")
    async def autoreply_hours(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(AutoReplyState.hours)
        await callback.message.answer(
            "Введите рабочее время в формате <code>9-22</code>. Для круглосуточной работы: <code>0-24</code>."
        )

    @router.message(AutoReplyState.hours, F.text)
    async def save_autoreply_hours(message: Message, state: FSMContext) -> None:
        match = re.fullmatch(r"\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*", message.text)
        if not match:
            await message.answer("Используйте формат 9-22 или 0-24.")
            return
        start, end = map(int, match.groups())
        if not 0 <= start <= 23 or not 1 <= end <= 24 or start == end:
            await message.answer("Начало: 0–23, окончание: 1–24; значения не должны совпадать.")
            return
        await db.set_integer_setting(message.from_user.id, "autoreply_work_start", start)
        await db.set_integer_setting(message.from_user.id, "autoreply_work_end", end)
        await state.clear()
        await show_autoreply(message, message.from_user.id)

    @router.callback_query(F.data == "autoreply_preview")
    async def autoreply_preview(callback: CallbackQuery) -> None:
        await callback.answer()
        row = await db.get_user(callback.from_user.id)
        runtime = manager.get(callback.from_user.id)

        class PreviewMessage:
            author = "Покупатель"
            chat_name = "Покупатель"
            chat_id = 123456

            def __str__(self) -> str:
                return "Здравствуйте, товар в наличии?"

        sample = PreviewMessage()
        rendered = render_template(
            row["autoreply_text"], message=sample, account=runtime.account if runtime else None
        )
        await callback.message.answer(
            f"👁 <b>Предпросмотр</b>\n<blockquote>{html.escape(rendered)}</blockquote>"
        )

    @router.callback_query(F.data == "variables")
    async def variables(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "🧩 <b>Переменные исходящих сообщений</b>\n\n"
            "<code>$username</code> — имя собеседника\n"
            "<code>$message_text</code> — входящее сообщение\n"
            "<code>$chat_id</code>, <code>$chat_name</code> — чат\n"
            "<code>$date</code>, <code>$time</code>, <code>$full_time</code> — дата и время\n"
            "<code>$account_name</code>, <code>$account_id</code> — ваш аккаунт\n"
            "<code>$order_id</code>, <code>$order_link</code>, <code>$order_title</code> — заказ.\n\n"
            "Для отзывов: <code>$stars</code>, <code>$rating</code>, "
            "<code>$review_text</code>, <code>$review_reply</code>.\n\n"
            "Переменные работают в автоответчике, ручных сообщениях и ответах на отзывы.",
            reply_markup=keyboard([[("⬅️ Автоответчик", "autoreply")]]),
        )

    @router.message(AutoReplyState.text, F.text)
    async def save_autoreply_text(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not value or len(value) > 1500:
            await message.answer("Текст должен содержать от 1 до 1500 символов.")
            return
        await db.set_autoreply_text(message.from_user.id, value)
        await state.clear()
        await message.answer("✅ Текст автоответа сохранён.")
        await show_autoreply(message, message.from_user.id)

    async def show_review_replies(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        previews = "\n".join(
            f"{'⭐' * stars}: <code>{html.escape(clipped(row[f'review_reply_{stars}'], 150))}</code>"
            for stars in range(1, 6)
        )
        await target.answer(
            "⭐ <b>Автоответы на отзывы</b>\n"
            "Для каждой оценки используется отдельный шаблон. При изменении отзыва ответ обновляется.\n\n"
            f"{previews}",
            reply_markup=keyboard([
                [(f"{bool_icon(row['review_reply_enabled'])} Автоответы", "toggle:review_reply_enabled")],
                [("⭐ 1", "review_template:1"), ("⭐⭐ 2", "review_template:2")],
                [("⭐⭐⭐ 3", "review_template:3"), ("⭐⭐⭐⭐ 4", "review_template:4")],
                [("⭐⭐⭐⭐⭐ 5", "review_template:5")],
                [("🧪 Проверить шаблон", "review_preview_menu")],
                [("🧩 Переменные", "variables"), ("⬅️ Назад", "autoreply")],
            ]),
        )

    @router.callback_query(F.data == "review_replies")
    async def review_replies(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_review_replies(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "review_preview_menu")
    async def review_preview_menu(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "🧪 <b>Проверка автоответа на отзыв</b>\n\n"
            "Выберите оценку. Бот подставит тестовые данные, но ничего не отправит на FunPay.",
            reply_markup=keyboard([
                [("⭐ 1", "review_preview:1"), ("⭐⭐ 2", "review_preview:2")],
                [("⭐⭐⭐ 3", "review_preview:3"), ("⭐⭐⭐⭐ 4", "review_preview:4")],
                [("⭐⭐⭐⭐⭐ 5", "review_preview:5")],
                [("⬅️ Автоответы", "review_replies")],
            ]),
        )

    @router.callback_query(F.data.startswith("review_preview:"))
    async def review_preview(callback: CallbackQuery) -> None:
        stars = int(callback.data.split(":", 1)[1])
        row = await db.get_user(callback.from_user.id)
        runtime = manager.get(callback.from_user.id)
        await callback.answer()
        if not row or stars not in range(1, 6):
            await callback.message.answer("Шаблон не найден.")
            return

        class PreviewReview:
            text = "Всё получил, спасибо!"
            reply = ""

            def __init__(self, value: int):
                self.stars = value

        class PreviewOrder:
            id = "TEST1234"
            buyer_username = "Покупатель"
            chat_id = 123456
            title = "Тестовый лот"

            def __init__(self, review: PreviewReview):
                self.review = review

        review = PreviewReview(stars)
        order = PreviewOrder(review)
        rendered = normalize_review_reply(
            render_template(
                row[f"review_reply_{stars}"],
                order=order,
                review=review,
                account=runtime.account if runtime else None,
            )
        )
        await callback.message.answer(
            f"🧪 <b>Предпросмотр для {'⭐' * stars}</b>\n\n"
            f"<pre>{html.escape(rendered or '[пустой ответ]')}</pre>",
            reply_markup=keyboard([
                [("✏️ Изменить", f"review_template:{stars}")],
                [("⬅️ Выбрать оценку", "review_preview_menu")],
            ]),
        )

    @router.callback_query(F.data.startswith("review_template:"))
    async def review_template(callback: CallbackQuery, state: FSMContext) -> None:
        stars = int(callback.data.split(":", 1)[1])
        await callback.answer()
        await state.clear()
        await state.update_data(stars=stars)
        await state.set_state(AutoReplyState.review_text)
        await callback.message.answer(
            f"Отправьте шаблон ответа для оценки {'⭐' * stars}. До 999 символов и 10 строк."
        )

    @router.message(AutoReplyState.review_text, F.text)
    async def save_review_template(message: Message, state: FSMContext) -> None:
        value = normalize_review_reply(message.text)
        if not value:
            await message.answer("Шаблон не может быть пустым.")
            return
        data = await state.get_data()
        await db.set_review_reply(message.from_user.id, data["stars"], value)
        await state.clear()
        await show_review_replies(message, message.from_user.id)

    async def show_auto_raise(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        runtime = manager.get(user_id)
        if not row:
            return
        last = runtime.last_raise_at.strftime("%d.%m %H:%M:%S") if runtime and runtime.last_raise_at else "ещё не было"
        summary = html.escape(runtime.last_raise_summary) if runtime else "Runner остановлен"
        await target.answer(
            "🆙 <b>Автоподнятие лотов</b>\n"
            f"Состояние: {bool_icon(row['auto_raise_enabled'])}\n"
            f"Последний запуск: <code>{last}</code>\n"
            f"Результат: <code>{summary}</code>\n\n"
            "Категории определяются автоматически по активным лотам профиля. "
            "Следующая попытка назначается по таймеру FunPay.",
            reply_markup=keyboard([
                [(f"{bool_icon(row['auto_raise_enabled'])} Автоподнятие", "toggle:auto_raise_enabled")],
                [("🆙 Поднять сейчас", "raise_now")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "auto_raise")
    async def auto_raise(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_auto_raise(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "raise_now")
    async def raise_now(callback: CallbackQuery) -> None:
        await callback.answer("Поднимаю лоты…")
        try:
            result = await manager.raise_lots_now(callback.from_user.id)
        except Exception as exc:
            logger.exception("Ручное поднятие лотов не удалось")
            await callback.message.answer(f"❌ Не удалось поднять лоты: {html.escape(clipped(exc, 500))}")
            return
        await callback.message.answer(result, reply_markup=keyboard([[("⬅️ Автоподнятие", "auto_raise")]]))

    async def show_playerok_plugins(target: Message, user_id: int) -> None:
        plugin_runtime = manager.playerok_plugins.runtimes.get(user_id)
        plugins = list(plugin_runtime.plugins.values()) if plugin_runtime else []
        await target.answer(
            "🧩 <b>Плагины Playerok</b>\n"
            f"Установлено: <b>{len(plugins)}</b>\n\n"
            "Каталог содержит официальные расширения и публикации пользователей. "
            "Настройки каждого установленного плагина доступны постоянно в его карточке.",
            reply_markup=keyboard([
                [("🧭 Каталог плагинов", "po_plugin_catalog:0")],
                [(f"🧩 Мои плагины ({len(plugins)})", "po_my_plugins")],
                [("➕ Загрузить плагин", "po_plugin_upload_warning")],
                [("📚 Документация", "po_plugin_docs")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "po_plugins")
    async def playerok_plugins(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_plugins(callback.message, callback.from_user.id)

    async def show_playerok_my_plugins(target: Message, user_id: int) -> None:
        plugin_runtime = manager.playerok_plugins.runtimes.get(user_id)
        plugins = list(plugin_runtime.plugins.values()) if plugin_runtime else []
        rows = [
            [(
                f"{'✅' if plugin.enabled else '❌'} {clipped(plugin.name, 27)} v{clipped(plugin.version, 9)}",
                f"po_pi:{plugin.uuid}",
            )]
            for plugin in plugins
        ]
        rows.append([("⬅️ Плагины", "po_plugins")])
        await target.answer(
            "🧩 <b>Мои плагины Playerok</b>\n\n"
            + ("Выберите плагин." if plugins else "Пока ничего не установлено."),
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "po_my_plugins")
    async def playerok_my_plugins(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_my_plugins(callback.message, callback.from_user.id)

    async def show_playerok_plugin_catalog(
        target: Message, user_id: int, page: int = 0
    ) -> None:
        page = max(0, page)
        offset = page * PLUGIN_CATALOG_PAGE_SIZE
        catalog, total = await db.list_playerok_catalog_plugins(
            PLUGIN_CATALOG_PAGE_SIZE, offset
        )
        if not catalog and page:
            page = max(0, (total - 1) // PLUGIN_CATALOG_PAGE_SIZE)
            offset = page * PLUGIN_CATALOG_PAGE_SIZE
            catalog, total = await db.list_playerok_catalog_plugins(
                PLUGIN_CATALOG_PAGE_SIZE, offset
            )
        plugin_runtime = manager.playerok_plugins.runtimes.get(user_id)
        installed = set(plugin_runtime.plugins) if plugin_runtime else set()
        rows = [
            [(
                (
                    f"{'✅' if item['uuid'] in installed else ('🛡' if item['is_official'] else '🧩')} "
                    f"{clipped(item['name'], 27)} v{clipped(item['version'], 8)}"
                ),
                f"po_pc_view:{item['uuid']}:{page}",
            )]
            for item in catalog
        ]
        navigation = []
        if page:
            navigation.append(("◀️", f"po_plugin_catalog:{page - 1}"))
        if offset + len(catalog) < total:
            navigation.append(("▶️", f"po_plugin_catalog:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("⬅️ Плагины", "po_plugins")])
        await target.answer(
            "🧭 <b>Каталог плагинов Playerok</b>\n\n"
            f"Публикаций: <b>{total}</b> · страница <b>{page + 1}</b>.\n"
            "🛡 — официальный · 🧩 — сообщество · ✅ — установлен.\n\n"
            "⚠️ Публикации сообщества не модерируются: перед установкой скачайте и проверьте исходник.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("po_plugin_catalog:"))
    async def playerok_plugin_catalog(callback: CallbackQuery) -> None:
        await callback.answer()
        try:
            page = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            page = 0
        await show_playerok_plugin_catalog(callback.message, callback.from_user.id, page)

    @router.callback_query(F.data.startswith("po_pc_view:"))
    async def playerok_catalog_details(callback: CallbackQuery) -> None:
        _, uuid, raw_page = callback.data.split(":", 2)
        item = await db.get_playerok_catalog_plugin(uuid)
        if not item:
            await callback.answer("Публикация не найдена", show_alert=True)
            return
        await callback.answer()
        plugin_runtime = manager.playerok_plugins.runtimes.get(callback.from_user.id)
        installed = bool(plugin_runtime and uuid in plugin_runtime.plugins)
        rows = [[(
            "🧩 Открыть установленный" if installed else "⬇️ Установить",
            f"po_pi:{uuid}" if installed else f"po_pc_ask:{uuid}",
        )]]
        rows.append([("📥 Скачать исходник", f"po_pc_source:{uuid}")])
        rows.append([("⬅️ Каталог", f"po_plugin_catalog:{raw_page}")])
        badge = "🛡 <b>Официальный плагин</b>" if item["is_official"] else "🧩 Плагин сообщества"
        await callback.message.answer(
            f"{badge}\n\n"
            f"<b>{html.escape(item['name'])}</b> v{html.escape(item['version'])}\n"
            f"<i>{html.escape(item['short_description'])}</i>\n\n"
            f"{html.escape(item['description'])}\n\n"
            f"Автор: <b>{html.escape(item['publisher_name'])}</b>\n"
            f"Установок: <b>{item['install_count']}</b>\n"
            f"UUID: <code>{uuid}</code>",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("po_pc_source:"))
    async def playerok_catalog_source(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_playerok_catalog_plugin(uuid)
        if not item:
            await callback.answer("Исходник недоступен", show_alert=True)
            return
        await callback.answer("Отправляю файл…")
        await callback.message.answer_document(
            BufferedInputFile(item["source"].encode("utf-8"), filename=item["filename"]),
            caption=f"Исходник Playerok-плагина <b>{html.escape(item['name'])}</b>. Проверьте его перед установкой.",
        )

    @router.callback_query(F.data.startswith("po_pc_ask:"))
    async def playerok_catalog_install_ask(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_playerok_catalog_plugin(uuid)
        if not item:
            await callback.answer("Плагин недоступен", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Установить <b>{html.escape(item['name'])}</b>?\n\n"
            "Плагин выполняется внутри процесса бота и получит доступ к Playerok-аккаунту, "
            "сети и возможностям Python-процесса. Каталог не гарантирует безопасность кода.",
            reply_markup=keyboard([
                [("📥 Скачать и проверить", f"po_pc_source:{uuid}")],
                [("Я доверяю — установить", f"po_pc_install:{uuid}")],
                [("Отмена", f"po_pc_view:{uuid}:0")],
            ]),
        )

    @router.callback_query(F.data.startswith("po_pc_install:"))
    async def playerok_catalog_install(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_playerok_catalog_plugin(uuid)
        runtime = await require_playerok_runtime(callback.message, callback.from_user.id)
        if not item or not runtime:
            return
        if manager.playerok_plugins.is_enabled(callback.from_user.id, uuid) or (
            manager.playerok_plugins.runtimes.get(callback.from_user.id)
            and uuid in manager.playerok_plugins.runtimes[callback.from_user.id].plugins
        ):
            await callback.answer("Плагин уже установлен", show_alert=True)
            return
        await callback.answer("Устанавливаю…")
        try:
            plugin = await manager.playerok_plugins.install(
                callback.from_user.id, item["filename"], item["source"], runtime
            )
        except Exception as exc:
            logger.exception("Не установлен Playerok-плагин из каталога")
            await callback.message.answer(f"❌ Установка не выполнена: {html.escape(clipped(exc, 600))}")
            return
        await db.increment_playerok_catalog_install(uuid)
        await callback.message.answer(
            f"✅ Playerok-плагин <b>{html.escape(plugin.name)}</b> установлен.",
            reply_markup=keyboard([[('⚙️ Открыть плагин', f"po_pi:{uuid}")]]),
        )

    @router.callback_query(F.data == "po_plugin_docs")
    async def playerok_plugin_docs(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "📚 <b>Playerok Plugin SDK</b>\n\n"
            "Плагины — одиночные UTF-8 файлы Python до 512 КБ. Они получают события сообщений, "
            "сделок, отзывов и периодический тик, а настройки строятся автоматически из SETTINGS.",
            reply_markup=keyboard([
                [("🚀 Быстрый старт", "po_docs:start"), ("🧱 Структура", "po_docs:structure")],
                [("⚡ Хуки", "po_docs:hooks"), ("⚙️ Настройки", "po_docs:settings")],
                [("🧭 Каталог", "po_docs:catalog")],
                [("📥 Скачать полную документацию", "po_docs_download")],
                [("🛡 Безопасность", "po_docs:safety")],
                [("⬅️ Плагины", "po_plugins")],
            ]),
        )

    @router.callback_query(F.data == "po_docs_download")
    async def playerok_plugin_docs_download(callback: CallbackQuery) -> None:
        if not PLAYEROK_PLUGIN_DOCUMENTATION_PATH.is_file():
            await callback.answer("Файл документации отсутствует", show_alert=True)
            return
        await callback.answer("Отправляю документацию…")
        await callback.message.answer_document(
            FSInputFile(PLAYEROK_PLUGIN_DOCUMENTATION_PATH),
            caption="Полная документация Playerok Plugin SDK. Её можно передать нейросети вместе с заданием на плагин.",
        )

    @router.callback_query(F.data.startswith("po_docs:"))
    async def playerok_plugin_docs_page(callback: CallbackQuery) -> None:
        await callback.answer()
        page = callback.data.split(":", 1)[1]
        pages = {
            "start": (
                "🚀 <b>Быстрый старт</b>\n\n"
                "1. Возьмите шаблон из скачиваемой документации.\n"
                "2. Создайте новый UUID4 и заполните метаданные.\n"
                "3. Объявите SETTINGS, ACTIONS и все списки BIND_TO_*.\n"
                "4. Загрузите .py через «Плагины → Загрузить плагин».\n"
                "5. Проверьте настройки и события на тестовом аккаунте."
            ),
            "structure": (
                "🧱 <b>Структура</b>\n\n"
                "Обязательны NAME, VERSION, DESCRIPTION, CREDITS, UUID, SETTINGS_PAGE, SETTINGS, "
                "ACTIONS, BIND_TO_DELETE и все списки хуков. Обработчик получает ctx; "
                "аккаунт доступен как <code>ctx.account</code>, настройки — <code>ctx.get_setting()</code>."
            ),
            "hooks": (
                "⚡ <b>Хуки</b>\n\n"
                "<code>BIND_TO_START</code>, <code>BIND_TO_STOP</code>, <code>BIND_TO_TICK</code>, "
                "<code>BIND_TO_NEW_MESSAGE</code>, <code>BIND_TO_DEAL_CHANGED</code>, "
                "<code>BIND_TO_NEW_REVIEW</code>, <code>BIND_TO_SETTING_CHANGED</code>.\n\n"
                "Допускаются def и async def. Синхронные функции выполняются вне event loop."
            ),
            "settings": (
                "⚙️ <b>Настройки</b>\n\n"
                "SETTINGS поддерживает типы bool, int, str и choice. Бот автоматически создаёт "
                "постоянную кнопку «Настройки», проверяет значения и сохраняет их в PostgreSQL. "
                "ACTIONS добавляет кнопки ручных действий на ту же страницу."
            ),
            "catalog": (
                "🧭 <b>Публикация</b>\n\n"
                "Установите и проверьте свой плагин, откройте его карточку, нажмите «Опубликовать» "
                "и добавьте описание назначения, настройки, зависимостей и ограничений. "
                "Чужой или официальный UUID перезаписать нельзя."
            ),
            "safety": (
                "🛡 <b>Безопасность</b>\n\n"
                "Плагин — исполняемый Python-код с доступом к Playerok-сессии и окружению процесса. "
                "Проверяйте исходник, не сохраняйте cookie и прокси в коде или логах. "
                "Официального стабильного Playerok API нет, поэтому обрабатывайте ошибки сети и изменения схемы."
            ),
        }
        text = pages.get(page)
        if not text:
            await callback.message.answer("Раздел документации не найден.")
            return
        await callback.message.answer(text, reply_markup=keyboard([[('⬅️ Документация', 'po_plugin_docs')]]))

    @router.callback_query(F.data == "po_plugin_upload_warning")
    async def playerok_plugin_upload_warning(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "⚠️ <b>Внимание</b>\n"
            "Playerok-плагин выполняется с правами процесса бота и получает доступ к аккаунту. "
            "Продолжайте только если доверяете автору и проверили исходный код.",
            reply_markup=keyboard([
                [("Я понимаю риск", "po_plugin_upload_confirm")],
                [("Отмена", "po_plugins")],
            ]),
        )

    @router.callback_query(F.data == "po_plugin_upload_confirm")
    async def playerok_plugin_upload_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_playerok_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.set_state(PlayerokPluginState.file)
        await callback.message.answer("Отправьте одиночный UTF-8 файл Playerok-плагина .py до 512 КБ. Для отмены: /cancel")

    @router.message(PlayerokPluginState.file)
    async def playerok_plugin_upload_file(message: Message, state: FSMContext) -> None:
        document = message.document
        if not document or not (document.file_name or "").lower().endswith(".py"):
            await message.answer("❌ Отправьте документ с расширением .py.")
            return
        if document.file_size and document.file_size > 512 * 1024:
            await message.answer("❌ Размер плагина не должен превышать 512 КБ.")
            return
        runtime = await require_playerok_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        buffer = BytesIO()
        await message.bot.download(document, destination=buffer)
        try:
            source = buffer.getvalue().decode("utf-8-sig")
            plugin = await manager.playerok_plugins.install(
                message.from_user.id, document.file_name, source, runtime
            )
        except UnicodeDecodeError:
            await message.answer("❌ Файл должен быть текстовым UTF-8 Python-модулем.")
            return
        except PlayerokPluginValidationError as exc:
            await message.answer(f"❌ Плагин отклонён: {html.escape(str(exc))}")
            return
        except Exception as exc:
            logger.exception("Не удалось установить Playerok-плагин")
            await message.answer(f"❌ Ошибка установки: {html.escape(clipped(exc, 600))}")
            return
        await state.clear()
        await message.answer(f"✅ Playerok-плагин <b>{html.escape(plugin.name)}</b> установлен.")
        await show_playerok_my_plugins(message, message.from_user.id)

    async def show_playerok_plugin_info(target: Message, user_id: int, uuid: str) -> None:
        runtime = manager.playerok_plugins.runtimes.get(user_id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        if not plugin:
            await target.answer("Плагин не найден.")
            return
        publication = await db.get_playerok_catalog_plugin(uuid)
        rows = []
        if plugin.settings_page:
            rows.append([("⚙️ Настройки", f"po_ps:{uuid}")])
        publication_status = "Не опубликован"
        if publication:
            if publication["is_official"]:
                publication_status = "🛡 официальная публикация"
                rows.append([("🧭 Открыть в каталоге", f"po_pc_view:{uuid}:0")])
            elif publication["owner_telegram_id"] == user_id:
                publication_status = "✅ опубликован вами"
                rows.append([("📝 Обновить публикацию", f"po_pub_start:{uuid}")])
                rows.append([("Убрать из каталога", f"po_unpub_ask:{uuid}")])
            else:
                publication_status = "Опубликован другим пользователем"
                rows.append([("🧭 Открыть в каталоге", f"po_pc_view:{uuid}:0")])
        elif uuid not in PLAYEROK_READY_PLUGIN_BY_UUID:
            rows.append([("🌐 Опубликовать в каталоге", f"po_pub_start:{uuid}")])
        rows.extend([
            [("Выключить" if plugin.enabled else "Включить", f"po_pt:{uuid}")],
            [("🗑 Удалить", f"po_pd_ask:{uuid}")],
            [("⬅️ Мои плагины", "po_my_plugins")],
        ])
        hooks_count = sum(len(value) for value in plugin.hooks.values())
        await target.answer(
            f"🧩 <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)}\n"
            f"{html.escape(plugin.description)}\n\n"
            f"Автор: {html.escape(plugin.credits)}\n"
            f"UUID: <code>{plugin.uuid}</code>\n"
            f"Хуков: <b>{hooks_count}</b> · действий: <b>{len(plugin.actions)}</b>\n"
            f"Настройки: {bool_icon(plugin.settings_page)}\n"
            f"Каталог: {html.escape(publication_status)}\n"
            f"Состояние: {bool_icon(plugin.enabled)}",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("po_pi:"))
    async def playerok_plugin_info(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await show_playerok_plugin_info(callback.message, callback.from_user.id, callback.data.split(":", 1)[1])

    async def show_playerok_plugin_settings(target: Message, user_id: int, uuid: str) -> None:
        runtime = manager.playerok_plugins.runtimes.get(user_id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        if not plugin or not plugin.settings_page:
            await target.answer("Страница настроек не найдена.")
            return
        values = runtime.settings.get(uuid, {})
        rows = []
        for index, (key, spec) in enumerate(plugin.settings_schema.items()):
            value = values.get(key, spec["default"])
            rows.append([(
                f"{spec['label']}: {clipped(playerok_setting_label(spec, value), 22)}",
                f"po_pset:{uuid}:{index}",
            )])
        for index, action in enumerate(plugin.actions.values()):
            rows.append([(action["label"], f"po_pact:{uuid}:{index}")])
        rows.append([("⬅️ К плагину", f"po_pi:{uuid}")])
        await target.answer(
            f"⚙️ <b>Настройки: {html.escape(plugin.name)}</b>\n\n"
            "Нажмите настройку для изменения. Значения сохраняются в PostgreSQL и восстанавливаются после перезапуска.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("po_ps:"))
    async def playerok_plugin_settings(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_playerok_plugin_settings(callback.message, callback.from_user.id, callback.data.split(":", 1)[1])

    @router.callback_query(F.data.startswith("po_pset:"))
    async def playerok_plugin_setting_change(callback: CallbackQuery, state: FSMContext) -> None:
        _, uuid, raw_index = callback.data.split(":", 2)
        runtime = manager.playerok_plugins.runtimes.get(callback.from_user.id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        if not plugin or not plugin.enabled:
            await callback.answer("Сначала включите плагин", show_alert=True)
            return
        items = list(plugin.settings_schema.items())
        try:
            key, spec = items[int(raw_index)]
        except (ValueError, IndexError):
            await callback.answer("Настройка не найдена", show_alert=True)
            return
        current = runtime.settings.get(uuid, {}).get(key, spec["default"])
        if spec["type"] == "bool":
            await manager.playerok_plugins.set_setting(callback.from_user.id, uuid, key, not bool(current))
            await callback.answer("Сохранено")
            await show_playerok_plugin_settings(callback.message, callback.from_user.id, uuid)
            return
        if spec["type"] == "choice":
            choices = list(spec["choices"])
            index = choices.index(str(current)) if str(current) in choices else -1
            await manager.playerok_plugins.set_setting(callback.from_user.id, uuid, key, choices[(index + 1) % len(choices)])
            await callback.answer("Выбран следующий вариант")
            await show_playerok_plugin_settings(callback.message, callback.from_user.id, uuid)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokPluginState.setting_value)
        await state.update_data(po_plugin_uuid=uuid, po_plugin_setting=key)
        constraints = (
            f"от {spec.get('min', '−∞')} до {spec.get('max', '+∞')}"
            if spec["type"] == "int"
            else f"от {spec.get('min_length', 0)} до {spec.get('max_length', 2000)} символов"
        )
        await callback.message.answer(
            f"Введите новое значение «<b>{html.escape(spec['label'])}</b>» ({constraints}). Для отмены: /cancel"
        )

    @router.message(PlayerokPluginState.setting_value, F.text)
    async def playerok_plugin_setting_save(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        uuid = data.get("po_plugin_uuid")
        key = data.get("po_plugin_setting")
        if not uuid or not key:
            await state.clear()
            return
        try:
            await manager.playerok_plugins.set_setting(message.from_user.id, uuid, key, message.text)
        except (KeyError, ValueError) as exc:
            await message.answer(f"❌ Значение не сохранено: {html.escape(str(exc))}")
            return
        await state.clear()
        await message.answer("✅ Настройка сохранена.")
        await show_playerok_plugin_settings(message, message.from_user.id, uuid)

    @router.callback_query(F.data.startswith("po_pact:"))
    async def playerok_plugin_action(callback: CallbackQuery) -> None:
        _, uuid, raw_index = callback.data.split(":", 2)
        runtime = manager.playerok_plugins.runtimes.get(callback.from_user.id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        if not plugin or not plugin.enabled:
            await callback.answer("Сначала включите плагин", show_alert=True)
            return
        actions = list(plugin.actions)
        try:
            action_id = actions[int(raw_index)]
        except (ValueError, IndexError):
            await callback.answer("Действие не найдено", show_alert=True)
            return
        await callback.answer("Выполняю…")
        try:
            result = await manager.playerok_plugins.run_action(callback.from_user.id, uuid, action_id)
        except Exception as exc:
            logger.exception("Ошибка действия Playerok-плагина %s", uuid)
            await callback.message.answer(f"❌ Действие завершилось ошибкой: <code>{html.escape(clipped(exc, 700))}</code>")
            return
        if result is not None:
            await callback.message.answer(clipped(str(result), 3900))
        await show_playerok_plugin_settings(callback.message, callback.from_user.id, uuid)

    @router.callback_query(F.data.startswith("po_pt:"))
    async def playerok_plugin_toggle(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        try:
            enabled = await manager.playerok_plugins.toggle(callback.from_user.id, uuid)
        except KeyError:
            await callback.answer("Плагин не найден", show_alert=True)
            return
        await callback.answer("Плагин включён" if enabled else "Плагин выключен")
        await show_playerok_plugin_info(callback.message, callback.from_user.id, uuid)

    @router.callback_query(F.data.startswith("po_pd_ask:"))
    async def playerok_plugin_delete_ask(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        await callback.answer()
        await callback.message.answer(
            "Удалить Playerok-плагин, его исходник и настройки?",
            reply_markup=keyboard([[("Да, удалить", f"po_pd_do:{uuid}"), ("Отмена", f"po_pi:{uuid}")]]),
        )

    @router.callback_query(F.data.startswith("po_pd_do:"))
    async def playerok_plugin_delete(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        await callback.answer("Удаляю…")
        await manager.playerok_plugins.delete(callback.from_user.id, uuid)
        await callback.message.answer("✅ Playerok-плагин удалён.")
        await show_playerok_my_plugins(callback.message, callback.from_user.id)

    @router.callback_query(F.data.startswith("po_pub_start:"))
    async def playerok_catalog_publish_start(callback: CallbackQuery, state: FSMContext) -> None:
        uuid = callback.data.split(":", 1)[1]
        plugin = await db.get_playerok_plugin(callback.from_user.id, uuid)
        publication = await db.get_playerok_catalog_plugin(uuid)
        if not plugin:
            await callback.answer("Установленный плагин не найден", show_alert=True)
            return
        if publication and (publication["is_official"] or publication["owner_telegram_id"] != callback.from_user.id):
            await callback.answer("Этот UUID уже опубликован другим автором", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(PlayerokCatalogPublishState.description)
        await state.update_data(po_catalog_uuid=uuid)
        await callback.message.answer(
            f"Отправьте описание Playerok-плагина: назначение, настройка, команды/действия, "
            f"зависимости и ограничения. От {PLUGIN_CATALOG_DESCRIPTION_MIN} до {PLUGIN_CATALOG_DESCRIPTION_MAX} символов.\n\nДля отмены: /cancel"
        )

    @router.message(PlayerokCatalogPublishState.description, F.text)
    async def playerok_catalog_publish_description(message: Message, state: FSMContext) -> None:
        try:
            description = validate_catalog_description(message.text or "")
        except ValueError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}")
            return
        uuid = (await state.get_data()).get("po_catalog_uuid")
        plugin = await db.get_playerok_plugin(message.from_user.id, uuid) if uuid else None
        if not plugin:
            await state.clear()
            await message.answer("❌ Плагин больше не установлен.")
            return
        await state.update_data(po_catalog_description=description)
        await message.answer(
            "🔎 <b>Предпросмотр публикации Playerok</b>\n\n"
            f"<b>{html.escape(plugin['name'])}</b> v{html.escape(plugin['version'])}\n"
            f"<i>{html.escape(plugin['description'])}</i>\n\n"
            f"{html.escape(description)}\n\n"
            f"Публичный автор: <b>{html.escape(telegram_publisher_name(message.from_user))}</b>",
            reply_markup=keyboard([[("✅ Опубликовать", f"po_pub_do:{uuid}"), ("Отмена", f"po_pi:{uuid}")]]),
        )

    @router.callback_query(F.data.startswith("po_pub_do:"))
    async def playerok_catalog_publish_do(callback: CallbackQuery, state: FSMContext) -> None:
        uuid = callback.data.split(":", 1)[1]
        data = await state.get_data()
        if data.get("po_catalog_uuid") != uuid or not data.get("po_catalog_description"):
            await callback.answer("Сессия публикации истекла", show_alert=True)
            return
        published = await db.publish_playerok_catalog_plugin(
            callback.from_user.id,
            uuid,
            telegram_publisher_name(callback.from_user),
            data["po_catalog_description"],
        )
        await state.clear()
        await callback.answer("Опубликовано" if published else "Публикация не выполнена", show_alert=not published)
        if published:
            await callback.message.answer("✅ Playerok-плагин опубликован в общем каталоге.", reply_markup=keyboard([[('🧭 Открыть', f"po_pc_view:{uuid}:0")]]))

    @router.callback_query(F.data.startswith("po_unpub_ask:"))
    async def playerok_catalog_unpublish_ask(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_playerok_catalog_plugin(uuid)
        if not item or item["owner_telegram_id"] != callback.from_user.id or item["is_official"]:
            await callback.answer("Удалить эту публикацию нельзя", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Убрать <b>{html.escape(item['name'])}</b> из каталога Playerok? Установленные копии останутся.",
            reply_markup=keyboard([[("Да, убрать", f"po_unpub_do:{uuid}"), ("Отмена", f"po_pi:{uuid}")]]),
        )

    @router.callback_query(F.data.startswith("po_unpub_do:"))
    async def playerok_catalog_unpublish_do(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        removed = await db.unpublish_playerok_catalog_plugin(callback.from_user.id, uuid)
        await callback.answer("Публикация удалена" if removed else "Публикация не найдена")
        await show_playerok_plugin_info(callback.message, callback.from_user.id, uuid)

    async def show_plugins(target: Message, user_id: int) -> None:
        runtime = await require_runtime(target, user_id)
        if not runtime:
            return
        plugin_runtime = manager.plugins.runtimes.get(user_id)
        plugins = list(plugin_runtime.plugins.values()) if plugin_runtime else []
        rows = [
            [("🧭 Каталог плагинов", "plugin_catalog:0")],
            [(f"🧩 Мои плагины ({len(plugins)})", "my_plugins")],
            [("➕ Загрузить плагин", "plugin_upload_warning")],
            [("📚 Документация", "plugin_docs")],
            [("⬅️ Меню", "menu")],
        ]
        await target.answer(
            "🧩 <b>Плагины FunPayCardinal</b>\n"
            f"Установлено: <b>{len(plugins)}</b>\n\n"
            "Откройте общий каталог, установите опубликованное расширение или загрузите "
            "собственный однофайловый .py-плагин.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "plugins")
    async def plugins(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_plugins(callback.message, callback.from_user.id)

    async def show_my_plugins(target: Message, user_id: int) -> None:
        if not await require_runtime(target, user_id):
            return
        plugin_runtime = manager.plugins.runtimes.get(user_id)
        plugins = list(plugin_runtime.plugins.values()) if plugin_runtime else []
        rows = [
            [(
                f"{'✅' if plugin.enabled else '❌'} {clipped(plugin.name, 27)} v{clipped(plugin.version, 10)}",
                f"plugin_info:{plugin.uuid}",
            )]
            for plugin in plugins
        ]
        rows.append([("⬅️ Плагины", "plugins")])
        text = (
            "🧩 <b>Мои плагины</b>\n\n"
            "Нажмите на плагин, чтобы открыть настройки, выключить или удалить его."
            if plugins
            else "🧩 <b>Мои плагины</b>\n\nПока ничего не установлено."
        )
        await target.answer(text, reply_markup=keyboard(rows))

    @router.callback_query(F.data == "my_plugins")
    async def my_plugins(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_my_plugins(callback.message, callback.from_user.id)

    async def show_plugin_catalog(target: Message, user_id: int, page: int = 0) -> None:
        if not await require_runtime(target, user_id):
            return
        page = max(0, page)
        catalog, total = await db.list_catalog_plugins(
            PLUGIN_CATALOG_PAGE_SIZE,
            page * PLUGIN_CATALOG_PAGE_SIZE,
        )
        total_pages = max(1, (total + PLUGIN_CATALOG_PAGE_SIZE - 1) // PLUGIN_CATALOG_PAGE_SIZE)
        if page >= total_pages:
            page = total_pages - 1
            catalog, total = await db.list_catalog_plugins(
                PLUGIN_CATALOG_PAGE_SIZE,
                page * PLUGIN_CATALOG_PAGE_SIZE,
            )
        plugin_runtime = manager.plugins.runtimes.get(user_id)
        installed = set(plugin_runtime.plugins) if plugin_runtime else set()
        rows = [
            [(
                (
                    f"{'✅' if plugin['uuid'] in installed else ('🛡' if plugin['is_official'] else '🧩')} "
                    f"{clipped(plugin['name'], 27)} v{clipped(plugin['version'], 8)}"
                ),
                f"catalog_view:{plugin['uuid']}:{page}",
            )]
            for plugin in catalog
        ]
        navigation = []
        if page > 0:
            navigation.append(("◀️", f"plugin_catalog:{page - 1}"))
        if page + 1 < total_pages:
            navigation.append(("▶️", f"plugin_catalog:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("⬅️ Плагины", "plugins")])
        await target.answer(
            "🧭 <b>Каталог плагинов</b>\n\n"
            f"Опубликовано: <b>{total}</b> · страница <b>{page + 1}/{total_pages}</b>\n"
            "🛡 — официальный · 🧩 — от сообщества · ✅ — уже установлен.\n\n"
            "Чтобы опубликовать свой плагин, откройте его в разделе «Мои плагины». "
            "⚠️ Каталог не модерируется: изучайте описание и скачивайте исходник перед установкой.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("plugin_catalog:"))
    async def plugin_catalog(callback: CallbackQuery) -> None:
        await callback.answer()
        try:
            page = int(callback.data.rsplit(":", 1)[1])
        except ValueError:
            page = 0
        await show_plugin_catalog(callback.message, callback.from_user.id, page)

    @router.callback_query(F.data == "ready_plugins")
    async def legacy_ready_plugins(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_plugin_catalog(callback.message, callback.from_user.id)

    @router.callback_query(F.data.startswith("catalog_view:"))
    async def catalog_plugin_details(callback: CallbackQuery) -> None:
        await callback.answer()
        _, uuid, raw_page = callback.data.split(":", 2)
        item = await db.get_catalog_plugin(uuid)
        if not item:
            await callback.message.answer("Публикация удалена из каталога.")
            return
        try:
            page = max(0, int(raw_page))
        except ValueError:
            page = 0
        installed = bool(
            manager.plugins.runtimes.get(callback.from_user.id)
            and uuid in manager.plugins.runtimes[callback.from_user.id].plugins
        )
        rows = []
        if installed:
            rows.append([("🧩 Открыть установленный", f"plugin_info:{uuid}")])
        else:
            rows.append([("⬇️ Установить", f"catalog_install_ask:{uuid}")])
        rows.append([("📥 Скачать исходник", f"catalog_source:{uuid}")])
        rows.append([("⬅️ Каталог", f"plugin_catalog:{page}")])
        badge = "🛡 <b>Официальный плагин</b>" if item["is_official"] else "🧩 Плагин сообщества"
        await callback.message.answer(
            f"{badge}\n"
            f"<b>{html.escape(clipped(item['name'], 100))}</b> "
            f"v{html.escape(clipped(item['version'], 30))}\n"
            f"<i>{html.escape(clipped(item['short_description'], 500))}</i>\n\n"
            f"{html.escape(item['description'])}\n\n"
            f"Опубликовал: <b>{html.escape(clipped(item['publisher_name'], 100))}</b>\n"
            f"Автор в файле: {html.escape(clipped(item['credits'], 200))}\n"
            f"Установок из каталога: <b>{item['install_count']}</b>\n"
            f"UUID: <code>{item['uuid']}</code>\n"
            f"Состояние: {'✅ установлен' if installed else 'не установлен'}",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("catalog_source:"))
    async def catalog_plugin_source(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_catalog_plugin(uuid)
        if not item:
            await callback.answer("Публикация удалена", show_alert=True)
            return
        await callback.answer("Готовлю исходник…")
        await callback.message.answer_document(
            BufferedInputFile(item["source"].encode("utf-8"), filename=item["filename"]),
            caption=(
                f"📄 Исходник <b>{html.escape(item['name'])}</b> v{html.escape(item['version'])}.\n"
                "Проверьте код перед установкой: плагины выполняются с правами процесса бота."
            ),
        )

    @router.callback_query(F.data.startswith("catalog_install_ask:"))
    async def catalog_plugin_install_ask(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_catalog_plugin(uuid)
        if not item:
            await callback.answer("Публикация удалена", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"⚠️ <b>Установить {html.escape(item['name'])}?</b>\n\n"
            "Плагин получит доступ к FunPay-аккаунту, golden_key, сети, переменным окружения "
            "и возможностям процесса бота. Каталог не гарантирует безопасность кода.",
            reply_markup=keyboard([
                [("📥 Скачать и проверить", f"catalog_source:{uuid}")],
                [("Я доверяю — установить", f"catalog_install_do:{uuid}")],
                [("Отмена", f"catalog_view:{uuid}:0")],
            ]),
        )

    @router.callback_query(F.data.startswith("catalog_install_do:"))
    async def catalog_plugin_install(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_catalog_plugin(uuid)
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not item or not runtime:
            await callback.answer("Плагин недоступен", show_alert=True)
            return
        if manager.plugins.runtimes.get(callback.from_user.id) and uuid in manager.plugins.runtimes[
            callback.from_user.id
        ].plugins:
            await callback.answer("Плагин уже установлен", show_alert=True)
            return
        await callback.answer("Устанавливаю…")
        try:
            await manager.plugins.install(
                callback.from_user.id,
                item["filename"],
                item["source"],
                runtime,
            )
        except Exception as exc:
            logger.exception("Не удалось установить плагин из каталога %s", uuid)
            await callback.message.answer(
                f"❌ Установка не выполнена: {html.escape(clipped(exc, 600))}"
            )
            return
        await db.increment_catalog_install(uuid)
        await callback.message.answer(
            f"✅ <b>{html.escape(item['name'])}</b> установлен из каталога.",
            reply_markup=keyboard([
                [("🧩 Открыть плагин", f"plugin_info:{uuid}")],
                [("⬅️ Каталог", "plugin_catalog:0")],
            ]),
        )

    @router.callback_query(F.data == "plugin_docs")
    async def plugin_docs(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "📚 <b>Документация по плагинам</b>\n\n"
            "Здесь описаны установка, полный контракт файла, события, Telegram-интерфейс и ограничения. "
            "Начните с раздела «Быстрый старт», если создаёте первый плагин.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚀 Быстрый старт", callback_data="plugin_docs:start"),
                    InlineKeyboardButton(text="🧱 Структура", callback_data="plugin_docs:structure"),
                ],
                [
                    InlineKeyboardButton(text="⚡ Хуки", callback_data="plugin_docs:hooks"),
                    InlineKeyboardButton(text="🤖 Telegram API", callback_data="plugin_docs:telegram"),
                ],
                [InlineKeyboardButton(text="🧭 Публикация в каталоге", callback_data="plugin_docs:catalog")],
                [InlineKeyboardButton(text="📥 Скачать полную документацию", callback_data="plugin_docs_download")],
                [InlineKeyboardButton(text="🛡 Совместимость и безопасность", callback_data="plugin_docs:safety")],
                [InlineKeyboardButton(text="🌐 Исходный FunPayCardinal", url="https://github.com/sidor0912/FunPayCardinal")],
                [InlineKeyboardButton(text="⬅️ Плагины", callback_data="plugins")],
            ]),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data == "plugin_docs_download")
    async def plugin_docs_download(callback: CallbackQuery) -> None:
        await callback.answer("Готовлю файл…")
        if not PLUGIN_DOCUMENTATION_PATH.is_file():
            await callback.message.answer(
                "❌ Файл документации отсутствует в текущей сборке."
            )
            return
        await callback.message.answer_document(
            document=FSInputFile(
                PLUGIN_DOCUMENTATION_PATH,
                filename="PLUGIN_DEVELOPMENT.md",
            ),
            caption=(
                "📚 Полный контракт плагинов <code>aiogram-compat-1</code>. "
                "Файл можно передать нейросети вместе с описанием нужного плагина."
            ),
        )

    @router.callback_query(F.data.startswith("plugin_docs:"))
    async def plugin_docs_page(callback: CallbackQuery) -> None:
        await callback.answer()
        page = callback.data.split(":", 1)[1]
        code_example = html.escape(
            """from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

NAME = "My Plugin"
VERSION = "1.0.0"
DESCRIPTION = "Описание"
CREDITS = "Автор"
SETTINGS_PAGE = False
UUID = "создайте UUID версии 4"
BIND_TO_DELETE = None

def on_message(cardinal, event):
    message = event.message
    if str(message).strip() == "#hello":
        cardinal.account.send_message(
            message.chat_id, "Привет!", message.chat_name
        )

BIND_TO_NEW_MESSAGE = [on_message]
"""
        )
        pages = {
            "start": (
                "🚀 <b>Быстрый старт</b>\n\n"
                "1. Создайте один UTF-8 файл с расширением <code>.py</code>.\n"
                "2. Добавьте обязательные метаданные и UUID4.\n"
                "3. Создайте функции-обработчики и поместите их в нужные списки BIND_TO_*.\n"
                "4. Откройте «Плагины → Загрузить плагин», подтвердите риск и отправьте файл документом.\n"
                "5. После проверки плагин появится в «Мои плагины». Его можно выключить без удаления.\n\n"
                "При перезапуске исходник восстанавливается из PostgreSQL автоматически. Максимальный размер — 512 КБ."
            ),
            "structure": (
                "🧱 <b>Минимальная структура плагина</b>\n\n"
                f"<pre>{code_example}</pre>\n"
                "<b>Обязательные поля:</b> NAME, VERSION, DESCRIPTION и CREDITS — строки; "
                "SETTINGS_PAGE — bool, создающий постоянную кнопку настроек; UUID — канонический UUID4; "
                "BIND_TO_DELETE — функция или None. "
                "Неиспользуемые BIND_TO_* можно не объявлять: загрузчик считает их пустыми списками."
            ),
            "hooks": (
                "⚡ <b>Поддерживаемые хуки</b>\n\n"
                "<b>Жизненный цикл:</b> PRE_INIT, POST_INIT, PRE_START, POST_START, PRE_STOP, POST_STOP.\n"
                "<b>Сообщения:</b> INIT_MESSAGE, MESSAGES_LIST_CHANGED, LAST_CHAT_MESSAGE_CHANGED, NEW_MESSAGE.\n"
                "<b>Заказы:</b> INIT_ORDER, NEW_ORDER, ORDERS_LIST_CHANGED, ORDER_STATUS_CHANGED.\n"
                "<b>Операции:</b> PRE_DELIVERY, POST_DELIVERY, PRE_LOTS_RAISE, POST_LOTS_RAISE.\n\n"
                "Перед каждым названием добавляется <code>BIND_TO_</code>. Событийный обработчик получает "
                "<code>(cardinal, event)</code>; обработчики жизненного цикла — объект cardinal; обработчик удаления — "
                "<code>(cardinal, callback)</code>. Ошибка одного обработчика записывается в лог и не останавливает остальные плагины."
            ),
            "telegram": (
                "🤖 <b>Telegram API плагина</b>\n\n"
                "Через <code>cardinal.telegram.bot</code> доступны: send_message, edit_message_text, "
                "edit_message_reply_markup, answer_callback_query, delete_message, message_handler, "
                "callback_query_handler и методы register_*_handler.\n\n"
                "Поддержаны обычные фильтры <code>commands</code>, <code>content_types</code> и <code>func</code>, "
                "а также InlineKeyboardButton/InlineKeyboardMarkup из <code>telebot.types</code>. "
                "Telegram-обработчики автоматически перестают выполняться при выключении или удалении плагина.\n\n"
                "При SETTINGS_PAGE=True карточка плагина показывает кнопку «Настройки» с callback "
                "<code>47:UUID:0</code>. Зарегистрируйте для неё callback-handler; префикс также доступен "
                "как <code>CBT.PLUGIN_SETTINGS</code> через <code>from tg_bot import CBT</code>.\n\n"
                "FunPay доступен через <code>cardinal.account</code>, Runner — через <code>cardinal.runner</code>."
            ),
            "safety": (
                "🛡 <b>Совместимость и безопасность</b>\n\n"
                "Поддерживается однофайловый контракт и 18 имён хуков FunPayCardinal, импорты <code>cardinal</code>, "
                "<code>FunPayAPI</code>, <code>tg_bot.CBT</code> и базовый слой <code>telebot.types</code>. "
                "Плагин, который импортирует дополнительные "
                "пакеты или внутренние модули конкретной сборки Cardinal, потребует добавить их в Docker-образ.\n\n"
                "⚠️ Python-плагин выполняется внутри процесса бота. Он может прочитать BOT_TOKEN, DATABASE_URL, "
                "golden_key, обращаться к сети и управлять аккаунтом. Проверяйте исходный код, UUID и автора. "
                "Выключение останавливает хуки, но для полного удаления недоверенного кода используйте кнопку «Удалить»."
            ),
            "catalog": (
                "🧭 <b>Публикация в каталоге</b>\n\n"
                "1. Сначала установите и проверьте собственный .py-плагин.\n"
                "2. Откройте «Мои плагины → плагин → Опубликовать в каталоге».\n"
                "3. Напишите подробное описание длиной 40–2000 символов: назначение, команды, "
                "настройка, зависимости, разрешения и ограничения.\n"
                "4. Проверьте карточку и подтвердите публикацию.\n\n"
                "При обновлении бот заново копирует метаданные и исходник текущей установленной версии. "
                "Чужой или официальный UUID перезаписать нельзя. Публикацию можно убрать; уже установленные "
                "копии у других пользователей сохранятся. Каталог сообщества не модерируется, поэтому "
                "перед установкой скачивайте и проверяйте исходный файл."
            ),
        }
        text = pages.get(page)
        if not text:
            await callback.message.answer("Раздел документации не найден.")
            return
        await callback.message.answer(
            text,
            reply_markup=keyboard([[("⬅️ Документация", "plugin_docs")]]),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data == "plugin_upload_warning")
    async def plugin_upload_warning(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "⚠️ <b>Внимание</b>\n"
            "Плагин выполняется с теми же правами, что и бот. Он может прочитать переменные окружения, "
            "получить доступ к FunPay-аккаунту и базе данных. Продолжайте только если доверяете автору.",
            reply_markup=keyboard([
                [("Я понимаю риск", "plugin_upload_confirm")],
                [("Отмена", "plugins")],
            ]),
        )

    @router.callback_query(F.data == "plugin_upload_confirm")
    async def plugin_upload_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.set_state(PluginState.file)
        await callback.message.answer(
            "Отправьте одиночный файл плагина <code>.py</code> до 512 КБ. Для отмены: /cancel"
        )

    @router.message(PluginState.file)
    async def plugin_upload_file(message: Message, state: FSMContext) -> None:
        document = message.document
        if not document or not (document.file_name or "").lower().endswith(".py"):
            await message.answer("❌ Отправьте документ с расширением .py.")
            return
        if document.file_size and document.file_size > 512 * 1024:
            await message.answer("❌ Размер плагина не должен превышать 512 КБ.")
            return
        runtime = await require_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        buffer = BytesIO()
        await message.bot.download(document, destination=buffer)
        try:
            source = buffer.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            await message.answer("❌ Файл должен быть текстовым UTF-8 Python-модулем.")
            return
        try:
            plugin = await manager.plugins.install(
                message.from_user.id,
                document.file_name,
                source,
                runtime,
            )
        except PluginValidationError as exc:
            await message.answer(f"❌ Плагин отклонён: {html.escape(str(exc))}")
            return
        except Exception as exc:
            logger.exception("Не удалось установить плагин")
            await message.answer(
                f"❌ Ошибка установки: {html.escape(clipped(exc, 600))}"
            )
            return
        await state.clear()
        await message.answer(
            f"✅ Плагин <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)} установлен."
        )
        await show_plugins(message, message.from_user.id)

    @router.callback_query(F.data.startswith("plugin_info:"))
    async def plugin_info(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        uuid = callback.data.split(":", 1)[1]
        plugin_runtime = manager.plugins.runtimes.get(callback.from_user.id)
        plugin = plugin_runtime.plugins.get(uuid) if plugin_runtime else None
        if not plugin:
            await callback.message.answer("Плагин не найден.")
            return
        hooks_count = sum(len(value) for value in plugin.hooks.values())
        publication = await db.get_catalog_plugin(uuid)
        rows = []
        settings_callback = plugin_settings_callback_data(plugin)
        if settings_callback:
            rows.append([("⚙️ Настройки", settings_callback)])
        publication_status = "Не опубликован"
        if publication:
            if publication["is_official"]:
                publication_status = "🛡 официальная публикация"
                rows.append([("🧭 Открыть в каталоге", f"catalog_view:{uuid}:0")])
            elif publication["owner_telegram_id"] == callback.from_user.id:
                publication_status = "✅ опубликован вами"
                rows.append([("📝 Обновить публикацию", f"catalog_publish_start:{uuid}")])
                rows.append([("Убрать из каталога", f"catalog_unpublish_ask:{uuid}")])
            else:
                publication_status = "Опубликован другим пользователем"
                rows.append([("🧭 Открыть в каталоге", f"catalog_view:{uuid}:0")])
        elif uuid not in READY_PLUGIN_BY_UUID:
            rows.append([("🌐 Опубликовать в каталоге", f"catalog_publish_start:{uuid}")])
        rows.extend([
            [("Выключить" if plugin.enabled else "Включить", f"plugin_toggle:{uuid}")],
            [("🗑 Удалить", f"plugin_delete_ask:{uuid}")],
            [("⬅️ Мои плагины", "my_plugins")],
        ])
        await callback.message.answer(
            f"🧩 <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)}\n"
            f"{html.escape(plugin.description)}\n\n"
            f"Автор: {html.escape(plugin.credits)}\n"
            f"UUID: <code>{plugin.uuid}</code>\n"
            f"Хуков: <b>{hooks_count}</b>\n"
            f"Страница настроек Cardinal: {bool_icon(plugin.settings_page)}\n"
            f"Каталог: {html.escape(publication_status)}\n"
            f"Состояние: {bool_icon(plugin.enabled)}",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data.startswith("catalog_publish_start:"))
    async def catalog_publish_start(callback: CallbackQuery, state: FSMContext) -> None:
        uuid = callback.data.split(":", 1)[1]
        plugin = await db.get_plugin(callback.from_user.id, uuid)
        publication = await db.get_catalog_plugin(uuid)
        if not plugin:
            await callback.answer("Установленный плагин не найден", show_alert=True)
            return
        if publication and (
            publication["is_official"]
            or publication["owner_telegram_id"] != callback.from_user.id
        ):
            await callback.answer("Этот UUID уже опубликован другим автором", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(CatalogPublishState.description)
        await state.update_data(catalog_uuid=uuid)
        current = (
            f"\n\nТекущее описание:\n<i>{html.escape(publication['description'])}</i>"
            if publication
            else ""
        )
        await callback.message.answer(
            "🌐 <b>Публикация плагина</b>\n\n"
            "Отправьте подробное описание: что делает плагин, как его настроить, какие команды "
            "он добавляет и какие разрешения или зависимости ему нужны. "
            f"От {PLUGIN_CATALOG_DESCRIPTION_MIN} до {PLUGIN_CATALOG_DESCRIPTION_MAX} символов."
            f"{current}\n\nДля отмены: /cancel"
        )

    @router.message(CatalogPublishState.description)
    async def catalog_publish_description(message: Message, state: FSMContext) -> None:
        try:
            description = validate_catalog_description(message.text or "")
        except ValueError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}")
            return
        data = await state.get_data()
        uuid = data.get("catalog_uuid")
        plugin = await db.get_plugin(message.from_user.id, uuid) if uuid else None
        if not plugin:
            await state.clear()
            await message.answer("❌ Плагин больше не установлен.")
            return
        await state.update_data(catalog_description=description)
        await message.answer(
            "🔎 <b>Предпросмотр публикации</b>\n\n"
            f"<b>{html.escape(clipped(plugin['name'], 100))}</b> "
            f"v{html.escape(clipped(plugin['version'], 30))}\n"
            f"<i>{html.escape(clipped(plugin['description'], 500))}</i>\n\n"
            f"{html.escape(description)}\n\n"
            f"Публичный автор: <b>{html.escape(telegram_publisher_name(message.from_user))}</b>\n"
            "После публикации любой пользователь бота сможет посмотреть и скачать исходник.",
            reply_markup=keyboard([
                [("✅ Опубликовать", f"catalog_publish_do:{uuid}")],
                [("Отмена", f"plugin_info:{uuid}")],
            ]),
        )

    @router.callback_query(F.data.startswith("catalog_publish_do:"))
    async def catalog_publish_do(callback: CallbackQuery, state: FSMContext) -> None:
        uuid = callback.data.split(":", 1)[1]
        data = await state.get_data()
        if data.get("catalog_uuid") != uuid or not data.get("catalog_description"):
            await callback.answer("Сессия публикации истекла", show_alert=True)
            return
        published = await db.publish_catalog_plugin(
            callback.from_user.id,
            uuid,
            telegram_publisher_name(callback.from_user),
            data["catalog_description"],
        )
        await state.clear()
        if not published:
            await callback.answer("Публикация не выполнена", show_alert=True)
            await callback.message.answer(
                "UUID уже занят либо установленный плагин был удалён."
            )
            return
        await callback.answer("Опубликовано")
        await callback.message.answer(
            "✅ Плагин опубликован в общем каталоге. Описание и исходник теперь видны другим пользователям.",
            reply_markup=keyboard([[('🧭 Открыть публикацию', f"catalog_view:{uuid}:0")]]),
        )

    @router.callback_query(F.data.startswith("catalog_unpublish_ask:"))
    async def catalog_unpublish_ask(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        item = await db.get_catalog_plugin(uuid)
        if not item or item["owner_telegram_id"] != callback.from_user.id or item["is_official"]:
            await callback.answer("Вы не можете удалить эту публикацию", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            f"Убрать <b>{html.escape(item['name'])}</b> из общего каталога? "
            "Уже установленные копии у других пользователей останутся.",
            reply_markup=keyboard([
                [("Да, убрать", f"catalog_unpublish_do:{uuid}")],
                [("Отмена", f"plugin_info:{uuid}")],
            ]),
        )

    @router.callback_query(F.data.startswith("catalog_unpublish_do:"))
    async def catalog_unpublish_do(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        removed = await db.unpublish_catalog_plugin(callback.from_user.id, uuid)
        await callback.answer("Публикация удалена" if removed else "Публикация не найдена")
        await callback.message.answer(
            (
                "✅ Плагин убран из каталога. Ваша установленная копия не изменена."
                if removed
                else "Публикация уже отсутствует или принадлежит другому пользователю."
            ),
            reply_markup=keyboard([[('⬅️ К плагину', f"plugin_info:{uuid}")]]),
        )

    @router.callback_query(F.data.startswith("plugin_toggle:"))
    async def plugin_toggle(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        try:
            enabled = await manager.plugins.toggle(callback.from_user.id, uuid)
        except KeyError:
            await callback.answer("Плагин не найден", show_alert=True)
            return
        await callback.answer("Плагин включён" if enabled else "Плагин выключен")
        await show_my_plugins(callback.message, callback.from_user.id)

    @router.callback_query(
        F.data.startswith(f"{PLUGIN_SETTINGS_CALLBACK_PREFIX}:")
    )
    async def external_plugin_settings(callback: CallbackQuery) -> None:
        parts = callback.data.split(":", 2)
        uuid = parts[1] if len(parts) == 3 else ""
        plugin_runtime = manager.plugins.runtimes.get(callback.from_user.id)
        plugin = plugin_runtime.plugins.get(uuid) if plugin_runtime else None
        if not plugin or not plugin.settings_page:
            await callback.answer("Страница настроек не найдена", show_alert=True)
            return
        if not plugin.enabled:
            await callback.answer(
                "Сначала включите плагин, затем откройте настройки.",
                show_alert=True,
            )
            return
        try:
            handled = await manager.plugins.dispatch_telegram_callback(
                callback.from_user.id, callback
            )
        except Exception:
            logger.exception("Ошибка страницы настроек плагина %s", uuid)
            await callback.answer(
                "Плагин завершил страницу настроек с ошибкой. Проверьте журнал.",
                show_alert=True,
            )
            return
        if not handled:
            await callback.answer(
                "Автор указал SETTINGS_PAGE=True, но не зарегистрировал обработчик настроек.",
                show_alert=True,
            )

    @router.callback_query(F.data.startswith("plugin_delete_ask:"))
    async def plugin_delete_ask(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        await callback.answer()
        await callback.message.answer(
            "Удалить плагин, его исходник и сохранённую запись?",
            reply_markup=keyboard([
                [("Да, удалить", f"plugin_delete_do:{uuid}")],
                [("Отмена", f"plugin_info:{uuid}")],
            ]),
        )

    @router.callback_query(F.data.startswith("plugin_delete_do:"))
    async def plugin_delete_do(callback: CallbackQuery) -> None:
        uuid = callback.data.split(":", 1)[1]
        await callback.answer("Удаляю…")
        try:
            await manager.plugins.delete(callback.from_user.id, uuid, callback)
        except Exception as exc:
            logger.exception("Ошибка обработчика удаления плагина")
            await callback.message.answer(
                f"❌ Плагин не удалён: {html.escape(clipped(exc, 500))}"
            )
            return
        await callback.message.answer("✅ Плагин удалён.")
        await show_my_plugins(callback.message, callback.from_user.id)

    async def require_builtin_plugin(
        target: Message, user_id: int, uuid: str
    ) -> AccountRuntime | None:
        runtime = await require_runtime(target, user_id)
        if not runtime:
            return None
        if not manager.plugins.is_enabled(user_id, uuid):
            await target.answer(
                "Плагин не установлен или выключен.",
                reply_markup=keyboard([[("🧭 Каталог плагинов", "plugin_catalog:0")]]),
            )
            return None
        return runtime

    async def show_auto_lots_plugin(target: Message, user_id: int) -> None:
        runtime = await require_builtin_plugin(target, user_id, AUTO_LOTS_PLUGIN_UUID)
        if not runtime:
            return
        try:
            _, common, currency = await asyncio.to_thread(
                load_lot_inventory, runtime.account
            )
        except Exception:
            logger.exception("Не удалось получить лоты для AutoLotsPlugin")
            await target.answer("❌ FunPay не отдал список лотов.")
            return
        all_lots = common + currency
        active = sum(bool(lot.active) for lot in all_lots)
        await target.answer(
            "🗂 <b>AutoLotsPlugin</b>\n\n"
            f"Всего предложений: <b>{len(all_lots)}</b>\n"
            f"Активно: <b>{active}</b> · выключено: <b>{len(all_lots) - active}</b>\n"
            f"Обычных лотов: <b>{len(common)}</b> · валютных: <b>{len(currency)}</b>\n\n"
            "Активация и деактивация применяются к обоим типам. При массовом удалении "
            "обычные лоты удаляются, а валютные предложения деактивируются, поскольку FunPay "
            "хранит их группами.",
            reply_markup=keyboard([
                [("✅ Активировать все", "ready_lots:activate")],
                [("⛔ Деактивировать все", "ready_lots:deactivate")],
                [("🗑 Удалить все", "ready_lots:delete_ask")],
                [("🔄 Обновить", f"builtin_open:{AUTO_LOTS_PLUGIN_UUID}")],
                [("⬅️ Мои плагины", "my_plugins")],
            ]),
        )

    async def show_status_plugin(target: Message, user_id: int) -> None:
        if not await require_builtin_plugin(target, user_id, STATUS_PLUGIN_UUID):
            return
        status_text = await db.get_plugin_setting(
            user_id,
            STATUS_PLUGIN_UUID,
            "status_text",
            "🟢 Продавец на связи. Можете оформлять заказ.",
        )
        await target.answer(
            "📡 <b>Status Plugin</b>\n\n"
            "Покупатель должен отправить в личном чате FunPay команду <code>#status</code>. "
            "Бот ответит следующим текстом:\n\n"
            f"<blockquote>{html.escape(status_text)}</blockquote>\n"
            "В тексте работают переменные автоответчика: $username, $chat_name, $account_name, $date и $time.",
            reply_markup=keyboard([
                [("✏️ Изменить статус", "ready_status:edit")],
                [("⬅️ Мои плагины", "my_plugins")],
            ]),
        )

    async def show_advanced_stats_plugin(target: Message, user_id: int) -> None:
        if not await require_builtin_plugin(
            target, user_id, ADVANCED_STATS_PLUGIN_UUID
        ):
            return
        await target.answer(
            "📈 <b>Advanced Profile Stats</b>\n\n"
            "Выберите период. Плагин посчитает продажи и выручку, затем добавит актуальный "
            "баланс, доступную к выводу сумму и средства на удержании.",
            reply_markup=keyboard([
                [("24 часа", "ready_stats:1"), ("7 дней", "ready_stats:7")],
                [("30 дней", "ready_stats:30"), ("90 дней", "ready_stats:90")],
                [("Год", "ready_stats:365"), ("Всё время", "ready_stats:all")],
                [("⬅️ Мои плагины", "my_plugins")],
            ]),
        )

    @router.callback_query(F.data.startswith("builtin_open:"))
    async def builtin_plugin_open(callback: CallbackQuery) -> None:
        await callback.answer()
        uuid = callback.data.split(":", 1)[1]
        if uuid == AUTO_LOTS_PLUGIN_UUID:
            await show_auto_lots_plugin(callback.message, callback.from_user.id)
        elif uuid == ADVANCED_STATS_PLUGIN_UUID:
            await show_advanced_stats_plugin(callback.message, callback.from_user.id)
        elif uuid == STATUS_PLUGIN_UUID:
            await show_status_plugin(callback.message, callback.from_user.id)
        else:
            await callback.message.answer("Для этого плагина нет встроенной страницы настроек.")

    @router.callback_query(F.data.startswith("ready_lots:"))
    async def ready_lots_action(callback: CallbackQuery) -> None:
        action = callback.data.split(":", 1)[1]
        runtime = await require_builtin_plugin(
            callback.message, callback.from_user.id, AUTO_LOTS_PLUGIN_UUID
        )
        if not runtime:
            await callback.answer()
            return
        if action == "delete_ask":
            await callback.answer()
            await callback.message.answer(
                "⚠️ <b>Удалить все обычные лоты?</b>\n\n"
                "Операция необратима. Валютные предложения будут деактивированы. "
                "Продолжить?",
                reply_markup=keyboard([
                    [("Да, удалить все", "ready_lots:delete")],
                    [("Отмена", f"builtin_open:{AUTO_LOTS_PLUGIN_UUID}")],
                ]),
            )
            return
        if action not in {"activate", "deactivate", "delete"}:
            await callback.answer("Неизвестное действие", show_alert=True)
            return
        await callback.answer("Выполняю…")
        progress = await callback.message.answer(
            "⏳ Обрабатываю лоты последовательно. Это может занять несколько минут."
        )
        try:
            result = await asyncio.to_thread(
                apply_bulk_lot_action, runtime.account, action
            )
        except Exception as exc:
            logger.exception("Ошибка массового управления лотами")
            await progress.edit_text(
                f"❌ Операция не выполнена: {html.escape(clipped(exc, 600))}"
            )
            return
        action_label = {
            "activate": "активировано",
            "deactivate": "деактивировано",
            "delete": "удалено/деактивировано",
        }[action]
        errors = "\n".join(
            f"• {html.escape(error)}" for error in result.errors[:10]
        )
        error_text = (
            f"\n\nОшибок: <b>{len(result.errors)}</b>\n{errors}"
            if result.errors
            else ""
        )
        await progress.edit_text(
            f"✅ Завершено: {action_label} <b>{result.changed}</b> предложений.\n"
            f"Найдено обычных: {result.common_total}, валютных: {result.currency_total}"
            f"{error_text}",
            reply_markup=keyboard([
                [("🔄 Обновить список", f"builtin_open:{AUTO_LOTS_PLUGIN_UUID}")]
            ]),
        )

    @router.callback_query(F.data.startswith("ready_stats:"))
    async def ready_stats_period(callback: CallbackQuery) -> None:
        runtime = await require_builtin_plugin(
            callback.message, callback.from_user.id, ADVANCED_STATS_PLUGIN_UUID
        )
        if not runtime:
            await callback.answer()
            return
        raw_period = callback.data.split(":", 1)[1]
        days = None if raw_period == "all" else int(raw_period)
        await callback.answer("Собираю статистику…")
        try:
            stats = await asyncio.to_thread(load_sales_stats, runtime.account, days)
        except Exception:
            logger.exception("Advanced Profile Stats не получил продажи")
            await callback.message.answer("❌ FunPay не отдал историю продаж.")
            return
        try:
            balance = await asyncio.to_thread(load_detailed_balance, runtime.account)
            balance_text = (
                "\n\n💳 <b>Средства</b>\n"
                f"Можно вывести: <b>{format_money(balance.available_rub)} ₽ · "
                f"{format_money(balance.available_usd)} $ · {format_money(balance.available_eur)} €</b>\n"
                f"На удержании: {format_money(balance.total_rub - balance.available_rub)} ₽ · "
                f"{format_money(balance.total_usd - balance.available_usd)} $ · "
                f"{format_money(balance.total_eur - balance.available_eur)} €\n"
                f"Всего: {format_money(balance.total_rub)} ₽ · {format_money(balance.total_usd)} $ · "
                f"{format_money(balance.total_eur)} €"
            )
        except Exception:
            logger.exception("Advanced Profile Stats не получил баланс")
            balance_text = "\n\n⚠️ Не удалось загрузить подробный баланс."
        await callback.message.answer(
            format_sales_stats(stats) + balance_text,
            reply_markup=keyboard([
                [("📅 Другой период", f"builtin_open:{ADVANCED_STATS_PLUGIN_UUID}")]
            ]),
        )

    @router.callback_query(F.data == "ready_status:edit")
    async def ready_status_edit(callback: CallbackQuery, state: FSMContext) -> None:
        if not await require_builtin_plugin(
            callback.message, callback.from_user.id, STATUS_PLUGIN_UUID
        ):
            await callback.answer()
            return
        await callback.answer()
        await state.clear()
        await state.set_state(StatusPluginState.text)
        await callback.message.answer(
            "Отправьте новый текст статуса: от 1 до 600 символов. "
            "Разрешены переменные автоответчика. Для отмены: /cancel"
        )

    @router.message(StatusPluginState.text, F.text)
    async def ready_status_save(message: Message, state: FSMContext) -> None:
        if not manager.plugins.is_enabled(message.from_user.id, STATUS_PLUGIN_UUID):
            await state.clear()
            await message.answer("Status Plugin выключен или удалён.")
            return
        value = message.text.strip()
        if not 1 <= len(value) <= 600:
            await message.answer("Текст должен содержать от 1 до 600 символов.")
            return
        await db.set_plugin_setting(
            message.from_user.id, STATUS_PLUGIN_UUID, "status_text", value
        )
        await state.clear()
        await message.answer("✅ Статус сохранён.")
        await show_status_plugin(message, message.from_user.id)

    async def show_chat_carousel(target: Message, user_id: int, index: int) -> None:
        runtime = await require_runtime(target, user_id)
        if not runtime:
            return
        try:
            chats_map = await asyncio.to_thread(runtime.account.get_chats, True)
        except Exception:
            logger.exception("Не удалось получить чаты")
            await target.answer("❌ Не удалось получить список чатов.")
            return
        chats_list = list(chats_map.values())
        if not chats_list:
            await target.answer("Чатов пока нет.", reply_markup=keyboard([[("⬅️ Меню", "menu")]]))
            return
        index %= len(chats_list)
        chat = chats_list[index]
        previous_index = (index - 1) % len(chats_list)
        next_index = (index + 1) % len(chats_list)
        unread = "🟠 есть непрочитанные" if chat.unread else "⚪ прочитан"
        text = (
            f"💬 <b>{html.escape(chat.name or '—')}</b>\n"
            f"Чат: <code>{chat.id}</code> · {unread}\n"
            f"Позиция: <b>{index + 1}/{len(chats_list)}</b>\n\n"
            f"<pre>{html.escape(clipped(chat.last_message_text or '[изображение]', 1800))}</pre>"
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️", callback_data=f"chat_view:{previous_index}"),
                InlineKeyboardButton(text=f"{index + 1}/{len(chats_list)}", callback_data="noop"),
                InlineKeyboardButton(text="➡️", callback_data=f"chat_view:{next_index}"),
            ],
            [InlineKeyboardButton(text="📖 Весь красивый чат", callback_data=f"chat_full:{chat.id}:{index}")],
            [
                InlineKeyboardButton(text="↩️ Ответить", callback_data=f"reply:{chat.id}"),
                InlineKeyboardButton(text="📷 Фото", callback_data=f"image_chat:{chat.id}"),
            ],
            [
                InlineKeyboardButton(text="🌐 FunPay", url=f"https://funpay.com/chat/?node={chat.id}"),
                InlineKeyboardButton(text="⬅️ Меню", callback_data="menu"),
            ],
        ])
        try:
            await target.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.answer(text, reply_markup=markup)

    @router.callback_query(F.data == "chats")
    async def chats(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю…")
        await show_chat_carousel(callback.message, callback.from_user.id, 0)

    @router.callback_query(F.data.startswith("chat_view:"))
    async def chat_view(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_chat_carousel(callback.message, callback.from_user.id, int(callback.data.split(":")[1]))

    @router.callback_query(F.data.startswith("chat_full:"))
    async def chat_full(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю историю…")
        _, raw_chat_id, raw_index = callback.data.split(":", 2)
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            chat, truncated = await asyncio.to_thread(
                load_full_chat, runtime.account, int(raw_chat_id)
            )
            chunks = format_chat_history(chat, runtime.account.id)
        except Exception:
            logger.exception("Не удалось получить полную историю чата %s", raw_chat_id)
            await callback.message.answer("❌ FunPay не отдал историю этого чата.")
            return
        if truncated:
            await callback.message.answer(
                "ℹ️ Показаны последние 2000 сообщений: это защитный лимит для очень длинных чатов."
            )
        for index, chunk in enumerate(chunks):
            markup = None
            if index == len(chunks) - 1:
                markup = keyboard([
                    [("↩️ Ответить", f"reply:{raw_chat_id}"), ("📷 Фото", f"image_chat:{raw_chat_id}")],
                    [("↩️ К карточке чата", f"chat_view:{raw_index}")],
                ])
            await callback.message.answer(chunk, reply_markup=markup, disable_web_page_preview=True)

    @router.callback_query(F.data.startswith("reply:"))
    async def reply_from_notification(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        chat_id = callback.data.split(":", 1)[1]
        if not chat_id.isdigit() or not await require_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.set_state(SendMessageState.text)
        await state.update_data(chat_id=int(chat_id))
        await callback.message.answer(
            "Введите ответ покупателю. Можно использовать переменные из раздела автоответчика или /cancel."
        )

    @router.callback_query(F.data == "send_message")
    async def send_message_begin(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.set_state(SendMessageState.chat_id)
        await callback.message.answer("Введите числовой ID чата из раздела «Последние чаты» или /cancel.")

    @router.message(SendMessageState.chat_id, F.text)
    async def send_message_chat(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not value.isdigit():
            await message.answer("ID чата должен состоять из цифр.")
            return
        await state.update_data(chat_id=int(value))
        await state.set_state(SendMessageState.text)
        await message.answer("Теперь отправьте текст сообщения (до 4000 символов).")

    @router.message(SendMessageState.text, F.text)
    async def send_message_text(message: Message, state: FSMContext) -> None:
        value = message.text.strip()
        if not value or len(value) > 4000:
            await message.answer("Текст должен содержать от 1 до 4000 символов.")
            return
        runtime = await require_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        data = await state.get_data()
        chat = runtime.account.get_chat_by_id(data["chat_id"])
        order = None
        if data.get("order_id"):
            try:
                order = await asyncio.to_thread(runtime.account.get_order, data["order_id"])
            except Exception:
                logger.warning(
                    "Не удалось загрузить заказ %s для переменных сообщения",
                    data["order_id"],
                    exc_info=True,
                )
        rendered = render_template(
            value,
            account=runtime.account,
            chat_id=data["chat_id"],
            chat_name=chat.name if chat else None,
            order=order,
        )
        try:
            await asyncio.to_thread(
                runtime.account.send_message,
                data["chat_id"],
                rendered,
                chat.name if chat else None,
            )
        except Exception as exc:
            logger.exception("Ручное сообщение не отправлено")
            await message.answer(f"❌ FunPay не отправил сообщение: {html.escape(clipped(exc, 300))}")
            return
        chat_id = data["chat_id"]
        await state.clear()
        await message.answer(
            "✅ Сообщение отправлено. Можно продолжить переписку или открыть весь диалог.",
            reply_markup=conversation_actions_keyboard(chat_id),
        )

    async def ask_for_image(target: Message, state: FSMContext, state_value: State, destination: str) -> None:
        await state.set_state(state_value)
        await target.answer(
            f"Отправьте изображение для {destination} как фото или графический файл до 20 МБ. Для отмены: /cancel"
        )

    async def download_telegram_image(message: Message) -> bytes | None:
        file_obj = message.photo[-1] if message.photo else message.document
        if not file_obj:
            await message.answer("❌ Отправьте фото или файл изображения.")
            return None
        if message.document and not (message.document.mime_type or "").startswith("image/"):
            await message.answer("❌ Документ должен быть изображением PNG, JPG, WEBP или GIF.")
            return None
        if file_obj.file_size and file_obj.file_size >= 20 * 1024 * 1024:
            await message.answer("❌ Размер изображения должен быть меньше 20 МБ.")
            return None
        buffer = BytesIO()
        await message.bot.download(file_obj, destination=buffer)
        return buffer.getvalue()

    @router.callback_query(F.data == "images")
    async def images_menu(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "🖼 <b>Изображения FunPay</b>\n\n"
            "Фото можно сразу отправить покупателю либо загрузить и прикрепить к существующему лоту.",
            reply_markup=keyboard([
                [("💬 Отправить в чат", "image_chat_begin")],
                [("🛒 Добавить в лот", "image_lot_begin")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "image_chat_begin")
    async def image_chat_begin(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.set_state(UploadImageState.chat_id)
        await callback.message.answer("Введите числовой ID чата, куда отправить изображение.")

    @router.message(UploadImageState.chat_id, F.text)
    async def image_chat_id(message: Message, state: FSMContext) -> None:
        if not message.text.strip().isdigit():
            await message.answer("ID чата должен состоять из цифр.")
            return
        await state.update_data(chat_id=int(message.text.strip()))
        await ask_for_image(message, state, UploadImageState.chat_file, "чата")

    @router.callback_query(F.data.startswith("image_chat:"))
    async def image_chat_direct(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        chat_id = callback.data.split(":", 1)[1]
        if not chat_id.isdigit():
            return
        await state.update_data(chat_id=int(chat_id))
        await ask_for_image(callback.message, state, UploadImageState.chat_file, "чата")

    @router.message(UploadImageState.chat_file)
    async def image_chat_file(message: Message, state: FSMContext) -> None:
        runtime = await require_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        image_data = await download_telegram_image(message)
        if image_data is None:
            return
        data = await state.get_data()
        try:
            await asyncio.to_thread(runtime.account.send_image, data["chat_id"], image_data)
        except Exception as exc:
            logger.exception("Не удалось отправить изображение в чат")
            await message.answer(f"❌ FunPay не принял изображение: {html.escape(clipped(exc, 400))}")
            return
        chat_id = data["chat_id"]
        await state.clear()
        await message.answer(
            "✅ Изображение отправлено в чат FunPay.",
            reply_markup=conversation_actions_keyboard(chat_id),
        )

    @router.callback_query(F.data == "image_lot_begin")
    async def image_lot_begin(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.set_state(UploadImageState.lot_id)
        await callback.message.answer("Введите ID существующего лота, к которому добавить изображение.")

    @router.message(UploadImageState.lot_id, F.text)
    async def image_lot_id(message: Message, state: FSMContext) -> None:
        if not message.text.strip().isdigit():
            await message.answer("ID лота должен состоять из цифр.")
            return
        await state.update_data(lot_id=int(message.text.strip()))
        await ask_for_image(message, state, UploadImageState.lot_file, "лота")

    @router.message(UploadImageState.lot_file)
    async def image_lot_file(message: Message, state: FSMContext) -> None:
        runtime = await require_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        image_data = await download_telegram_image(message)
        if image_data is None:
            return
        data = await state.get_data()
        try:
            image_id = await asyncio.to_thread(runtime.account.upload_image, image_data, "offer")
            lot_fields = await asyncio.to_thread(runtime.account.get_lot_fields, data["lot_id"])
            if image_id not in lot_fields.images:
                lot_fields.images.append(image_id)
            await asyncio.to_thread(runtime.account.save_lot, lot_fields.renew_fields())
        except Exception as exc:
            logger.exception("Не удалось добавить изображение к лоту")
            await message.answer(f"❌ Не удалось обновить лот: {html.escape(clipped(exc, 400))}")
            return
        await state.clear()
        await message.answer(
            f"✅ Изображение <code>{image_id}</code> добавлено к лоту <code>{data['lot_id']}</code>.",
            reply_markup=main_keyboard(),
        )

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    async def show_order(target: Message, user_id: int, order_id: str) -> None:
        runtime = await require_runtime(target, user_id)
        if not runtime:
            return
        try:
            order = await asyncio.to_thread(runtime.account.get_order, order_id)
            if runtime.account.id not in {order.seller_id, order.buyer_id}:
                raise RuntimeError("заказ не принадлежит подключённому аккаунту")
        except Exception as exc:
            logger.exception("Не удалось получить заказ %s", order_id)
            await target.answer(
                f"❌ Не удалось загрузить заказ: {html.escape(clipped(exc, 400))}",
                reply_markup=keyboard([[("⬅️ Меню", "menu")]]),
            )
            return

        buttons: list[list[InlineKeyboardButton]] = []
        if str(order.chat_id).isdigit():
            buttons.append([
                InlineKeyboardButton(
                    text="↩️ Ответить", callback_data=f"order_reply:{order.id}"
                ),
                InlineKeyboardButton(text="💬 Весь чат", callback_data=f"chat_full:{order.chat_id}:0"),
            ])
        if order.seller_id == runtime.account.id and order.status is types.OrderStatuses.PAID:
            buttons.append([
                InlineKeyboardButton(text="💸 Вернуть деньги", callback_data=f"refund_ask:{order.id}")
            ])
        buttons.append([
            InlineKeyboardButton(text="🌐 Открыть на FunPay", url=f"https://funpay.com/orders/{order.id}/")
        ])
        buttons.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")])
        await target.answer(
            format_order(order),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            disable_web_page_preview=True,
        )

    @router.callback_query(F.data == "order_lookup")
    async def order_lookup(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_runtime(callback.message, callback.from_user.id):
            return
        await state.set_state(OrderState.order_id)
        await callback.message.answer("Введите ID заказа без символа #. Для отмены: /cancel")

    @router.message(OrderState.order_id, F.text)
    async def order_lookup_id(message: Message, state: FSMContext) -> None:
        order_id = message.text.strip().removeprefix("#")
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", order_id):
            await message.answer("ID заказа выглядит некорректно. Введите его ещё раз или нажмите /cancel.")
            return
        await state.clear()
        await show_order(message, message.from_user.id, order_id)

    @router.callback_query(F.data.startswith("order_view:"))
    async def order_view(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю заказ…")
        order_id = callback.data.split(":", 1)[1]
        await show_order(callback.message, callback.from_user.id, order_id)

    @router.callback_query(F.data.startswith("order_reply:"))
    async def order_reply(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer("Загружаю заказ…")
        order_id = callback.data.split(":", 1)[1]
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            order = await asyncio.to_thread(runtime.account.get_order, order_id)
            if runtime.account.id not in {order.seller_id, order.buyer_id}:
                raise RuntimeError("заказ не принадлежит подключённому аккаунту")
            if not str(order.chat_id).isdigit():
                raise RuntimeError("у заказа нет личного чата")
        except Exception as exc:
            logger.warning("Не удалось открыть ответ по заказу %s", order_id, exc_info=True)
            await callback.message.answer(
                f"❌ Нельзя открыть ответ по заказу: {html.escape(clipped(exc, 400))}"
            )
            return
        await state.clear()
        await state.set_state(SendMessageState.text)
        await state.update_data(chat_id=int(order.chat_id), order_id=order.id)
        await callback.message.answer(
            "Введите ответ покупателю. Переменные заказа и чата будут подставлены автоматически."
        )

    @router.callback_query(F.data.startswith("review_manual:"))
    async def review_manual(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        order_id = callback.data.split(":", 1)[1]
        if not await require_runtime(callback.message, callback.from_user.id):
            return
        await state.clear()
        await state.update_data(order_id=order_id)
        await state.set_state(ReviewReplyState.text)
        await callback.message.answer(
            "Введите ответ на отзыв до 999 символов. Можно использовать переменные. Для отмены: /cancel"
        )

    @router.message(ReviewReplyState.text, F.text)
    async def review_manual_text(message: Message, state: FSMContext) -> None:
        runtime = await require_runtime(message, message.from_user.id)
        if not runtime:
            await state.clear()
            return
        data = await state.get_data()
        try:
            order = await asyncio.to_thread(runtime.account.get_order, data["order_id"])
            if order.seller_id != runtime.account.id or not order.review:
                raise RuntimeError("у заказа нет доступного отзыва покупателя")
            text = normalize_review_reply(
                render_template(
                    message.text,
                    order=order,
                    review=order.review,
                    account=runtime.account,
                )
            )
            if not text:
                raise RuntimeError("ответ не может быть пустым")
            await asyncio.to_thread(runtime.account.send_review, order.id, text)
        except Exception as exc:
            logger.exception("Не удалось отправить ручной ответ на отзыв")
            await message.answer(
                f"❌ Ответ не отправлен: {html.escape(clipped(exc, 500))}"
            )
            return
        order_id = data["order_id"]
        await state.clear()
        await message.answer(
            "✅ Ответ на отзыв отправлен.",
            reply_markup=keyboard([
                [("📦 Открыть заказ", f"order_view:{order_id}")],
                [("⭐ Настройки автоответов", "review_replies")],
            ]),
        )

    @router.callback_query(F.data.startswith("refund_ask:"))
    async def refund_ask(callback: CallbackQuery) -> None:
        order_id = callback.data.split(":", 1)[1]
        await callback.answer()
        await callback.message.answer(
            f"⚠️ Вернуть покупателю всю сумму заказа <code>{html.escape(order_id)}</code>?\n"
            "Действие выполняется на FunPay и необратимо.",
            reply_markup=keyboard([
                [("Да, вернуть деньги", f"refund_do:{order_id}")],
                [("Отмена", f"refund_cancel:{order_id}")],
            ]),
        )

    @router.callback_query(F.data.startswith("refund_cancel:"))
    async def refund_cancel(callback: CallbackQuery) -> None:
        await callback.answer("Возврат отменён")
        await callback.message.edit_text("Возврат отменён.")

    @router.callback_query(F.data.startswith("refund_do:"))
    async def refund_do(callback: CallbackQuery) -> None:
        order_id = callback.data.split(":", 1)[1]
        await callback.answer("Проверяю заказ…")
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            order = await asyncio.to_thread(runtime.account.get_order, order_id)
            if order.seller_id != runtime.account.id:
                raise RuntimeError("заказ не принадлежит подключённому продавцу")
            if order.status is not types.OrderStatuses.PAID:
                raise RuntimeError(f"возврат недоступен для статуса {order.status.name}")
            await asyncio.to_thread(runtime.account.refund, order_id)
        except Exception as exc:
            logger.exception("Возврат заказа %s не выполнен", order_id)
            await callback.message.edit_text(
                f"❌ Возврат заказа <code>{html.escape(order_id)}</code> не выполнен: "
                f"{html.escape(clipped(exc, 500))}"
            )
            return
        await callback.message.edit_text(
            f"✅ Деньги по заказу <code>{html.escape(order_id)}</code> возвращены покупателю.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🌐 Открыть заказ", url=f"https://funpay.com/orders/{order_id}/")
            ]]),
        )

    async def show_account(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        if not row or not row["account_active"]:
            await target.answer("Аккаунт не подключён.", reply_markup=keyboard([[("🔗 Подключить", "connect")]]))
            return
        try:
            proxy = proxy_label(secrets.decrypt(row["proxy_enc"]))
        except (InvalidToken, ValueError, TypeError):
            proxy = "не удалось расшифровать"
        status = "🟢 работает" if manager.get(user_id) else "🔴 остановлен"
        await target.answer(
            "⚙️ <b>Аккаунт</b>\n"
            f"FunPay: <b>{html.escape(row['funpay_username'] or '—')}</b> (<code>{row['funpay_id']}</code>)\n"
            f"Прокси: <code>{html.escape(proxy)}</code>\n"
            f"Runner: {status}\n"
            f"Вечный онлайн / обновление сессии: {bool_icon(row['keep_online_enabled'])}",
            reply_markup=keyboard([
                [(f"{bool_icon(row['keep_online_enabled'])} Поддерживать сессию", "toggle:keep_online_enabled")],
                [("🔄 Переподключить", "reconnect"), ("🔑 Изменить данные", "connect")],
                [("🗑 Отключить аккаунт", "disconnect_confirm")],
                [("⬅️ Меню", "menu")],
            ]),
        )

    @router.callback_query(F.data == "account")
    async def account(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_account(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "reconnect")
    async def reconnect(callback: CallbackQuery) -> None:
        await callback.answer("Переподключаю…")
        try:
            await manager.start(callback.from_user.id)
        except Exception:
            logger.exception("Переподключение не удалось")
            await callback.message.answer("❌ Переподключиться не удалось. Проверьте прокси и golden_key.")
            return
        await callback.message.answer("✅ Подключение восстановлено.", reply_markup=main_keyboard())

    @router.callback_query(F.data == "disconnect_confirm")
    async def disconnect_confirm(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "Удалить сохранённые прокси и golden_key и остановить автоматизацию?",
            reply_markup=keyboard([[("Да, отключить", "disconnect"), ("Нет", "account")]]),
        )

    @router.callback_query(F.data == "disconnect")
    async def disconnect(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await manager.stop(callback.from_user.id)
        await db.disconnect_account(callback.from_user.id)
        await state.clear()
        await callback.message.answer(
            "Аккаунт отключён, сохранённые прокси и golden_key удалены.",
            reply_markup=keyboard([[("🔗 Подключить заново", "connect")]]),
        )

    @router.message()
    async def fallback(message: Message) -> None:
        try:
            if await manager.plugins.dispatch_telegram_message(
                message.from_user.id, message
            ):
                return
        except Exception:
            logger.exception("Ошибка Telegram-хэндлера плагина")
        await show_main(message, message.from_user.id)

    @router.callback_query()
    async def plugin_callback_fallback(callback: CallbackQuery) -> None:
        try:
            if await manager.plugins.dispatch_telegram_callback(
                callback.from_user.id, callback
            ):
                return
        except Exception:
            logger.exception("Ошибка callback-хэндлера плагина")
        await callback.answer("Действие устарело или не поддерживается", show_alert=True)

    return router


async def main() -> None:
    config = Config.from_env()
    db = Database(config.database_url)
    await db.connect()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    secrets = SecretBox(config.app_secret)
    manager = RuntimeManager(bot, db, secrets)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(db, manager, secrets))
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ])
    await manager.start_saved()
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await manager.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
