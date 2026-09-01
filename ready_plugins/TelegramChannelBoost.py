from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import string
import time
from concurrent.futures import CancelledError, Future
from datetime import datetime, timezone
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
VERSION = "1.2.0"
DESCRIPTION = "Склад Telegram-каналов для нескольких лотов и нескольких Telethon-аккаунтов"
CREDITS = "FunPay aiogram bot"
SETTINGS_PAGE = True
TELETHON = True
UUID = "3f4874b9-0797-4d4a-aba6-c69aa63b2e08"

CALLBACK_PREFIX = "tcb:"
SETTINGS_CALLBACK = f"47:{UUID}:0"
DEFAULT_API_URL = "https://smmway.ru/api/v2"
POLL_SECONDS = 30
INVENTORY_CHECK_SECONDS = 5 * 60
JOB_TIMEOUT_SECONDS = 24 * 60 * 60

logger = logging.getLogger("fpc_plugin.telegram_channel_boost")

_cardinal: Any | None = None
_client: Any | None = None
_pending_input: tuple[str, int | None] | None = None
_lot_cache: dict[str, str] = {}
_draft_rule: dict[str, Any] = {}
_futures: set[Future[Any]] = set()
_running_job_ids: set[int] = set()
_running_transfer_ids: set[int] = set()
_running_inventory_ids: set[int] = set()
_inventory_tasks: set[asyncio.Task[Any]] = set()
_inventory_maintenance_lock: asyncio.Lock | None = None
_inventory_loop_future: Future[Any] | None = None


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


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
            min_ready_channels INTEGER NOT NULL DEFAULT 1,
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
            rule_id BIGINT,
            lot_id TEXT,
            lot_title TEXT NOT NULL,
            telethon_session_id BIGINT,
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

        CREATE TABLE IF NOT EXISTS telegram_channel_boost_inventory (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            rule_id BIGINT,
            lot_id TEXT,
            lot_title TEXT,
            telethon_session_id BIGINT,
            channel_id BIGINT,
            channel_access_hash BIGINT,
            channel_username TEXT,
            channel_url TEXT,
            service_id BIGINT NOT NULL,
            quantity INTEGER NOT NULL,
            target_members INTEGER NOT NULL,
            smm_order_id TEXT,
            smm_status TEXT,
            member_count INTEGER NOT NULL DEFAULT 0,
            refill_id TEXT,
            refill_pending BOOLEAN NOT NULL DEFAULT FALSE,
            last_refill_at TIMESTAMPTZ,
            reserved_order_id TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            error_text TEXT,
            ready_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, channel_id),
            UNIQUE (telegram_id, reserved_order_id)
        );

        CREATE INDEX IF NOT EXISTS telegram_channel_boost_jobs_status_idx
            ON telegram_channel_boost_jobs (telegram_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS telegram_channel_boost_inventory_status_idx
            ON telegram_channel_boost_inventory
                (telegram_id, status, ready_at, updated_at);

        CREATE TABLE IF NOT EXISTS telegram_channel_boost_lot_rules (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            lot_id TEXT NOT NULL,
            lot_title TEXT NOT NULL,
            service_id BIGINT NOT NULL,
            quantity INTEGER NOT NULL,
            target_members INTEGER NOT NULL,
            min_ready_channels INTEGER NOT NULL DEFAULT 1,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, lot_id)
        );
        """
    )
    await _db().execute(
        """
        ALTER TABLE telegram_channel_boost_settings
            ADD COLUMN IF NOT EXISTS min_ready_channels INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE telegram_channel_boost_jobs
            ADD COLUMN IF NOT EXISTS buyer_id BIGINT;
        ALTER TABLE telegram_channel_boost_jobs
            ADD COLUMN IF NOT EXISTS inventory_id BIGINT
                REFERENCES telegram_channel_boost_inventory(id) ON DELETE SET NULL;
        ALTER TABLE telegram_channel_boost_jobs ADD COLUMN IF NOT EXISTS rule_id BIGINT;
        ALTER TABLE telegram_channel_boost_jobs ADD COLUMN IF NOT EXISTS lot_id TEXT;
        ALTER TABLE telegram_channel_boost_jobs ADD COLUMN IF NOT EXISTS telethon_session_id BIGINT;
        ALTER TABLE telegram_channel_boost_inventory ADD COLUMN IF NOT EXISTS rule_id BIGINT;
        ALTER TABLE telegram_channel_boost_inventory ADD COLUMN IF NOT EXISTS lot_id TEXT;
        ALTER TABLE telegram_channel_boost_inventory ADD COLUMN IF NOT EXISTS lot_title TEXT;
        ALTER TABLE telegram_channel_boost_inventory ADD COLUMN IF NOT EXISTS telethon_session_id BIGINT;
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
    await _db().execute(
        """
        INSERT INTO telegram_channel_boost_lot_rules
            (telegram_id, lot_id, lot_title, service_id, quantity,
             target_members, min_ready_channels)
        SELECT telegram_id, lot_id, lot_title, service_id, quantity,
               target_members, min_ready_channels
          FROM telegram_channel_boost_settings
         WHERE telegram_id=$1 AND lot_id IS NOT NULL AND lot_title IS NOT NULL
           AND service_id IS NOT NULL
        ON CONFLICT (telegram_id, lot_id) DO NOTHING;

        UPDATE telegram_channel_boost_inventory AS inventory
           SET rule_id=rule.id, lot_id=rule.lot_id, lot_title=rule.lot_title
          FROM telegram_channel_boost_lot_rules AS rule
         WHERE inventory.telegram_id=$1 AND inventory.rule_id IS NULL
           AND rule.telegram_id=inventory.telegram_id
           AND rule.id=(
               SELECT MIN(single_rule.id)
                 FROM telegram_channel_boost_lot_rules AS single_rule
               WHERE single_rule.telegram_id=inventory.telegram_id
           );

        UPDATE telegram_channel_boost_jobs AS job
           SET rule_id=rule.id, lot_id=rule.lot_id,
               lot_title=COALESCE(job.lot_title, rule.lot_title)
          FROM telegram_channel_boost_lot_rules AS rule
         WHERE job.telegram_id=$1 AND job.rule_id IS NULL
           AND rule.telegram_id=job.telegram_id
           AND rule.id=(
               SELECT MIN(single_rule.id)
                 FROM telegram_channel_boost_lot_rules AS single_rule
                WHERE single_rule.telegram_id=job.telegram_id
           );

        UPDATE telegram_channel_boost_settings AS settings
           SET lot_id=NULL, lot_title=NULL, updated_at=NOW()
         WHERE settings.telegram_id=$1 AND settings.lot_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM telegram_channel_boost_lot_rules AS rule
                WHERE rule.telegram_id=settings.telegram_id
           );
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
        "min_ready_channels",
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
    )


def _rule_ready(rule: Any) -> bool:
    return bool(
        rule and rule["enabled"] and rule["lot_id"] and rule["lot_title"]
        and int(rule["service_id"] or 0) > 0
        and int(rule["quantity"] or 0) > 0
        and int(rule["target_members"] or 0) > 0
        and int(rule["min_ready_channels"] or 0) >= 1
    )


async def _rules(*, enabled_only: bool = False) -> list[Any]:
    await _ensure_schema()
    clause = " AND enabled=TRUE" if enabled_only else ""
    return list(await _db().fetch(
        f"""SELECT * FROM telegram_channel_boost_lot_rules
             WHERE telegram_id=$1{clause} ORDER BY created_at, id""",
        _telegram_id(),
    ))


async def _rule(rule_id: int) -> Any | None:
    await _ensure_schema()
    return await _db().fetchrow(
        """SELECT * FROM telegram_channel_boost_lot_rules
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), rule_id,
    )


