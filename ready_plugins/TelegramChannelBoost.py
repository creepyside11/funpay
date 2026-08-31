from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import string
import time
from concurrent.futures import CancelledError, Future
from typing import Any
from urllib.parse import urlsplit

import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon.errors import (
    PasswordHashInvalidError,
    UserNotParticipantError,
    UsernameInvalidError,
    UsernameOccupiedError,
)
from telethon.password import compute_check
from telethon.tl.functions.account import GetPasswordRequest, GetPasswordSettingsRequest
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    EditAdminRequest,
    GetParticipantRequest,
    UpdateUsernameRequest,
)
from telethon.tl.functions.messages import EditChatCreatorRequest
from telethon.tl.types import ChatAdminRights, InputChannel


NAME = "Telegram Channel Boost"
VERSION = "1.0.2"
DESCRIPTION = "Создание, раскрутка и передача Telegram-каналов покупателям FunPay"
CREDITS = "FunPay aiogram bot"
SETTINGS_PAGE = True
TELETHON = True
UUID = "3f4874b9-0797-4d4a-aba6-c69aa63b2e08"

CALLBACK_PREFIX = "tcb:"
SETTINGS_CALLBACK = f"47:{UUID}:0"
DEFAULT_API_URL = "https://smmway.ru/api/v2"
POLL_SECONDS = 30
JOB_TIMEOUT_SECONDS = 24 * 60 * 60

logger = logging.getLogger("fpc_plugin.telegram_channel_boost")

_cardinal: Any | None = None
_client: Any | None = None
_pending_input: tuple[str, int | None] | None = None
_lot_cache: dict[str, str] = {}
_futures: set[Future[Any]] = set()
_running_job_ids: set[int] = set()
_running_transfer_ids: set[int] = set()


def _markup(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for row in rows:
        markup.row(
            *[
                InlineKeyboardButton(text=text, callback_data=callback)
                for text, callback in row
            ]
        )
    return markup


def _bot() -> Any:
    if _cardinal is None:
        raise RuntimeError("плагин ещё не инициализирован")
    return _cardinal.telegram.bot


def _db() -> Any:
    if _cardinal is None:
        raise RuntimeError("плагин ещё не инициализирован")
    return _cardinal.plugin_manager.db


def _telegram_id() -> int:
    if _cardinal is None:
        raise RuntimeError("плагин ещё не инициализирован")
    return int(_cardinal.runtime.telegram_id)


def _secret_box() -> Any:
    if _cardinal is None or not _cardinal.plugin_manager.telethon_service:
        raise RuntimeError("хранилище секретов недоступно")
    return _cardinal.plugin_manager.telethon_service.secrets


def _sync(awaitable: Any, timeout: float = 60) -> Any:
    if _cardinal is None:
        raise RuntimeError("плагин ещё не инициализирован")
    return asyncio.run_coroutine_threadsafe(
        awaitable, _cardinal.telegram.loop
    ).result(timeout=timeout)


def _spawn(awaitable: Any) -> Future[Any]:
    if _cardinal is None:
        raise RuntimeError("плагин ещё не инициализирован")
    future = asyncio.run_coroutine_threadsafe(awaitable, _cardinal.telegram.loop)
    _futures.add(future)

    def done(completed: Future[Any]) -> None:
        _futures.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except (asyncio.CancelledError, CancelledError):
            pass
        except Exception:
            logger.exception("Фоновая задача Telegram Channel Boost завершилась с ошибкой")

    future.add_done_callback(done)
    return future


async def _ensure_schema() -> None:
    await _db().execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_channel_boost_settings (
            telegram_id BIGINT PRIMARY KEY
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            api_base_url TEXT NOT NULL DEFAULT 'https://smmway.ru/api/v2',
            api_token_enc TEXT,
            service_id BIGINT,
            quantity INTEGER NOT NULL DEFAULT 100,
            target_members INTEGER NOT NULL DEFAULT 100,
            lot_id TEXT,
            lot_title TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS telegram_channel_boost_jobs (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            order_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            buyer_id BIGINT,
            lot_title TEXT NOT NULL,
            channel_id BIGINT,
            channel_access_hash BIGINT,
            channel_username TEXT,
            channel_url TEXT,
            smm_order_id TEXT,
            smm_status TEXT,
            member_count INTEGER NOT NULL DEFAULT 0,
            target_members INTEGER NOT NULL,
            buyer_username TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            error_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, order_id)
        );

        CREATE INDEX IF NOT EXISTS telegram_channel_boost_jobs_status_idx
            ON telegram_channel_boost_jobs (telegram_id, status, updated_at DESC);
        """
    )
    await _db().execute(
        """
        ALTER TABLE telegram_channel_boost_jobs
            ADD COLUMN IF NOT EXISTS buyer_id BIGINT
        """
    )
    await _db().execute(
        """
        INSERT INTO telegram_channel_boost_settings (telegram_id)
        VALUES ($1)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        _telegram_id(),
    )


