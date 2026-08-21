from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import threading
from dataclasses import dataclass, field
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
                autoreply_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                autoreply_text TEXT NOT NULL DEFAULT 'Здравствуйте! Спасибо за сообщение. Скоро отвечу.',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

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


@dataclass
class AccountRuntime:
    telegram_id: int
    account: Account
    runner: Runner
    stop_event: threading.Event = field(default_factory=threading.Event)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


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
            except Exception:
                logger.exception("Не удалось запустить FunPay-аккаунт пользователя %s", user_id)
                await self.safe_notify(
                    user_id,
                    "⚠️ Не удалось восстановить подключение к FunPay. Нажмите «Переподключить» или обновите данные.",
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
        runtime = AccountRuntime(telegram_id, account, runner)
        self.runtimes[telegram_id] = runtime
        runtime.tasks = [
            asyncio.create_task(asyncio.to_thread(runner.loop, runtime.stop_event)),
            asyncio.create_task(asyncio.to_thread(self._listen, runtime)),
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
                refresh_interval=2700,
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
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(
                            text="Открыть на FunPay",
                            url=f"https://funpay.com/chat/?node={chat_id}",
                        )]]
                    ),
                )
            if row["autoreply_enabled"] and await self.db.claim_autoreply(runtime.telegram_id, chat_id):
                try:
                    await asyncio.to_thread(
                        runtime.account.send_message,
                        message.chat_id,
                        row["autoreply_text"],
                        message.chat_name,
                    )
                except Exception:
                    logger.exception("Автоответ не отправлен в чат %s", chat_id)
                    await self.safe_notify(runtime.telegram_id, f"⚠️ Не удалось отправить автоответ в чат <code>{chat_id}</code>.")
        elif isinstance(event, events.NewOrderEvent) and row["notify_new_orders"]:
            order = event.order
            await self.safe_notify(
                runtime.telegram_id,
                "🛒 <b>Новый заказ</b>\n"
                f"ID: <code>{html.escape(order.id)}</code>\n"
                f"Покупатель: {html.escape(order.buyer_username or '—')}\n"
                f"Сумма: <b>{order.price} {html.escape(str(order.currency))}</b>\n"
                f"Товар: {html.escape(clipped(order.description))}",
            )
        elif isinstance(event, events.OrderStatusChangedEvent) and row["notify_order_status"]:
            order = event.order
            statuses = {
                types.OrderStatuses.PAID: "оплачен",
                types.OrderStatuses.CLOSED: "закрыт",
                types.OrderStatuses.REFUNDED: "возврат",
                types.OrderStatuses.PARTIALLY_REFUNDED: "частичный возврат",
                types.OrderStatuses.UNPAID: "не оплачен",
            }
            await self.safe_notify(
                runtime.telegram_id,
                f"📦 Статус заказа <code>{html.escape(order.id)}</code>: "
                f"<b>{statuses.get(order.status, order.status.name.lower())}</b>.",
            )


class ConnectState(StatesGroup):
    proxy = State()
    golden_key = State()


class AutoReplyState(StatesGroup):
    text = State()


class SendMessageState(StatesGroup):
    chat_id = State()
    text = State()


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
        except Exception:
            logger.exception("Не удалось обновить баланс")
            await callback.message.answer("❌ Не удалось проверить баланс FunPay.")
            return
        await callback.message.answer(
            f"💰 Баланс: <b>{runtime.account.total_balance} {html.escape(str(runtime.account.currency))}</b>\n"
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
            "autoreply_enabled",
        }
        if not row or column not in allowed_columns:
            await callback.answer("Неизвестная настройка", show_alert=True)
            return
        await db.set_flag(callback.from_user.id, column, not row[column])
        await callback.answer("Сохранено")
        if column == "autoreply_enabled":
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

    @router.callback_query(F.data == "chats")
    async def chats(callback: CallbackQuery) -> None:
        await callback.answer("Загружаю…")
        runtime = await require_runtime(callback.message, callback.from_user.id)
        if not runtime:
            return
        try:
            chats_map = await asyncio.to_thread(runtime.account.get_chats, True)
        except Exception:
            logger.exception("Не удалось получить чаты")
            await callback.message.answer("❌ Не удалось получить список чатов.")
            return
        lines = ["💬 <b>Последние чаты</b>"]
        for chat in list(chats_map.values())[:10]:
            lines.append(
                f"\n<b>{html.escape(chat.name or '—')}</b> · <code>{chat.id}</code>\n"
                f"{html.escape(clipped(chat.last_message_text, 100))}"
            )
        if len(lines) == 1:
            lines.append("\nЧатов пока нет.")
        await callback.message.answer("".join(lines), reply_markup=keyboard([[("⬅️ Меню", "menu")]]))

    @router.callback_query(F.data == "send_message")
    async def send_message_begin(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not await require_runtime(callback.message, callback.from_user.id):
            return
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
        try:
            await asyncio.to_thread(runtime.account.send_message, data["chat_id"], value)
        except Exception as exc:
            logger.exception("Ручное сообщение не отправлено")
            await message.answer(f"❌ FunPay не отправил сообщение: {html.escape(clipped(exc, 300))}")
            return
        await state.clear()
        await message.answer("✅ Сообщение отправлено.", reply_markup=main_keyboard())

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
            f"Runner: {status}",
            reply_markup=keyboard([
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