async def _upsert_rule(lot_id: str, lot_title: str, service_id: int,
                       quantity: int, target_members: int,
                       min_ready_channels: int) -> Any:
    return await _db().fetchrow(
        """INSERT INTO telegram_channel_boost_lot_rules
               (telegram_id, lot_id, lot_title, service_id, quantity,
                target_members, min_ready_channels)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (telegram_id, lot_id) DO UPDATE SET
                lot_title=EXCLUDED.lot_title, service_id=EXCLUDED.service_id,
                quantity=EXCLUDED.quantity, target_members=EXCLUDED.target_members,
                min_ready_channels=EXCLUDED.min_ready_channels,
                enabled=TRUE, updated_at=NOW()
            RETURNING *""",
        _telegram_id(), lot_id, lot_title, service_id, quantity,
        target_members, min_ready_channels,
    )


async def _update_rule(rule_id: int, column: str, value: Any) -> None:
    if column not in {"service_id", "quantity", "target_members", "min_ready_channels", "enabled"}:
        raise ValueError("неизвестное поле позиции")
    await _db().execute(
        f"""UPDATE telegram_channel_boost_lot_rules
               SET {column}=$3, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), rule_id, value,
    )


async def _delete_rule(rule_id: int) -> None:
    active = await _db().fetchrow(
        """SELECT 1 FROM telegram_channel_boost_jobs
             WHERE telegram_id=$1 AND rule_id=$2
               AND status NOT IN ('completed','failed','canceled') LIMIT 1""",
        _telegram_id(), rule_id,
    )
    if active:
        raise RuntimeError("у позиции есть активный заказ; сначала дождитесь его завершения")
    await _db().execute(
        """UPDATE telegram_channel_boost_inventory SET status='failed',
                  error_text='Lot binding deleted', updated_at=NOW()
             WHERE telegram_id=$1 AND rule_id=$2 AND status NOT IN ('transferred','reserved');
            DELETE FROM telegram_channel_boost_lot_rules
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), rule_id,
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