async def _settings() -> Any:
    await _ensure_schema()
    return await _db().fetchrow(
        "SELECT * FROM telegram_channel_boost_settings WHERE telegram_id=$1",
        _telegram_id(),
    )


async def _set_setting(column: str, value: Any) -> None:
    allowed = {
        "api_base_url",
        "api_token_enc",
        "service_id",
        "quantity",
        "target_members",
        "lot_id",
        "lot_title",
    }
    if column not in allowed:
        raise ValueError("неизвестная настройка")
    await _ensure_schema()
    await _db().execute(
        f"""
        UPDATE telegram_channel_boost_settings
           SET {column}=$2, updated_at=NOW()
         WHERE telegram_id=$1
        """,
        _telegram_id(),
        value,
    )


def _api_token(settings: Any) -> str:
    encrypted = settings["api_token_enc"] if settings else None
    if not encrypted:
        return ""
    try:
        return _secret_box().decrypt(encrypted)
    except Exception as exc:
        raise RuntimeError("API-токен не удалось расшифровать") from exc


def _token_label(settings: Any) -> str:
    try:
        token = _api_token(settings)
    except Exception:
        return "ошибка расшифровки"
    if not token:
        return "не задан"
    return "••••" + token[-4:] if len(token) >= 4 else "••••"


def _settings_ready(settings: Any) -> bool:
    return bool(
        settings
        and settings["api_base_url"]
        and settings["api_token_enc"]
        and settings["service_id"]
        and int(settings["quantity"] or 0) > 0
        and int(settings["target_members"] or 0) > 0
        and settings["lot_id"]
        and settings["lot_title"]
    )


async def _recent_jobs(limit: int = 5) -> list[Any]:
    return list(
        await _db().fetch(
            """
            SELECT * FROM telegram_channel_boost_jobs
             WHERE telegram_id=$1
             ORDER BY created_at DESC LIMIT $2
            """,
            _telegram_id(),
            limit,
        )
    )


def _status_label(status: str) -> str:
    return {
        "queued": "ожидает запуска",
        "boosting": "идёт накрутка",
        "awaiting_username": "ожидается @username",
        "username_confirmation": "ожидается подтверждение username",
        "awaiting_join": "ожидается вступление покупателя",
        "awaiting_owner_2fa": "автоматическая передача владельца",
        "completed": "владелец передан",
        "canceled": "заказ отменён",
        "failed": "ошибка",
    }.get(status, status)


def _show_settings(chat_id: int) -> None:
    settings = _sync(_settings())
    jobs = _sync(_recent_jobs())
    telethon_ready = bool(
        _cardinal
        and _cardinal.telethon
        and _cardinal.telethon.is_connected(UUID)
    )
    lines = [
        "🚀 <b>Telegram Channel Boost</b>",
        "",
        f"Telethon: <b>{'подключён' if telethon_ready else 'не подключён'}</b>",
        f"API URL: <code>{html.escape(str(settings['api_base_url']))}</code>",
        f"API-токен: <b>{html.escape(_token_label(settings))}</b>",
        f"ID услуги: <b>{settings['service_id'] or 'не задан'}</b>",
        f"Количество для SMM: <b>{settings['quantity']}</b>",
        f"Порог готовности: <b>{settings['target_members']} подписчиков</b>",
        f"Лот Telegram: <b>{html.escape(str(settings['lot_title'] or 'не выбран'))}</b>",
        "",
        f"Готовность: <b>{'✅ настроено' if _settings_ready(settings) and telethon_ready else '⚠️ требуется настройка'}</b>",
        "",
        "Пароль 2FA хранится только зашифрованно в Telethon-сессии и используется для автоматической передачи владельца.",
    ]
    if jobs:
        lines.extend(["", "<b>Последние задания</b>"])
        for job in jobs:
            smm = html.escape(str(job["smm_status"] or "—"))
            lines.append(
                f"• <code>#{html.escape(job['order_id'])}</code> — "
                f"{html.escape(_status_label(job['status']))}; "
                f"SMM: {smm}; подписчики: {job['member_count']}/{job['target_members']}"
            )
    rows: list[list[tuple[str, str]]] = [
        [("🌐 Base URL API", f"{CALLBACK_PREFIX}set:base")],
        [("🔑 API-токен", f"{CALLBACK_PREFIX}set:token")],
        [("🧩 ID услуги", f"{CALLBACK_PREFIX}set:service")],
        [("📈 Количество накрутки", f"{CALLBACK_PREFIX}set:quantity")],
        [("🎯 Порог подписчиков", f"{CALLBACK_PREFIX}set:target")],
        [("🛒 Выбрать Telegram-лот", f"{CALLBACK_PREFIX}lots")],
        [("🧪 Проверить API", f"{CALLBACK_PREFIX}api_test")],
    ]
    rows.append([("🔄 Обновить", SETTINGS_CALLBACK)])
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))


