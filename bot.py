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
from typing import Any
from urllib.parse import urlsplit

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from cryptography.fernet import Fernet, InvalidToken

from FunPayAPI import Account, Runner, events, types
from FunPayAPI import exceptions as fp_exceptions
from plugin_system import PluginData, PluginManager, PluginValidationError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("funpay_bot")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_reviews BOOLEAN NOT NULL DEFAULT TRUE;
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
            """
        )

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
                   account_active=TRUE, updated_at=NOW()
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
                   account_active=FALSE, updated_at=NOW()
             WHERE telegram_id=$1
            """,
            telegram_id,
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

    async def set_integer_setting(self, telegram_id: int, column: str, value: int) -> None:
        allowed = {
            "autoreply_cooldown_minutes": (0, 1440),
            "autoreply_delay_seconds": (0, 300),
            "autoreply_work_start": (0, 23),
            "autoreply_work_end": (1, 24),
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

    async def list_plugins(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.fetch(
            "SELECT * FROM funpay_plugins WHERE telegram_id=$1 ORDER BY uploaded_at, name",
            telegram_id,
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

    async def active_users(self) -> list[asyncpg.Record]:
        return await self.fetch(
            """
            SELECT * FROM funpay_users
             WHERE account_active=TRUE AND proxy_enc IS NOT NULL AND golden_key_enc IS NOT NULL
            """
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
        if item.author_id == 0:
            icon, author = "⚙️", "FunPay"
        elif item.author_id == account_id or item.by_bot or item.by_vertex:
            icon, author = "🟢", item.author or "Вы"
        else:
            icon, author = "🔵", item.author or chat.name or "Покупатель"
        body = item.text or (f'<a href="{html.escape(item.image_link, quote=True)}">Изображение</a>' if item.image_link else "[без текста]")
        if item.text:
            body = html.escape(clipped(body, 2600))
        blocks.append(f"{icon} <b>{html.escape(author)}</b>\n<blockquote>{body}</blockquote>")
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


class RuntimeManager:
    def __init__(self, bot: Bot, db: Database, secrets: SecretBox):
        self.bot = bot
        self.db = db
        self.secrets = secrets
        self.runtimes: dict[int, AccountRuntime] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.plugins = PluginManager(db, bot)

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

    async def close(self) -> None:
        for telegram_id in list(self.runtimes):
            await self.stop(telegram_id)

    def get(self, telegram_id: int) -> AccountRuntime | None:
        return self.runtimes.get(telegram_id)

    async def safe_notify(self, telegram_id: int, text: str, **kwargs: Any) -> None:
        try:
            await self.bot.send_message(telegram_id, text, **kwargs)
        except Exception:
            logger.exception("Не удалось отправить Telegram-уведомление пользователю %s", telegram_id)

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
            text = (
                "⭐ <b>Отзыв о заказе</b>\n"
                f"Заказ: <code>{html.escape(order_id or '—')}</code>\n"
                f"Лот: {html.escape(clipped(title, 900))}\n"
                f"Оценка: <b>{rating}</b>\n"
                f"Комментарий: <blockquote>{html.escape(clipped(comment, 1500))}</blockquote>"
            )
            if reply_text:
                text += f"\n🤖 Ответ отправлен: <i>{html.escape(reply_text)}</i>"
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
                    f"💬 <b>Новое сообщение от {chat_name}</b>\n{body}\n\n"
                    f"Чат: <code>{html.escape(chat_id)}</code>",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="↩️ Ответить", callback_data=f"reply:{chat_id}"),
                            InlineKeyboardButton(text="💬 Весь чат", callback_data=f"chat_full:{chat_id}:0"),
                        ],
                        [InlineKeyboardButton(text="🌐 FunPay", url=f"https://funpay.com/chat/?node={chat_id}")],
                    ]),
                )
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
        elif isinstance(event, events.NewOrderEvent) and row["notify_new_orders"]:
            order = event.order
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
                "🛒 <b>Новый заказ</b>\n"
                f"ID: <code>{html.escape(order.id)}</code>\n"
                f"Покупатель: {html.escape(order.buyer_username or '—')}\n"
                f"Сумма: <b>{order.price} {html.escape(str(order.currency))}</b>\n"
                f"Товар: {html.escape(clipped(order.description))}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        elif isinstance(event, events.OrderStatusChangedEvent) and row["notify_order_status"]:
            order = event.order
            await self.safe_notify(
                runtime.telegram_id,
                f"📦 Статус заказа <code>{html.escape(order.id)}</code>: "
                f"<b>{html.escape(order_status_label(order.status))}</b>.\n"
                f"Лот: {html.escape(clipped(order.description or '—', 1200))}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📦 Подробности", callback_data=f"order_view:{order.id}"),
                    InlineKeyboardButton(text="🌐 FunPay", url=f"https://funpay.com/orders/{order.id}/"),
                ]]),
            )