async def _inventory_counts() -> Any:
    return await _db().fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status='ready') AS ready,
            COUNT(*) FILTER (WHERE status IN ('queued', 'boosting')) AS preparing,
            COUNT(*) FILTER (WHERE status='reserved') AS reserved,
            COUNT(*) FILTER (WHERE status='failed') AS failed
          FROM telegram_channel_boost_inventory
         WHERE telegram_id=$1
        """,
        _telegram_id(),
    )


async def _recent_inventory(limit: int = 5) -> list[Any]:
    return list(
        await _db().fetch(
            """
            SELECT * FROM telegram_channel_boost_inventory
             WHERE telegram_id=$1
             ORDER BY created_at DESC LIMIT $2
            """,
            _telegram_id(),
            limit,
        )
    )


def _status_label(status: str) -> str:
    return {
        "waiting_inventory": "ожидает готовый канал",
        "assigning": "резервируется канал",
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


def _inventory_status_label(status: str) -> str:
    return {
        "queued": "в очереди",
        "boosting": "готовится",
        "ready": "готов к продаже",
        "reserved": "зарезервирован покупателю",
        "transferred": "передан",
        "failed": "исключён из склада",
    }.get(status, status)


def _show_settings(chat_id: int) -> None:
    settings = _sync(_settings())
    rules = _sync(_rules())
    jobs = _sync(_recent_jobs())
    inventory = _sync(_recent_inventory())
    counts = _sync(_inventory_counts())
    account_count = len(_telethon_accounts())
    enabled_rules = sum(1 for rule in rules if rule["enabled"])
    lines = [
        "🚀 <b>Telegram Channel Boost</b>",
        "",
        f"Telegram-аккаунты: <b>{account_count} подключено</b>",
        f"API URL: <code>{html.escape(str(settings['api_base_url']))}</code>",
        f"API-токен: <b>{html.escape(_token_label(settings))}</b>",
        f"Позиции: <b>{enabled_rules}/{len(rules)} включено</b>",
        "",
        "<b>Склад каналов</b>",
        f"Готовы: <b>{int(counts['ready'] or 0)}</b> · готовятся: <b>{int(counts['preparing'] or 0)}</b> · "
        f"зарезервированы: <b>{int(counts['reserved'] or 0)}</b> · исключены: <b>{int(counts['failed'] or 0)}</b>",
        "",
        f"Готовность: <b>{'✅ настроено' if _settings_ready(settings) and account_count and enabled_rules else '⚠️ требуется настройка'}</b>",
        "",
        "Каждая позиция имеет свои услугу, количество, порог и запас. Каналы распределяются между Telegram-аккаунтами автоматически.",
    ]
    if rules:
        lines.extend(["", "<b>Привязанные позиции</b>"])
        for rule in rules[:12]:
            marker = "✅" if rule["enabled"] else "⏸"
            lines.append(
                f"{marker} {html.escape(str(rule['lot_title'])[:65])}\n"
                f"   услуга <code>{rule['service_id']}</code> · {rule['quantity']} SMM · "
                f"порог {rule['target_members']} · склад {rule['min_ready_channels']}"
            )
    if inventory:
        lines.extend(["", "<b>Последние каналы склада</b>"])
        for item in inventory:
            link = html.escape(str(item["channel_url"] or f"канал #{item['id']}"))
            refill = " · refill" if item["refill_pending"] else ""
            lines.append(
                f"• {html.escape(str(_row_get(item, 'lot_title', '—'))[:35])}: {link} — {html.escape(_inventory_status_label(item['status']))}{refill}; "
                f"{item['member_count']}/{item['target_members']}"
            )
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
        [("📱 Telegram-аккаунты", f"plugin_telethon:{UUID}")],
        [("➕ Добавить позицию", f"{CALLBACK_PREFIX}lots")],
        [("🧩 Управление позициями", f"{CALLBACK_PREFIX}rules")],
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
    existing_ids = {str(rule["lot_id"]) for rule in _sync(_rules())}
    lots = [lot for lot in lots if str(lot.id) not in existing_ids]
    if not lots:
        _bot().send_message(
            chat_id,
            "❌ Нет свободных лотов Telegram: все найденные уже привязаны либо профиль пуст.",
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
        "После выбора поэтапно задайте услугу, количество, порог и размер склада.",
        reply_markup=_markup(*rows),
    )


def _show_rules(chat_id: int) -> None:
    rules = _sync(_rules())
    rows = [[(
        f"{'✅' if rule['enabled'] else '⏸'} {str(rule['lot_title'])[:42]}",
        f"{CALLBACK_PREFIX}r:{rule['id']}",
    )] for rule in rules]
    rows.extend([
        [("➕ Добавить позицию", f"{CALLBACK_PREFIX}lots")],
        [("⬅️ Настройки", SETTINGS_CALLBACK)],
    ])
    _bot().send_message(
        chat_id,
        "🧩 <b>Позиции Telegram Channel Boost</b>\n\n"
        + ("Выберите позицию для настройки." if rules else "Пока нет привязанных лотов."),
        reply_markup=_markup(*rows),
    )


def _show_rule(chat_id: int, rule_id: int) -> None:
    rule = _sync(_rule(rule_id))
    if not rule:
        raise RuntimeError("позиция не найдена")
    text = (
        f"🛒 <b>{html.escape(str(rule['lot_title']))}</b>\n\n"
        f"ID лота: <code>{rule['lot_id']}</code>\n"
        f"ID услуги: <b>{rule['service_id']}</b>\n"
        f"Количество SMM: <b>{rule['quantity']}</b>\n"
        f"Порог подписчиков: <b>{rule['target_members']}</b>\n"
        f"Минимум готовых: <b>{rule['min_ready_channels']}</b>\n"
        f"Состояние: <b>{'включена' if rule['enabled'] else 'выключена'}</b>"
    )
    _bot().send_message(chat_id, text, reply_markup=_markup(
        [("🧩 Изменить ID услуги", f"{CALLBACK_PREFIX}rs:{rule_id}")],
        [("📈 Изменить количество", f"{CALLBACK_PREFIX}rq:{rule_id}")],
        [("🎯 Изменить порог", f"{CALLBACK_PREFIX}rtg:{rule_id}")],
        [("📦 Изменить склад", f"{CALLBACK_PREFIX}rst:{rule_id}")],
        [("⏸ Выключить" if rule["enabled"] else "▶️ Включить", f"{CALLBACK_PREFIX}re:{rule_id}")],
        [("🗑 Удалить", f"{CALLBACK_PREFIX}rd:{rule_id}")],
        [("⬅️ Все позиции", f"{CALLBACK_PREFIX}rules")],
    ))


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
        elif data == f"{CALLBACK_PREFIX}lots":
            _load_telegram_lots(chat_id)
        elif data == f"{CALLBACK_PREFIX}rules":
            _show_rules(chat_id)
        elif data.startswith(f"{CALLBACK_PREFIX}lot:"):
            lot_id = data.rsplit(":", 1)[1]
            title = _lot_cache.get(lot_id)
            if not title:
                raise RuntimeError("список лотов устарел; откройте его повторно")
            _draft_rule.clear()
            _draft_rule.update(lot_id=lot_id, lot_title=title)
            _prompt(chat_id, "new_service", "Шаг 1/4. Отправьте числовой ID SMM-услуги.")
        elif data.startswith(f"{CALLBACK_PREFIX}r:"):
            _show_rule(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith(f"{CALLBACK_PREFIX}rs:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_service", "Отправьте новый ID SMM-услуги.", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rq:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_quantity", "Отправьте новое количество SMM.", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rtg:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_target", "Отправьте новый порог подписчиков.", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rst:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_stock", "Отправьте новый минимум готовых каналов (1–20).", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}re:"):
            rule_id = int(data.rsplit(":", 1)[1])
            rule = _sync(_rule(rule_id))
            if not rule:
                raise RuntimeError("позиция не найдена")
            _sync(_update_rule(rule_id, "enabled", not bool(rule["enabled"])))
            _spawn(_maintain_inventory())
            _show_rule(chat_id, rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rd:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _bot().send_message(chat_id, "Удалить эту привязку? Готовые непроданные каналы позиции будут исключены.", reply_markup=_markup(
                [("Да, удалить", f"{CALLBACK_PREFIX}rx:{rule_id}")],
                [("Отмена", f"{CALLBACK_PREFIX}r:{rule_id}")],
            ))
        elif data.startswith(f"{CALLBACK_PREFIX}rx:"):
            _sync(_delete_rule(int(data.rsplit(":", 1)[1])))
            _bot().send_message(chat_id, "✅ Привязка удалена.")
            _show_rules(chat_id)
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
        elif key in {
            "new_service", "new_quantity", "new_target", "new_stock",
            "edit_service", "edit_quantity", "edit_target", "edit_stock",
        }:
            if not value.isdigit():
                raise ValueError("нужно отправить целое положительное число")
            number = int(value)
            maximum = 20 if key in {"new_stock", "edit_stock"} else 1_000_000
            if not 1 <= number <= maximum:
                raise ValueError(f"значение должно быть от 1 до {maximum}")
            if key == "new_service":
                _draft_rule["service_id"] = number
                _prompt(chat_id, "new_quantity", "Шаг 2/4. Отправьте количество для SMM-заказа.")
                return
            if key == "new_quantity":
                _draft_rule["quantity"] = number
                _prompt(chat_id, "new_target", "Шаг 3/4. При каком фактическом числе подписчиков канал считать готовым?")
                return
            if key == "new_target":
                _draft_rule["target_members"] = number
                _prompt(chat_id, "new_stock", "Шаг 4/4. Сколько готовых каналов этой позиции держать на складе (1–20)?")
                return
            if key == "new_stock":
                required = {"lot_id", "lot_title", "service_id", "quantity", "target_members"}
                if not required.issubset(_draft_rule):
                    raise RuntimeError("мастер устарел; выберите лот заново")
                _sync(_upsert_rule(
                    str(_draft_rule["lot_id"]), str(_draft_rule["lot_title"]),
                    int(_draft_rule["service_id"]), int(_draft_rule["quantity"]),
                    int(_draft_rule["target_members"]), number,
                ))
                _draft_rule.clear()
            else:
                if job_id is None:
                    raise RuntimeError("позиция не выбрана")
                column = {
                    "edit_service": "service_id", "edit_quantity": "quantity",
                    "edit_target": "target_members", "edit_stock": "min_ready_channels",
                }[key]
                _sync(_update_rule(job_id, column, number))
        _bot().send_message(chat_id, "✅ Настройка сохранена.")
        _spawn(_maintain_inventory())
        _show_settings(chat_id)
    except Exception as exc:
        logger.exception("Настройка Telegram Channel Boost не сохранена")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


def _normalized_order_title(value: str) -> str:
    value = re.sub(r",\s*\d{1,3}(?:\s?\d{3})*\s*(?:шт|pcs)\.\s*$", "", value, flags=re.I)
    return " ".join(value.split()).casefold()


def _match_rule(description: str, rules: list[Any], lot_id: str | None = None) -> Any | None:
    enabled = [rule for rule in rules if _rule_ready(rule)]
    if lot_id is not None:
        return next((rule for rule in enabled if str(rule["lot_id"]) == str(lot_id)), None)
    normalized = _normalized_order_title(description)
    matches = [rule for rule in enabled
               if _normalized_order_title(str(rule["lot_title"])) == normalized]
    return matches[0] if len(matches) == 1 else None


async def _insert_job(order: dict[str, Any], rule: Any) -> Any | None:
    return await _db().fetchrow(
        """
        INSERT INTO telegram_channel_boost_jobs
            (telegram_id, order_id, chat_id, chat_name, buyer_id, rule_id,
             lot_id, lot_title, target_members, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'waiting_inventory')
        ON CONFLICT (telegram_id, order_id) DO NOTHING
        RETURNING *
        """,
        _telegram_id(),
        order["id"],
        str(order["chat_id"]),
        order["chat_name"],
        order["buyer_id"],
        int(rule["id"]),
        str(rule["lot_id"]),
        str(rule["lot_title"]),
        int(rule["target_members"]),
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
        "inventory_id",
        "telethon_session_id",
        "smm_order_id",
        "smm_status",
        "member_count",
        "target_members",
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


async def _claim_ready_inventory(order_id: str, rule_id: int) -> Any | None:
    return await _db().fetchrow(
        """
        WITH candidate AS (
            SELECT id
              FROM telegram_channel_boost_inventory
             WHERE telegram_id=$1 AND status='ready' AND rule_id=$3
             ORDER BY ready_at, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        )
        UPDATE telegram_channel_boost_inventory AS inventory
           SET status='reserved', reserved_order_id=$2, updated_at=NOW()
          FROM candidate
         WHERE inventory.id=candidate.id
        RETURNING inventory.*
        """,
        _telegram_id(),
        order_id,
        rule_id,
    )


async def _assign_inventory_to_job(job_id: int) -> bool:
    job = await _db().fetchrow(
        """
        UPDATE telegram_channel_boost_jobs
           SET status='assigning', updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2 AND status='waiting_inventory'
        RETURNING *
        """,
        _telegram_id(),
        job_id,
    )
    if not job:
        return False
    claimed_item: Any | None = None
    assignment_saved = False
    try:
        settings = await _settings()
        while True:
            item = await _claim_ready_inventory(str(job["order_id"]), int(job["rule_id"]))
            if not item:
                await _update_job(job_id, status="waiting_inventory")
                return False
            claimed_item = item
            try:
                members = await _member_count(item)
            except Exception:
                members = int(item["member_count"] or 0)
                logger.warning(
                    "Не выполнена финальная проверка канала склада %s",
                    item["id"],
                    exc_info=True,
                )
            required_members = max(
                int(item["target_members"]), int(job["target_members"])
            )
            if members < required_members:
                await _update_inventory(
                    item["id"],
                    member_count=members,
                    target_members=required_members,
                    status="boosting",
                    reserved_order_id=None,
                )
                item = await _inventory_item(int(item["id"]))
                await _request_inventory_refill(item, settings)
                _launch_inventory_item(int(item["id"]))
                claimed_item = None
                continue
            assigned_job = await _db().fetchrow(
                """
                UPDATE telegram_channel_boost_jobs
                   SET inventory_id=$3, channel_id=$4, channel_access_hash=$5,
                       channel_username=$6, channel_url=$7, smm_order_id=$8,
                       smm_status=$9, member_count=$10, target_members=$11,
                       telethon_session_id=$12, status='awaiting_username',
                       error_text=NULL, updated_at=NOW()
                 WHERE telegram_id=$1 AND id=$2 AND status='assigning'
                RETURNING *
                """,
                _telegram_id(),
                job_id,
                int(item["id"]),
                int(item["channel_id"]),
                int(item["channel_access_hash"]),
                str(item["channel_username"]),
                str(item["channel_url"]),
                str(item["smm_order_id"]),
                str(item["smm_status"] or "Completed"),
                members,
                required_members,
                item["telethon_session_id"],
            )
            if not assigned_job:
                await _update_inventory(
                    int(item["id"]), status="ready", reserved_order_id=None
                )
                return False
            assignment_saved = True
            claimed_item = None
            job = assigned_job
            try:
                await _funpay_send(
                    job,
                    "✅ Ваш заранее подготовленный Telegram-канал готов:\n"
                    f"{job['channel_url']}\n\n"
                    "1. Вступите в канал.\n"
                    "2. Отправьте сюда свой Telegram username строго в формате @username.\n"
                    "После этого бот попросит подтвердить написание username.",
                )
            except Exception:
                logger.exception("Не отправлена ссылка готового канала покупателю")
            try:
                await _notify_owner(
                    "📦 <b>Готовый канал выдан со склада</b>\n\n"
                    f"Заказ: <code>#{html.escape(str(job['order_id']))}</code>\n"
                    f"Канал: {html.escape(str(job['channel_url']))}\n"
                    f"Подписчики: <b>{members}/{job['target_members']}</b>"
                )
            except Exception:
                logger.exception("Не отправлено уведомление о выдаче канала")
            return True
    except Exception as exc:
        if assignment_saved:
            await _update_job(job_id, status="awaiting_username", error_text=str(exc)[:1000])
            logger.exception("Канал закреплён, но выдача завершилась с ошибкой")
            return True
        if claimed_item:
            await _update_inventory(
                int(claimed_item["id"]),
                status="ready",
                reserved_order_id=None,
            )
        await _update_job(job_id, status="waiting_inventory", error_text=str(exc)[:1000])
        logger.exception("Не выделен канал для заказа %s", job["order_id"])
        return False


async def _assign_waiting_jobs() -> None:
    rows = await _db().fetch(
        """
        SELECT id FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1 AND status='waiting_inventory'
         ORDER BY created_at
        """,
        _telegram_id(),
    )
    for row in rows:
        await _assign_inventory_to_job(int(row["id"]))


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


def _telethon_accounts() -> list[tuple[int, Any]]:
    service = getattr(getattr(_cardinal, "plugin_manager", None), "telethon_service", None)
    if service is None:
        return []
    result: list[tuple[int, Any]] = []
    for client in service.get_clients(_telegram_id(), UUID):
        if not client.is_connected():
            continue
        session_id = service.session_id_for_client(_telegram_id(), UUID, client)
        if session_id is not None:
            result.append((int(session_id), client))
    return result


def _client_for(row: Any) -> Any:
    session_id = _row_get(row, "telethon_session_id")
    service = getattr(getattr(_cardinal, "plugin_manager", None), "telethon_service", None)
    client = (
        service.get_client_by_session(_telegram_id(), UUID, int(session_id))
        if service is not None and session_id is not None else _client
    )
    if client is None or not client.is_connected():
        raise RuntimeError("Telegram-аккаунт канала не подключён")
    return client


async def _password_for(row: Any) -> str | None:
    service = getattr(getattr(_cardinal, "plugin_manager", None), "telethon_service", None)
    if service is None:
        return await _stored_2fa_password()
    session_id = _row_get(row, "telethon_session_id")
    return await service.get_2fa_password(
        _telegram_id(), UUID, int(session_id) if session_id is not None else None
    )


async def _select_telethon_account() -> tuple[int, Any]:
    accounts = _telethon_accounts()
    if not accounts:
        raise RuntimeError("нет подключённых Telegram-аккаунтов")
    usage_rows = await _db().fetch(
        """SELECT telethon_session_id, COUNT(*) AS count
             FROM telegram_channel_boost_inventory
            WHERE telegram_id=$1 AND status NOT IN ('transferred','failed')
            GROUP BY telethon_session_id""",
        _telegram_id(),
    )
    usage = {int(row["telethon_session_id"]): int(row["count"])
             for row in usage_rows if row["telethon_session_id"] is not None}
    return min(accounts, key=lambda pair: (usage.get(pair[0], 0), pair[0]))


async def _create_public_channel(reference: str, client: Any | None = None) -> tuple[Any, str]:
    client = client or _client
    if client is None or not client.is_connected():
        raise RuntimeError("Telethon не подключён")
    result = await client(
        CreateChannelRequest(
            title=f"Ready Telegram channel {reference}",
            about="Канал заранее подготовлен для автоматической выдачи на FunPay.",
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
            await client(UpdateUsernameRequest(channel, username))
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
    client = _client_for(job)
    participants = await client.get_participants(_input_channel(job), limit=0)
    return int(getattr(participants, "total", len(participants)))


async def _inventory_item(item_id: int) -> Any | None:
    return await _db().fetchrow(
        """SELECT * FROM telegram_channel_boost_inventory
            WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(),
        item_id,
    )


