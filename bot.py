from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_reviews BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_lots_raise BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS notify_system BOOLEAN NOT NULL DEFAULT TRUE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS auto_raise_enabled BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE funpay_users ADD COLUMN IF NOT EXISTS keep_online_enabled BOOLEAN NOT NULL DEFAULT TRUE;

            CREATE TABLE IF NOT EXISTS funpay_autoreply_log (
                telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL,
                last_sent TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (telegram_id, chat_id)
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

    async def claim_autoreply(self, telegram_id: int, chat_id: str) -> bool:
        row = await self.fetchrow(
            """
            INSERT INTO funpay_autoreply_log (telegram_id, chat_id, last_sent)
            VALUES ($1, $2, NOW())
            ON CONFLICT (telegram_id, chat_id) DO UPDATE
               SET last_sent=NOW()
             WHERE funpay_autoreply_log.last_sent < NOW() - INTERVAL '30 minutes'
            RETURNING telegram_id
            """,
            telegram_id,
            chat_id,
        )
        return row is not None

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
        "$order_title": str(order.description if order else ""),
    }
    for variable in sorted(variables, key=len, reverse=True):
        text = text.replace(variable, variables[variable])
    return text


def format_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".")


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
    stop_event: threading.Event = field(default_factory=threading.Event)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
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
        )
        self.runtimes[telegram_id] = runtime
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
            categories: dict[int, str] = {}
            for subcategory in profile.get_sorted_lots(2):
                if subcategory.type is types.SubCategoryTypes.COMMON:
                    categories[subcategory.category.id] = subcategory.category.name
            if not categories:
                raise RuntimeError("В профиле нет обычных лотов для поднятия")

            results: list[str] = []
            waits: list[int] = []
            now = asyncio.get_running_loop().time()
            for category_id, category_name in categories.items():
                scheduled = runtime.raise_schedule.get(category_id, 0)
                if not force and scheduled > now:
                    waits.append(max(int(scheduled - now), 1))
                    continue
                try:
                    wait = await asyncio.to_thread(runtime.account.raise_lots, category_id)
                    wait = max(int(wait or 3600), 60)
                    waits.append(wait)
                    runtime.raise_schedule[category_id] = now + wait
                    results.append(f"✅ {category_name}")
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
        runtime.stop_event.set()
        try:
            await asyncio.wait_for(asyncio.gather(*runtime.tasks, return_exceptions=True), timeout=12)
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

    async def handle_event(self, runtime: AccountRuntime, event: Any) -> None:
        row = await self.db.get_user(runtime.telegram_id)
        if not row or not row["account_active"]:
            return
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
                if row["notify_reviews"]:
                    await self.safe_notify(
                        runtime.telegram_id,
                        "⭐ <b>Новый или изменённый отзыв</b>\n"
                        f"{html.escape(clipped(message.text or 'Откройте чат для подробностей', 1400))}",
                        reply_markup=keyboard([
                            [("💬 Открыть чат", f"chat_full:{message.chat_id}:0")],
                        ]),
                    )
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
            if row["autoreply_enabled"] and await self.db.claim_autoreply(runtime.telegram_id, chat_id):
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
                    logger.exception("Автоответ не отправлен в чат %s", chat_id)
                    if row["notify_system"]:
                        await self.safe_notify(runtime.telegram_id, f"⚠️ Не удалось отправить автоответ в чат <code>{chat_id}</code>.")
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
                f"<b>{html.escape(order_status_label(order.status))}</b>.",
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
        [("💬 Последние чаты", "chats"), ("✉️ Отправить сообщение", "send_message")],
        [("📦 Заказ по ID", "order_lookup"), ("🖼 Изображения", "images")],
        [("🆙 Автоподнятие", "auto_raise"), ("⚙️ Аккаунт", "account")],
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
        await callback.message.answer(
            "👤 <b>Подробный профиль</b>\n"
            f"Ник: <b>{html.escape(profile_obj.username)}</b>\n"
            f"ID: <code>{profile_obj.id}</code>\n"
            f"Онлайн: {bool_icon(profile_obj.online)}\n"
            f"Заблокирован: {'да' if profile_obj.banned else 'нет'}\n"
            f"Активных продаж: <b>{runtime.account.active_sales}</b>\n"
            f"Активных покупок: <b>{runtime.account.active_purchases}</b>\n"
            f"Лотов в профиле: <b>{len(lots)}</b>",
            reply_markup=keyboard([[("⬅️ Меню", "menu")]]),
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
            await show_auto_raise(callback.message, callback.from_user.id)
        elif column == "autoreply_enabled":
            await show_autoreply(callback.message, callback.from_user.id)
        else:
            await show_notifications(callback.message, callback.from_user.id)

    async def show_autoreply(target: Message, user_id: int) -> None:
        row = await db.get_user(user_id)
        await target.answer(
            "🤖 <b>Автоответчик</b>\n"
            "Ответ отправляется входящему собеседнику не чаще одного раза в 30 минут.\n\n"
            f"Текст: <i>{html.escape(clipped(row['autoreply_text'], 1000))}</i>",
            reply_markup=keyboard([
                [(f"{bool_icon(row['autoreply_enabled'])} Включён", "toggle:autoreply_enabled")],
                [("✏️ Изменить текст", "autoreply_text")],
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
        await state.set_state(AutoReplyState.text)
        await callback.message.answer("Отправьте новый текст автоответа (до 1500 символов) или /cancel.")

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
            "Переменные работают в автоответчике и ручных сообщениях.",
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
        await show_main(message, message.from_user.id)

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