class ConnectState(StatesGroup):
    proxy = State()
    golden_key = State()


class AutoReplyState(StatesGroup):
    text = State()
    cooldown = State()
    delay = State()
    hours = State()
    review_text = State()


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


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return keyboard([
        [("👤 Подробный профиль", "profile"), ("💰 Баланс", "balance")],
        [("🔔 Уведомления", "notifications"), ("🤖 Автоответчик", "autoreply")],
        [("💬 Последние чаты", "chats"), ("📦 Заказ по ID", "order_lookup")],
        [("🆙 Автоподнятие", "auto_raise"), ("🧩 Плагины", "plugins")],
        [("⚙️ Аккаунт", "account")],
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
        if not row or not row["account_active"]:
            await target.answer(
                "Аккаунт FunPay пока не подключён.",
                reply_markup=keyboard([[("🔗 Подключить", "connect")]]),
            )
            return
        online = "🟢" if manager.get(user_id) else "🔴"
        await target.answer(
            f"{online} <b>{html.escape(row['funpay_username'] or 'FunPay')}</b>\n{text}",
            reply_markup=main_keyboard(),
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

    async def begin_connect(target: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(ConnectState.proxy)
        await target.answer(
            "1/2. Отправьте прокси. Поддерживаемые форматы:\n"
            "<code>http://user:password@host:port</code>\n"
            "<code>socks5://user:password@host:port</code>\n\n"
            "Сообщение будет удалено после обработки. Для отмены: /cancel"
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await db.ensure_user(message.from_user.id)
        row = await db.get_user(message.from_user.id)
        if row and row["account_active"]:
            if not manager.get(message.from_user.id):
                try:
                    await manager.start(message.from_user.id, row=row)
                except Exception:
                    logger.exception("Ручной запуск аккаунта не удался")
            await show_main(message, message.from_user.id)
        else:
            await begin_connect(message, state)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_main(message, message.from_user.id, "Текущее действие отменено.")

    @router.callback_query(F.data == "connect")
    async def connect_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await begin_connect(callback.message, state)

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
        else:
            await show_notifications(callback.message, callback.from_user.id)

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
            f"{'⭐' * stars}: <i>{html.escape(clipped(row[f'review_reply_{stars}'], 150))}</i>"
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
                [("🧩 Переменные", "variables"), ("⬅️ Назад", "autoreply")],
            ]),
        )

    @router.callback_query(F.data == "review_replies")
    async def review_replies(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_review_replies(callback.message, callback.from_user.id)

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

    async def show_plugins(target: Message, user_id: int) -> None:
        runtime = await require_runtime(target, user_id)
        if not runtime:
            return
        plugin_runtime = manager.plugins.runtimes.get(user_id)
        plugins = list(plugin_runtime.plugins.values()) if plugin_runtime else []
        rows = [
            [(f"{'✅' if plugin.enabled else '❌'} {clipped(plugin.name, 28)} v{clipped(plugin.version, 12)}", f"plugin_info:{plugin.uuid}")]
            for plugin in plugins
        ]
        rows.extend([
            [("➕ Загрузить плагин", "plugin_upload_warning")],
            [("📚 Документация", "plugin_docs")],
            [("⬅️ Меню", "menu")],
        ])
        await target.answer(
            "🧩 <b>Плагины FunPayCardinal</b>\n"
            f"Установлено: <b>{len(plugins)}</b>\n\n"
            "Поддерживаются одиночные .py-плагины с метаданными и списками хуков Cardinal.",
            reply_markup=keyboard(rows),
        )

    @router.callback_query(F.data == "plugins")
    async def plugins(callback: CallbackQuery) -> None:
        await callback.answer()
        await show_plugins(callback.message, callback.from_user.id)

    @router.callback_query(F.data == "plugin_docs")
    async def plugin_docs(callback: CallbackQuery) -> None:
        await callback.answer()
        await callback.message.answer(
            "📚 <b>Формат плагинов FunPayCardinal</b>\n\n"
            "Файл: одиночный UTF-8 <code>.py</code> до 512 КБ. Обязательные поля:\n"
            "<code>NAME, VERSION, DESCRIPTION, CREDITS, SETTINGS_PAGE, UUID, BIND_TO_DELETE</code>.\n\n"
            "Поддерживаемые хуки:\n"
            "<code>BIND_TO_PRE_INIT, BIND_TO_POST_INIT, BIND_TO_PRE_START, BIND_TO_POST_START, "
            "BIND_TO_PRE_STOP, BIND_TO_POST_STOP, BIND_TO_INIT_MESSAGE, "
            "BIND_TO_MESSAGES_LIST_CHANGED, BIND_TO_LAST_CHAT_MESSAGE_CHANGED, "
            "BIND_TO_NEW_MESSAGE, BIND_TO_INIT_ORDER, BIND_TO_NEW_ORDER, "
            "BIND_TO_ORDERS_LIST_CHANGED, BIND_TO_ORDER_STATUS_CHANGED, "
            "BIND_TO_PRE_DELIVERY, BIND_TO_POST_DELIVERY, BIND_TO_PRE_LOTS_RAISE, "
            "BIND_TO_POST_LOTS_RAISE</code>.\n\n"
            "Функции событий получают <code>(cardinal, event)</code>. Доступны импорты "
            "<code>from cardinal import Cardinal, get_cardinal</code> и встроенный FunPayAPI.\n\n"
            "⚠️ Плагин — произвольный Python-код и имеет доступ к аккаунту, окружению и процессу. "
            "Устанавливайте только проверенные файлы.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🌐 Исходный FunPayCardinal",
                    url="https://github.com/sidor0912/FunPayCardinal",
                )],
                [InlineKeyboardButton(text="⬅️ Плагины", callback_data="plugins")],
            ]),
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
    async def plugin_info(callback: CallbackQuery) -> None:
        await callback.answer()
        uuid = callback.data.split(":", 1)[1]
        plugin_runtime = manager.plugins.runtimes.get(callback.from_user.id)
        plugin = plugin_runtime.plugins.get(uuid) if plugin_runtime else None
        if not plugin:
            await callback.message.answer("Плагин не найден.")
            return
        hooks_count = sum(len(value) for value in plugin.hooks.values())
        await callback.message.answer(
            f"🧩 <b>{html.escape(plugin.name)}</b> v{html.escape(plugin.version)}\n"
            f"{html.escape(plugin.description)}\n\n"
            f"Автор: {html.escape(plugin.credits)}\n"
            f"UUID: <code>{plugin.uuid}</code>\n"
            f"Хуков: <b>{hooks_count}</b>\n"
            f"Страница настроек Cardinal: {bool_icon(plugin.settings_page)}\n"
            f"Состояние: {bool_icon(plugin.enabled)}",
            reply_markup=keyboard([
                [("Выключить" if plugin.enabled else "Включить", f"plugin_toggle:{uuid}")],
                [("🗑 Удалить", f"plugin_delete_ask:{uuid}")],
                [("⬅️ Плагины", "plugins")],
            ]),
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
        await show_plugins(callback.message, callback.from_user.id)

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
        await show_plugins(callback.message, callback.from_user.id)

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
            f"<blockquote>{html.escape(clipped(chat.last_message_text or '[изображение]', 1800))}</blockquote>"
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
        await state.clear()
        await message.answer("✅ Сообщение отправлено.", reply_markup=main_keyboard())

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
        await state.clear()
        await message.answer("✅ Изображение отправлено в чат FunPay.", reply_markup=main_keyboard())

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
        await state.clear()
        await message.answer("✅ Ответ на отзыв отправлен.", reply_markup=main_keyboard())

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