async def _update_inventory(item_id: int, **values: Any) -> None:
    allowed = {
        "channel_id", "channel_access_hash", "channel_username", "channel_url",
        "smm_order_id", "smm_status", "member_count", "target_members", "refill_id",
        "refill_pending", "last_refill_at", "reserved_order_id", "status",
        "error_text", "ready_at",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(
        f"{key}=${index + 3}" for index, key in enumerate(values)
    )
    await _db().execute(
        f"""UPDATE telegram_channel_boost_inventory
               SET {assignments}, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(),
        item_id,
        *values.values(),
    )


async def _insert_inventory_item(rule: Any) -> Any:
    session_id, _selected_client = await _select_telethon_account()
    return await _db().fetchrow(
        """INSERT INTO telegram_channel_boost_inventory
               (telegram_id, rule_id, lot_id, lot_title, telethon_session_id,
                service_id, quantity, target_members, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'queued')
            RETURNING *""",
        _telegram_id(),
        int(rule["id"]), str(rule["lot_id"]), str(rule["lot_title"]), session_id,
        int(rule["service_id"]), int(rule["quantity"]), int(rule["target_members"]),
    )


def _refill_due(item: Any, now: datetime | None = None) -> bool:
    if item["refill_pending"]:
        return False
    last = item["last_refill_at"]
    if not last:
        return True
    current = now or datetime.now(timezone.utc)
    if getattr(last, "tzinfo", None) is None:
        last = last.replace(tzinfo=timezone.utc)
    return (current - last).total_seconds() >= INVENTORY_CHECK_SECONDS


async def _request_inventory_refill(item: Any, settings: Any) -> bool:
    if not item["smm_order_id"] or not _refill_due(item):
        return False
    await _update_inventory(item["id"], last_refill_at=datetime.now(timezone.utc))
    try:
        response = await asyncio.to_thread(
            _smm_request,
            settings,
            action="refill",
            order=item["smm_order_id"],
        )
        refill_id = response.get("refill")
        if refill_id is None or str(refill_id).strip() == "":
            raise RuntimeError("SMM API не вернул ID рефилла")
    except Exception as exc:
        await _update_inventory(item["id"], error_text=f"Refill: {str(exc)[:700]}")
        logger.warning("Не запрошен refill канала склада %s", item["id"], exc_info=True)
        return False
    await _update_inventory(
        item["id"],
        refill_id=str(refill_id),
        refill_pending=True,
        smm_status="Refill requested",
        status="boosting",
        error_text=None,
    )
    await _notify_owner(
        "♻️ <b>Запрошен refill готового Telegram-канала</b>\n\n"
        f"Канал: {html.escape(str(item['channel_url']))}\n"
        f"Подписчики: <b>{item['member_count']}/{item['target_members']}</b>\n"
        f"SMM order: <code>{html.escape(str(item['smm_order_id']))}</code>\n"
        f"Refill: <code>{html.escape(str(refill_id))}</code>"
    )
    return True


def _track_async_task(task: asyncio.Task[Any], error_label: str) -> None:
    _inventory_tasks.add(task)

    def done(completed: asyncio.Task[Any]) -> None:
        _inventory_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception("%s завершилась с ошибкой", error_label)

    task.add_done_callback(done)


def _launch_inventory_item(item_id: int) -> None:
    if item_id in _running_inventory_ids:
        return
    _track_async_task(
        asyncio.create_task(_run_inventory_item(item_id)),
        "Подготовка канала склада",
    )


async def _run_inventory_item(item_id: int) -> None:
    if item_id in _running_inventory_ids:
        return
    _running_inventory_ids.add(item_id)
    started_at = time.monotonic()
    try:
        item = await _inventory_item(item_id)
        if not item or item["status"] not in {"queued", "boosting"}:
            return
        settings = await _settings()
        rule = await _rule(int(item["rule_id"])) if item["rule_id"] else None
        if not _settings_ready(settings) or not _rule_ready(rule):
            return
        while True:
            try:
                account_client = _client_for(item)
                break
            except RuntimeError:
                account_client = None
            if not _cardinal.plugin_manager.is_enabled(_telegram_id(), UUID):
                return
            if time.monotonic() - started_at > 600:
                raise RuntimeError("Telethon не подключён в течение 10 минут")
            await asyncio.sleep(5)
        if not item["channel_id"]:
            channel, username = await _create_public_channel(f"stock-{item_id}", account_client)
            await _update_inventory(
                item_id,
                channel_id=int(channel.id),
                channel_access_hash=int(channel.access_hash),
                channel_username=username,
                channel_url=f"https://t.me/{username}",
                status="boosting",
            )
            item = await _inventory_item(item_id)
        if not item["smm_order_id"]:
            response = await asyncio.to_thread(
                _smm_request,
                settings,
                action="add",
                service=int(item["service_id"]),
                link=item["channel_url"],
                quantity=int(item["quantity"]),
            )
            smm_order_id = response.get("order")
            if not smm_order_id:
                raise RuntimeError("SMM API не вернул ID заказа")
            await _update_inventory(
                item_id,
                smm_order_id=str(smm_order_id),
                smm_status="Pending",
                status="boosting",
            )
            item = await _inventory_item(item_id)
            await _notify_owner(
                "🚀 <b>Началась предварительная подготовка Telegram-канала</b>\n\n"
                f"Канал: {html.escape(str(item['channel_url']))}\n"
                f"SMM order: <code>{html.escape(str(item['smm_order_id']))}</code>\n"
                f"Цель: <b>{item['target_members']} подписчиков</b>"
            )
        while True:
            item = await _inventory_item(item_id)
            if not item or item["status"] != "boosting":
                return
            if not _cardinal.plugin_manager.is_enabled(_telegram_id(), UUID):
                return
            smm_status = str(item["smm_status"] or "Unknown")
            try:
                response = await asyncio.to_thread(
                    _smm_request,
                    settings,
                    action="status",
                    order=item["smm_order_id"],
                )
                smm_status = str(response.get("status", smm_status))
            except Exception:
                logger.warning("Не проверен SMM order склада %s", item["smm_order_id"], exc_info=True)
            try:
                members = await _member_count(item)
            except Exception:
                logger.warning("Не проверены подписчики канала склада %s", item_id, exc_info=True)
                members = int(item["member_count"] or 0)
            await _update_inventory(item_id, smm_status=smm_status, member_count=members)
            if members >= int(item["target_members"]):
                await _update_inventory(
                    item_id,
                    status="ready",
                    refill_pending=False,
                    error_text=None,
                    ready_at=datetime.now(timezone.utc),
                )
                await _notify_owner(
                    "✅ <b>Telegram-канал добавлен на склад</b>\n\n"
                    f"Канал: {html.escape(str(item['channel_url']))}\n"
                    f"Подписчики: <b>{members}/{item['target_members']}</b>"
                )
                await _assign_waiting_jobs()
                asyncio.create_task(_maintain_inventory())
                return
            if smm_status.casefold() in {
                "partial", "canceled", "cancelled", "error", "refunded",
                "completed", "complete", "done",
            }:
                item = await _inventory_item(item_id)
                await _request_inventory_refill(item, settings)
            if time.monotonic() - started_at >= JOB_TIMEOUT_SECONDS:
                raise RuntimeError("истёк 24-часовой срок подготовки канала")
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Ошибка подготовки канала склада %s", item_id)
        await _update_inventory(item_id, status="failed", error_text=str(exc)[:1000])
        await _notify_owner(
            "❌ <b>Не подготовлен Telegram-канал для склада</b>\n\n"
            f"Запись: <code>#{item_id}</code>\n"
            f"Ошибка: <code>{html.escape(str(exc)[:700])}</code>"
        )
        asyncio.create_task(_maintain_inventory())
    finally:
        _running_inventory_ids.discard(item_id)


async def _check_ready_inventory(settings: Any, rule: Any | None = None) -> None:
    rows = await _db().fetch(
        """SELECT * FROM telegram_channel_boost_inventory
            WHERE telegram_id=$1 AND status='ready'
              AND ($2::BIGINT IS NULL OR rule_id=$2)
              AND EXISTS (
                  SELECT 1 FROM telegram_channel_boost_lot_rules AS rule
                   WHERE rule.id=telegram_channel_boost_inventory.rule_id
                     AND rule.enabled=TRUE
              )
            ORDER BY ready_at, id""",
        _telegram_id(),
        int(rule["id"]) if rule else None,
    )
    for item in rows:
        try:
            members = await _member_count(item)
        except Exception:
            logger.warning("Не проверен готовый канал %s", item["id"], exc_info=True)
            continue
        await _update_inventory(item["id"], member_count=members)
        if members >= int(item["target_members"]):
            continue
        await _update_inventory(item["id"], status="boosting")
        item = await _inventory_item(int(item["id"]))
        await _request_inventory_refill(item, settings)
        _launch_inventory_item(int(item["id"]))


async def _maintain_inventory() -> None:
    global _inventory_maintenance_lock
    if _inventory_maintenance_lock is None:
        _inventory_maintenance_lock = asyncio.Lock()
    async with _inventory_maintenance_lock:
        settings = await _settings()
        rules = await _rules(enabled_only=True)
        if (
            not _settings_ready(settings)
            or not _telethon_accounts()
            or not rules
            or not _cardinal.plugin_manager.is_enabled(_telegram_id(), UUID)
        ):
            return
        await _check_ready_inventory(settings)
        await _assign_waiting_jobs()
        preparing = await _db().fetch(
            """SELECT id FROM telegram_channel_boost_inventory
                WHERE telegram_id=$1 AND status IN ('queued', 'boosting')
                  AND EXISTS (
                      SELECT 1 FROM telegram_channel_boost_lot_rules AS rule
                       WHERE rule.id=telegram_channel_boost_inventory.rule_id
                         AND rule.enabled=TRUE
                  )
                ORDER BY created_at""",
            _telegram_id(),
        )
        for item in preparing:
            _launch_inventory_item(int(item["id"]))
        for rule in rules:
            count = await _db().fetchrow(
                """SELECT COUNT(*) AS count FROM telegram_channel_boost_inventory
                    WHERE telegram_id=$1 AND rule_id=$2
                      AND (
                          status IN ('ready', 'queued')
                          OR (status='boosting' AND last_refill_at IS NULL)
                      )""",
                _telegram_id(), int(rule["id"]),
            )
            shortage = max(
                int(rule["min_ready_channels"]) - int(count["count"] if count else 0),
                0,
            )
            for _ in range(shortage):
                item = await _insert_inventory_item(rule)
                _launch_inventory_item(int(item["id"]))


async def _inventory_loop() -> None:
    while True:
        try:
            await _maintain_inventory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка пятиминутной проверки склада Telegram-каналов")
        await asyncio.sleep(INVENTORY_CHECK_SECONDS)


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
    rule = _match_rule(
        str(order.get("description") or ""),
        await _rules(enabled_only=True),
        str(order["lot_id"]) if order.get("lot_id") is not None else None,
    )
    if not rule:
        return
    if not _settings_ready(settings):
        await _notify_owner(
            "❌ Получен заказ для Telegram Channel Boost, но настройки заполнены не полностью. "
            f"Заказ: <code>#{html.escape(order['id'])}</code>"
        )
        return
    job = await _insert_job(order, rule)
    if not job:
        return
    if not await _assign_inventory_to_job(int(job["id"])):
        job = await _job(int(job["id"]))
        await _funpay_send(
            job,
            "⏳ Заказ принят. Готовый канал сейчас резервируется или склад пополняется. "
            "Ссылка будет отправлена автоматически, как только канал достигнет заданного порога.",
        )
        await _notify_owner(
            "⚠️ <b>На складе временно нет готового Telegram-канала</b>\n\n"
            f"Заказ: <code>#{html.escape(str(job['order_id']))}</code>\n"
            "Пополнение запущено автоматически."
        )
    await _maintain_inventory()


async def _resume_jobs() -> None:
    await _ensure_schema()
    await _db().execute(
        """
        UPDATE telegram_channel_boost_jobs
           SET status='waiting_inventory', updated_at=NOW()
         WHERE telegram_id=$1 AND status='assigning';
        UPDATE telegram_channel_boost_inventory AS inventory
           SET status='ready', reserved_order_id=NULL, updated_at=NOW()
         WHERE inventory.telegram_id=$1
           AND inventory.status='reserved'
           AND NOT EXISTS (
               SELECT 1 FROM telegram_channel_boost_jobs AS job
                WHERE job.telegram_id=$1
                  AND job.inventory_id=inventory.id
                  AND job.status NOT IN ('completed', 'failed', 'canceled')
           );
        """,
        _telegram_id(),
    )
    rows = await _db().fetch(
        """
        SELECT id FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1 AND status IN ('queued', 'boosting')
         ORDER BY created_at
        """,
        _telegram_id(),
    )
    for row in rows:
        _track_async_task(
            asyncio.create_task(_run_job(int(row["id"]))),
            "Восстановление старого задания Telegram Channel Boost",
        )
    await _assign_waiting_jobs()
    await _maintain_inventory()


async def _resume_transfers() -> None:
    rows = await _db().fetch(
        """
        SELECT id FROM telegram_channel_boost_jobs
         WHERE telegram_id=$1 AND status='awaiting_owner_2fa'
         ORDER BY created_at
        """,
        _telegram_id(),
    )
    transfers = []
    for row in rows:
        job = await _job(int(row["id"]))
        password = await _password_for(job) if job else None
        if password:
            transfers.append(_transfer_owner(int(row["id"]), password))
    await asyncio.gather(*transfers, return_exceptions=True)


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
    try:
        account_client = _client_for(job)
    except RuntimeError:
        await _funpay_send(job, "Telethon временно не подключён. Продавец уже уведомлён.")
        await _notify_owner("⚠️ Для проверки покупателя подключите Telethon в настройках плагина.")
        return
    try:
        user = await account_client.get_entity(username)
        await account_client(GetParticipantRequest(_input_channel(job), user))
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
    password = await _password_for(job)
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
        try:
            account_client = _client_for(job)
        except RuntimeError:
            await _notify_owner("❌ Telethon не подключён; автоматическая передача канала отложена.")
            return
        user = await account_client.get_entity(job["buyer_username"])
        channel = _input_channel(job)
        await account_client(GetParticipantRequest(channel, user))
        password_state = await account_client(GetPasswordRequest())
        password_check = await asyncio.to_thread(
            compute_check, password_state, password
        )
        await account_client(GetPasswordSettingsRequest(password_check))
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
        await account_client(EditAdminRequest(channel, user, rights, rank="Владелец"))
        promoted = True
        password_state = await account_client(GetPasswordRequest())
        password_check = await asyncio.to_thread(
            compute_check, password_state, password
        )
        await account_client(EditChatCreatorRequest(channel, user, password_check))
    except PasswordHashInvalidError:
        if promoted and channel is not None and user is not None:
            await _revoke_admin(channel, user, account_client)
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
            await _revoke_admin(channel, user, account_client)
        logger.exception("Не удалось передать владельца Telegram-канала")
        await _notify_owner(
            "❌ Telegram не передал владельца. Проверьте, что 2FA включена более 7 дней, "
            "текущая сессия активна более 24 часов и на аккаунте нет ограничения публичных каналов.\n\n"
            f"Ошибка: <code>{html.escape(str(exc)[:500])}</code>",
        )
        return
    else:
        await _update_job(job_id, status="completed", error_text=None)
        inventory_id = _row_get(job, "inventory_id")
        if inventory_id:
            await _update_inventory(
                int(inventory_id),
                status="transferred",
                refill_pending=False,
                error_text=None,
            )
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


async def _revoke_admin(channel: InputChannel, user: Any, client: Any | None = None) -> None:
    try:
        await (client or _client)(EditAdminRequest(channel, user, ChatAdminRights(), rank=""))
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
    inventory_id = _row_get(job, "inventory_id")
    if inventory_id:
        await _update_inventory(
            int(inventory_id),
            status="failed",
            refill_pending=False,
            error_text=f"FunPay order {status}",
        )
        asyncio.create_task(_maintain_inventory())
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
    global _cardinal, _client, _inventory_loop_future
    _cardinal = cardinal
    _client = client
    _spawn(_resume_jobs())
    _spawn(_resume_transfers())
    if _inventory_loop_future is None or _inventory_loop_future.done():
        _inventory_loop_future = _spawn(_inventory_loop())


def telethon_disconnected(cardinal: Any, client: Any) -> None:
    global _client
    if _client is client:
        _client = next(
            (current for _session_id, current in _telethon_accounts()
             if current is not client),
            None,
        )


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
        "lot_id": _order_lot_id(order),
    }
    _spawn(_process_new_order(payload))


def _order_lot_id(order: Any) -> str | None:
    direct = getattr(order, "lot_id", None)
    if direct is not None:
        return str(direct)
    widget_html = str(getattr(order, "html", "") or "")
    for pattern in (r"(?:lots/offer\?id=|offer=|data-offer=[\"'])(\d+)",):
        match = re.search(pattern, widget_html, re.I)
        if match:
            return match.group(1)
    return None


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
    global _client, _inventory_loop_future
    _client = None
    for future in list(_futures):
        future.cancel()
    _inventory_loop_future = None

    def cancel_tasks() -> None:
        for task in list(_inventory_tasks):
            task.cancel()

    cardinal.telegram.loop.call_soon_threadsafe(cancel_tasks)


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
            "DELETE FROM telegram_channel_boost_inventory WHERE telegram_id=$1",
            _telegram_id(),
        )
    )
    _sync(
        _db().execute(
            "DELETE FROM telegram_channel_boost_settings WHERE telegram_id=$1",
            _telegram_id(),
        )
    )
    _sync(
        _db().execute(
            "DELETE FROM telegram_channel_boost_lot_rules WHERE telegram_id=$1",
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