def _validate_api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("нужен HTTPS URL без логина и пароля")
    if parsed.fragment or parsed.query:
        raise ValueError("Base URL не должен содержать query или fragment")
    return value


def _prompt(chat_id: int, key: str, text: str, job_id: int | None = None) -> None:
    global _pending_input
    _pending_input = (key, job_id)
    _bot().send_message(chat_id, text)


def _load_telegram_lots(chat_id: int) -> None:
    global _lot_cache
    profile = _cardinal.account.get_user(_cardinal.account.id)
    lots = []
    for lot in profile.get_lots():
        subcategory = getattr(lot, "subcategory", None)
        category = getattr(subcategory, "category", None)
        haystack = " ".join(
            str(value or "")
            for value in (
                getattr(category, "name", ""),
                getattr(subcategory, "name", ""),
                getattr(subcategory, "fullname", ""),
            )
        ).casefold()
        if "telegram" in haystack or "телеграм" in haystack:
            lots.append(lot)
    if not lots:
        _bot().send_message(
            chat_id,
            "❌ В профиле не найдены лоты из категории Telegram. Создайте или активируйте лот и обновите список.",
        )
        return
    _lot_cache = {
        str(lot.id): str(getattr(lot, "description", None) or f"Лот {lot.id}")
        for lot in lots[:40]
    }
    rows = [
        [
            (
                f"{title[:45]} · ID {lot_id}",
                f"{CALLBACK_PREFIX}lot:{lot_id}",
            )
        ]
        for lot_id, title in _lot_cache.items()
    ]
    rows.append([("⬅️ Настройки", SETTINGS_CALLBACK)])
    _bot().send_message(
        chat_id,
        "🛒 <b>Выберите лот категории Telegram</b>\n\n"
        "Плагин будет запускаться только для заказов, описание которых содержит название выбранного лота.",
        reply_markup=_markup(*rows),
    )


def _smm_request(settings: Any, **payload: Any) -> dict[str, Any]:
    token = _api_token(settings)
    if not token:
        raise RuntimeError("API-токен не задан")
    data = {"key": token, **payload}
    response = requests.post(
        str(settings["api_base_url"]),
        data=data,
        timeout=30,
        allow_redirects=False,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("SMM API вернул неожиданный ответ")
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result


async def _api_test(chat_id: int) -> None:
    try:
        settings = await _settings()
        result = await asyncio.to_thread(
            _smm_request, settings, action="balance"
        )
        balance = result.get("balance", "—")
        currency = result.get("currency", "")
        text = f"✅ API отвечает. Баланс: <b>{html.escape(str(balance))} {html.escape(str(currency))}</b>"
    except Exception as exc:
        logger.exception("Проверка SMM API не выполнена")
        text = f"❌ SMM API не отвечает: <code>{html.escape(str(exc)[:500])}</code>"
    await asyncio.to_thread(_bot().send_message, chat_id, text)


def _on_callback(call: Any) -> None:
    data = str(call.data or "")
    chat_id = int(call.message.chat.id)
    try:
        _bot().answer_callback_query(call.id)
        if data == SETTINGS_CALLBACK or data == f"{CALLBACK_PREFIX}open":
            _show_settings(chat_id)
        elif data == f"{CALLBACK_PREFIX}set:base":
            _prompt(chat_id, "base", "Отправьте HTTPS Base URL API сервиса, например <code>https://smmway.ru/api/v2</code>.")
        elif data == f"{CALLBACK_PREFIX}set:token":
            _prompt(chat_id, "token", "Отправьте API-токен. Сообщение будет удалено, токен сохранится зашифрованным.")
        elif data == f"{CALLBACK_PREFIX}set:service":
            _prompt(chat_id, "service", "Отправьте числовой ID услуги подписчиков Telegram.")
        elif data == f"{CALLBACK_PREFIX}set:quantity":
            _prompt(chat_id, "quantity", "Отправьте количество подписчиков для заказа в SMM API (1–1 000 000).")
        elif data == f"{CALLBACK_PREFIX}set:target":
            _prompt(chat_id, "target", "Отправьте фактическое число подписчиков канала, при котором заказ готов (1–1 000 000).")
        elif data == f"{CALLBACK_PREFIX}lots":
            _load_telegram_lots(chat_id)
        elif data.startswith(f"{CALLBACK_PREFIX}lot:"):
            lot_id = data.rsplit(":", 1)[1]
            title = _lot_cache.get(lot_id)
            if not title:
                raise RuntimeError("список лотов устарел; откройте его повторно")
            _sync(_set_setting("lot_id", lot_id))
            _sync(_set_setting("lot_title", title))
            _bot().send_message(chat_id, f"✅ Выбран лот: <b>{html.escape(title)}</b>")
            _show_settings(chat_id)
        elif data == f"{CALLBACK_PREFIX}api_test":
            _spawn(_api_test(chat_id))
    except Exception as exc:
        logger.exception("Ошибка callback Telegram Channel Boost")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


def _on_setting_message(message: Any) -> None:
    global _pending_input
    if _pending_input is None:
        return
    key, job_id = _pending_input
    _pending_input = None
    value = str(message.text or "").strip()
    chat_id = int(message.chat.id)
    try:
        if key == "token":
            try:
                _bot().delete_message(chat_id, message.message_id)
            except Exception:
                logger.warning("Не удалось удалить сообщение с секретом", exc_info=True)
        if key == "base":
            _sync(_set_setting("api_base_url", _validate_api_url(value)))
        elif key == "token":
            if not 8 <= len(value) <= 1024:
                raise ValueError("длина API-токена должна быть от 8 до 1024 символов")
            _sync(_set_setting("api_token_enc", _secret_box().encrypt(value)))
        elif key in {"service", "quantity", "target"}:
            if not value.isdigit():
                raise ValueError("нужно отправить целое положительное число")
            number = int(value)
            if not 1 <= number <= 1_000_000:
                raise ValueError("значение должно быть от 1 до 1 000 000")
            column = {
                "service": "service_id",
                "quantity": "quantity",
                "target": "target_members",
            }[key]
            _sync(_set_setting(column, number))
        _bot().send_message(chat_id, "✅ Настройка сохранена.")
        _show_settings(chat_id)
    except Exception as exc:
        logger.exception("Настройка Telegram Channel Boost не сохранена")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


async def _insert_job(order: dict[str, Any], settings: Any) -> Any | None:
    return await _db().fetchrow(
        """
        INSERT INTO telegram_channel_boost_jobs
            (telegram_id, order_id, chat_id, chat_name, buyer_id,
             lot_title, target_members)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (telegram_id, order_id) DO NOTHING
        RETURNING *
        """,
        _telegram_id(),
        order["id"],
        str(order["chat_id"]),
        order["chat_name"],
        order["buyer_id"],
        order["description"],
        int(settings["target_members"]),
    )


async def _job(job_id: int) -> Any | None:
    return await _db().fetchrow(
        """
        SELECT * FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        job_id,
    )


async def _update_job(job_id: int, **values: Any) -> None:
    allowed = {
        "channel_id",
        "chat_id",
        "channel_access_hash",
        "channel_username",
        "channel_url",
        "smm_order_id",
        "smm_status",
        "member_count",
        "buyer_username",
        "status",
        "error_text",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(
        f"{key}=${index + 3}" for index, key in enumerate(values)
    )
    await _db().execute(
        f"""
        UPDATE telegram_channel_boost_jobs
           SET {assignments}, updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        job_id,
        *values.values(),
    )


async def _funpay_send(job: Any, text: str) -> None:
    await asyncio.to_thread(
        _cardinal.account.send_message,
        job["chat_id"],
        text,
        job["chat_name"],
    )


async def _notify_owner(text: str, reply_markup: Any | None = None) -> None:
    await asyncio.to_thread(
        _cardinal.telegram.send_notification,
        text,
        reply_markup=reply_markup,
    )


async def _stored_2fa_password() -> str | None:
    service = getattr(_cardinal.plugin_manager, "telethon_service", None)
    if service is None:
        return None
    return await service.get_2fa_password(_telegram_id(), UUID)


async def _create_public_channel(order_id: str) -> tuple[Any, str]:
    if _client is None or not _client.is_connected():
        raise RuntimeError("Telethon не подключён")
    result = await _client(
        CreateChannelRequest(
            title=f"Telegram order {order_id}",
            about="Канал создан автоматически после заказа на FunPay.",
            broadcast=True,
            megagroup=False,
        )
    )
    channel = result.chats[0]
    for _ in range(15):
        username = "fp" + "".join(
            random.choice(string.ascii_lowercase) for _ in range(14)
        )
        try:
            await _client(UpdateUsernameRequest(channel, username))
            return channel, username
        except UsernameOccupiedError:
            continue
        except UsernameInvalidError as exc:
            raise RuntimeError("Telegram отклонил сгенерированный username") from exc
    raise RuntimeError("не удалось подобрать свободный username канала")


def _input_channel(job: Any) -> InputChannel:
    if not job["channel_id"] or not job["channel_access_hash"]:
        raise RuntimeError("данные Telegram-канала не сохранены")
    return InputChannel(int(job["channel_id"]), int(job["channel_access_hash"]))


async def _member_count(job: Any) -> int:
    participants = await _client.get_participants(_input_channel(job), limit=0)
    return int(getattr(participants, "total", len(participants)))


async def _fail_job(job: Any, reason: str) -> None:
    reason = str(reason)[:1000]
    await _update_job(job["id"], status="failed", error_text=reason)
    try:
        await _funpay_send(
            job,
            "Произошла ошибка при подготовке Telegram-канала. Продавец уже уведомлён и проверяет заказ.",
        )
    except Exception:
        logger.exception("Не удалось сообщить покупателю об ошибке задания")
    await _notify_owner(
        "❌ <b>Telegram Channel Boost</b>\n\n"
        f"Заказ: <code>#{html.escape(job['order_id'])}</code>\n"
        f"Ошибка: <code>{html.escape(reason)}</code>"
    )


async def _run_job(job_id: int) -> None:
    if job_id in _running_job_ids:
        return
    _running_job_ids.add(job_id)
    started_at = time.monotonic()
    try:
        job = await _job(job_id)
        if not job or job["status"] not in {"queued", "boosting"}:
            return
        settings = await _settings()
        if not _settings_ready(settings):
            await _fail_job(job, "настройки плагина заполнены не полностью")
            return
        while _client is None or not _client.is_connected():
            if time.monotonic() - started_at > 600:
                await _fail_job(job, "Telethon не подключён в течение 10 минут")
                return
            await asyncio.sleep(5)
        if not job["channel_id"]:
            channel, username = await _create_public_channel(job["order_id"])
            await _update_job(
                job_id,
                channel_id=int(channel.id),
                channel_access_hash=int(channel.access_hash),
                channel_username=username,
                channel_url=f"https://t.me/{username}",
                status="boosting",
            )
            job = await _job(job_id)
        if not job["smm_order_id"]:
            response = await asyncio.to_thread(
                _smm_request,
                settings,
                action="add",
                service=int(settings["service_id"]),
                link=job["channel_url"],
                quantity=int(settings["quantity"]),
            )
            smm_order_id = response.get("order")
            if not smm_order_id:
                raise RuntimeError("SMM API не вернул ID заказа")
            await _update_job(
                job_id,
                smm_order_id=str(smm_order_id),
                smm_status="Pending",
                status="boosting",
            )
            job = await _job(job_id)
            await _notify_owner(
                "🚀 <b>Запущена подготовка Telegram-канала</b>\n\n"
                f"FunPay: <code>#{html.escape(job['order_id'])}</code>\n"
                f"Канал: {html.escape(job['channel_url'])}\n"
                f"SMM order: <code>{html.escape(job['smm_order_id'])}</code>\n"
                f"Цель: <b>{job['target_members']} подписчиков</b>"
            )
        while True:
            job = await _job(job_id)
            if not job or job["status"] != "boosting":
                return
            if not _cardinal.plugin_manager.is_enabled(_telegram_id(), UUID):
                await asyncio.sleep(POLL_SECONDS)
                continue
            try:
                full_order = await asyncio.to_thread(
                    _cardinal.account.get_order, job["order_id"]
                )
                order_status = getattr(getattr(full_order, "status", None), "name", "")
                if order_status in {"REFUNDED", "UNPAID"}:
                    await _update_job(job_id, status="canceled", error_text=order_status)
                    await _notify_owner(
                        f"⚠️ Задание <code>#{html.escape(job['order_id'])}</code> остановлено: статус FunPay {order_status}."
                    )
                    return
            except Exception:
                logger.warning("Не удалось проверить статус FunPay-заказа %s", job["order_id"], exc_info=True)
            smm_status = str(job["smm_status"] or "Unknown")
            try:
                status_response = await asyncio.to_thread(
                    _smm_request,
                    settings,
                    action="status",
                    order=job["smm_order_id"],
                )
                smm_status = str(status_response.get("status", smm_status))
            except Exception:
                logger.warning("Не удалось проверить статус SMM order %s", job["smm_order_id"], exc_info=True)
            try:
                members = await _member_count(job)
            except Exception:
                logger.warning("Не удалось получить число подписчиков канала %s", job["channel_id"], exc_info=True)
                members = int(job["member_count"] or 0)
            await _update_job(
                job_id,
                smm_status=smm_status,
                member_count=members,
            )
            if members >= int(job["target_members"]):
                await _update_job(job_id, status="awaiting_username", error_text=None)
                job = await _job(job_id)
                await _funpay_send(
                    job,
                    "✅ Ваш Telegram-канал подготовлен:\n"
                    f"{job['channel_url']}\n\n"
                    "1. Вступите в канал.\n"
                    "2. Отправьте сюда свой Telegram username строго в формате @username.\n"
                    "После этого бот попросит подтвердить написание username.",
                )
                await _notify_owner(
                    "✅ <b>Канал набрал заданный порог</b>\n\n"
                    f"Заказ: <code>#{html.escape(job['order_id'])}</code>\n"
                    f"Подписчики: <b>{members}/{job['target_members']}</b>\n"
                    f"SMM: <b>{html.escape(smm_status)}</b>\n"
                    "Ссылка отправлена покупателю; ожидается его @username."
                )
                return
            if smm_status.casefold() in {"partial", "canceled", "cancelled", "error", "refunded"}:
                await _fail_job(
                    job,
                    f"SMM-заказ завершился со статусом {smm_status}, а подписчиков только {members}/{job['target_members']}",
                )
                return
            if time.monotonic() - started_at >= JOB_TIMEOUT_SECONDS:
                await _fail_job(job, "истёк 24-часовой срок ожидания подписчиков")
                return
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Ошибка задания Telegram Channel Boost %s", job_id)
        job = await _job(job_id)
        if job:
            await _fail_job(job, str(exc))
    finally:
        _running_job_ids.discard(job_id)


async def _process_new_order(order: dict[str, Any]) -> None:
    settings = await _settings()
    lot_title = str(settings["lot_title"] or "").strip()
    description = str(order["description"] or "")
    if not lot_title or lot_title.casefold() not in description.casefold():
        return
    if not _settings_ready(settings):
        await _notify_owner(
            "❌ Получен заказ для Telegram Channel Boost, но настройки заполнены не полностью. "
            f"Заказ: <code>#{html.escape(order['id'])}</code>"
        )
        return
    job = await _insert_job(order, settings)
    if not job:
        return
    await _funpay_send(
        job,
        "🚀 Заказ принят. Создаю публичный Telegram-канал и запускаю набор подписчиков. "
        "Ссылку и дальнейшие инструкции отправлю автоматически после достижения заданного количества.",
    )
    await _run_job(int(job["id"]))


async def _resume_jobs() -> None:
    await _ensure_schema()
    rows = await _db().fetch(
        """
        SELECT id FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1 AND status IN ('queued', 'boosting')
         ORDER BY created_at
        """,
        _telegram_id(),
    )
    await asyncio.gather(
        *[_run_job(int(row["id"])) for row in rows],
        return_exceptions=True,
    )


async def _resume_transfers() -> None:
    password = await _stored_2fa_password()
    if not password:
        return
    rows = await _db().fetch(
        """
        SELECT id FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1 AND status='awaiting_owner_2fa'
         ORDER BY created_at
        """,
        _telegram_id(),
    )
    await asyncio.gather(
        *[_transfer_owner(int(row["id"]), password) for row in rows],
        return_exceptions=True,
    )


async def _active_buyer_job(
    chat_id: str,
    buyer_id: int | None,
    chat_name: str | None,
    order_chat_id: str | None,
) -> Any | None:
    normalized_name = (chat_name or "").strip() or None
    job = await _db().fetchrow(
        """
        SELECT * FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1
           AND (
               chat_id=$2
               OR ($3::BIGINT IS NOT NULL AND buyer_id=$3)
               OR ($4::TEXT IS NOT NULL AND LOWER(chat_name)=LOWER($4))
               OR ($5::TEXT IS NOT NULL AND chat_id=$5)
           )
           AND status IN (
               'awaiting_username', 'username_confirmation',
               'awaiting_join', 'awaiting_owner_2fa'
           )
         ORDER BY created_at DESC LIMIT 1
        """,
        _telegram_id(),
        str(chat_id),
        buyer_id,
        normalized_name,
        order_chat_id,
    )
    if job and str(job["chat_id"]) != str(chat_id):
        await _update_job(int(job["id"]), chat_id=str(chat_id))
        return await _job(int(job["id"]))
    return job


async def _verify_buyer(job: Any) -> None:
    username = str(job["buyer_username"] or "")
    if not username:
        await _update_job(job["id"], status="awaiting_username")
        return
    if _client is None or not _client.is_connected():
        await _funpay_send(job, "Telethon временно не подключён. Продавец уже уведомлён.")
        await _notify_owner("⚠️ Для проверки покупателя подключите Telethon в настройках плагина.")
        return
    try:
        user = await _client.get_entity(username)
        await _client(GetParticipantRequest(_input_channel(job), user))
    except UserNotParticipantError:
        await _update_job(job["id"], status="awaiting_join")
        await _funpay_send(
            job,
            f"Пользователь {username} ещё не найден в канале. Вступите по ссылке {job['channel_url']} "
            "и отправьте команду #проверить.",
        )
        return
    except Exception as exc:
        await _funpay_send(
            job,
            f"Не удалось проверить {username}: {str(exc)[:300]}. Отправьте #изменить и укажите username заново.",
        )
        return
    await _update_job(job["id"], status="awaiting_owner_2fa")
    await _funpay_send(
        job,
        f"✅ {username} найден в канале. Автоматически передаю вам права владельца.",
    )
    password = await _stored_2fa_password()
    if not password:
        await _notify_owner(
            "⚠️ <b>Нельзя автоматически передать Telegram-канал</b>\n\n"
            f"Заказ: <code>#{html.escape(job['order_id'])}</code>\n"
            "В сохранённой Telethon-сессии нет пароля 2FA. Авторизуйте Telegram-аккаунт "
            "заново через номер, код и 2FA — после входа передача продолжится автоматически."
        )
        return
    await _transfer_owner(int(job["id"]), password)


async def _process_funpay_message(message: dict[str, Any]) -> None:
    job = await _active_buyer_job(
        message["chat_id"],
        message.get("buyer_id"),
        message.get("chat_name"),
        message.get("order_chat_id"),
    )
    if not job or job["status"] == "awaiting_owner_2fa":
        return
    text = str(message["text"] or "").strip()
    lowered = text.casefold()
    if lowered == "#изменить":
        await _update_job(job["id"], status="awaiting_username", buyer_username=None)
        await _funpay_send(job, "Отправьте новый username в формате @username.")
        return
    if lowered == "#проверить" and job["status"] == "awaiting_join":
        await _verify_buyer(job)
        return
    if lowered == "#да" and job["status"] == "username_confirmation":
        await _verify_buyer(job)
        return
    match = re.fullmatch(r"@([A-Za-z0-9_]{5,32})", text)
    if match and job["status"] in {"awaiting_username", "awaiting_join"}:
        username = "@" + match.group(1)
        await _update_job(
            job["id"],
            buyer_username=username,
            status="username_confirmation",
        )
        await _funpay_send(
            job,
            f"Вы указали {username}. Username написан верно?\n\n"
            "Отправьте #да для подтверждения или #изменить, чтобы указать другой.",
        )


async def _transfer_owner(job_id: int, password: str) -> None:
    if job_id in _running_transfer_ids:
        return
    _running_transfer_ids.add(job_id)
    job: Any | None = None
    channel: InputChannel | None = None
    user: Any | None = None
    promoted = False
    try:
        job = await _job(job_id)
        if not job or job["status"] != "awaiting_owner_2fa":
            return
        if _client is None or not _client.is_connected():
            await _notify_owner("❌ Telethon не подключён; автоматическая передача канала отложена.")
            return
        user = await _client.get_entity(job["buyer_username"])
        channel = _input_channel(job)
        await _client(GetParticipantRequest(channel, user))
        password_state = await _client(GetPasswordRequest())
        password_check = await asyncio.to_thread(
            compute_check, password_state, password
        )
        await _client(GetPasswordSettingsRequest(password_check))
        rights = ChatAdminRights(
            change_info=True,
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=True,
            manage_call=True,
            post_stories=True,
            edit_stories=True,
            delete_stories=True,
        )
        await _client(EditAdminRequest(channel, user, rights, rank="Владелец"))
        promoted = True
        password_state = await _client(GetPasswordRequest())
        password_check = await asyncio.to_thread(
            compute_check, password_state, password
        )
        await _client(EditChatCreatorRequest(channel, user, password_check))
    except PasswordHashInvalidError:
        if promoted and channel is not None and user is not None:
            await _revoke_admin(channel, user)
        await _notify_owner(
            "❌ Сохранённый пароль 2FA больше не подходит. Авторизуйте Telegram-аккаунт "
            "плагина заново; передача продолжится автоматически."
        )
        return
    except UserNotParticipantError:
        await _update_job(job_id, status="awaiting_join")
        if job:
            await _funpay_send(job, "Вы покинули канал. Вступите снова и отправьте #проверить.")
        await _notify_owner("❌ Покупатель больше не состоит в канале.")
        return
    except Exception as exc:
        if promoted and channel is not None and user is not None:
            await _revoke_admin(channel, user)
        logger.exception("Не удалось передать владельца Telegram-канала")
        await _notify_owner(
            "❌ Telegram не передал владельца. Проверьте, что 2FA включена более 7 дней, "
            "текущая сессия активна более 24 часов и на аккаунте нет ограничения публичных каналов.\n\n"
            f"Ошибка: <code>{html.escape(str(exc)[:500])}</code>",
        )
        return
    else:
        await _update_job(job_id, status="completed", error_text=None)
        await _funpay_send(
            job,
            f"✅ Права владельца канала {job['channel_url']} переданы пользователю {job['buyer_username']}. "
            "Заказ выполнен; подтвердите выполнение на FunPay после проверки.",
        )
        await _notify_owner(
            f"✅ Владелец канала по заказу <code>#{html.escape(job['order_id'])}</code> "
            f"автоматически передан пользователю <b>{html.escape(job['buyer_username'])}</b>."
        )
    finally:
        _running_transfer_ids.discard(job_id)


async def _revoke_admin(channel: InputChannel, user: Any) -> None:
    try:
        await _client(EditAdminRequest(channel, user, ChatAdminRights(), rank=""))
    except Exception:
        logger.exception("Не удалось отозвать временные права администратора")


async def _process_order_status(order_id: str, status: str) -> None:
    if status not in {"REFUNDED", "UNPAID"}:
        return
    job = await _db().fetchrow(
        """
        SELECT * FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1 AND order_id=$2
           AND status NOT IN ('completed', 'failed', 'canceled')
        """,
        _telegram_id(),
        order_id,
    )
    if not job:
        return
    await _update_job(job["id"], status="canceled", error_text=status)
    await _notify_owner(
        f"⚠️ Telegram-задание <code>#{html.escape(order_id)}</code> отменено из-за статуса FunPay {status}."
    )


def pre_init(cardinal: Any) -> None:
    global _cardinal
    _cardinal = cardinal
    _sync(_ensure_schema())
    bot = cardinal.telegram.bot
    bot.register_callback_query_handler(
        _on_callback,
        func=lambda call: str(call.data or "") == SETTINGS_CALLBACK
        or str(call.data or "").startswith(CALLBACK_PREFIX),
    )
    bot.register_message_handler(
        _on_setting_message,
        content_types=["text"],
        func=lambda _message: _pending_input is not None,
    )


def telethon_ready(cardinal: Any, client: Any) -> None:
    global _cardinal, _client
    _cardinal = cardinal
    _client = client
    _spawn(_resume_jobs())
    _spawn(_resume_transfers())


def telethon_disconnected(cardinal: Any, client: Any) -> None:
    global _client
    if _client is client:
        _client = None


def new_order(cardinal: Any, event: Any) -> None:
    order = event.order
    status = getattr(getattr(order, "status", None), "name", "")
    if status and status != "PAID":
        return
    payload = {
        "id": str(order.id),
        "chat_id": str(order.chat_id),
        "chat_name": str(order.buyer_username or "Покупатель"),
        "buyer_id": int(order.buyer_id) if getattr(order, "buyer_id", None) else None,
        "description": str(order.description or ""),
    }
    _spawn(_process_new_order(payload))


def new_message(cardinal: Any, event: Any) -> None:
    message = event.message
    if (
        getattr(message, "author_id", None) in {0, cardinal.account.id}
        or getattr(message, "by_bot", False)
        or getattr(message, "by_vertex", False)
    ):
        return
    buyer_id = (
        int(message.interlocutor_id)
        if getattr(message, "interlocutor_id", None)
        else None
    )
    order_chat_id = None
    if buyer_id:
        first_id, second_id = sorted((buyer_id, int(cardinal.account.id)))
        order_chat_id = f"users-{first_id}-{second_id}"
    _spawn(
        _process_funpay_message(
            {
                "chat_id": str(message.chat_id),
                "chat_name": str(message.chat_name or ""),
                "buyer_id": buyer_id,
                "order_chat_id": order_chat_id,
                "text": str(message.text or ""),
            }
        )
    )


def order_status_changed(cardinal: Any, event: Any) -> None:
    status = getattr(getattr(event.order, "status", None), "name", "")
    _spawn(_process_order_status(str(event.order.id), status))


def pre_stop(cardinal: Any) -> None:
    global _client
    _client = None
    for future in list(_futures):
        future.cancel()


def on_delete(cardinal: Any, callback: Any) -> None:
    pre_stop(cardinal)
    _sync(
        _db().execute(
            "DELETE FROM telegram_channel_boost_jobs WHERE telegram_id=$1",
            _telegram_id(),
        )
    )
    _sync(
        _db().execute(
            "DELETE FROM telegram_channel_boost_settings WHERE telegram_id=$1",
            _telegram_id(),
        )
    )


BIND_TO_PRE_INIT = [pre_init]
BIND_TO_POST_INIT = []
BIND_TO_PRE_START = []
BIND_TO_POST_START = []
BIND_TO_PRE_STOP = [pre_stop]
BIND_TO_POST_STOP = []
BIND_TO_INIT_MESSAGE = []
BIND_TO_MESSAGES_LIST_CHANGED = []
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = []
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_INIT_ORDER = []
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_ORDERS_LIST_CHANGED = []
BIND_TO_ORDER_STATUS_CHANGED = [order_status_changed]
BIND_TO_PRE_DELIVERY = []
BIND_TO_POST_DELIVERY = []
BIND_TO_PRE_LOTS_RAISE = []
BIND_TO_POST_LOTS_RAISE = []
BIND_TO_TELETHON_READY = [telethon_ready]
BIND_TO_TELETHON_DISCONNECTED = [telethon_disconnected]
BIND_TO_DELETE = on_delete
