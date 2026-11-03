from __future__ import annotations

import asyncio
import html
import logging
import re
from concurrent.futures import CancelledError, Future
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

NAME = "Spotify Account Shop"
VERSION = "1.0.2"
DESCRIPTION = "Склад Spotify-аккаунтов: привязка лотов, автовыдача и напоминание об отзыве"
CREDITS = "FunPay aiogram bot"
SETTINGS_PAGE = True
TELETHON = False
UUID = "9d378672-7534-41b2-ab16-87057c697f37"

CALLBACK_PREFIX = "sps:"
STOCK_PENDING_KEY = "__stock__"
PLUGIN_SETTINGS_PREFIX = "47"
SETTINGS_CALLBACK = f"{PLUGIN_SETTINGS_PREFIX}:{UUID}:0"
DEFAULT_POSITION_DAYS = 30
MAX_FOLLOWUP_MINUTES = 7 * 24 * 60
MAX_STOCK_LINES = 500
MAX_TEMPLATE_LENGTH = 3500
WORKER_SECONDS = 45
FOLLOWUP_RETRY_MINUTES = 5
FOLLOWUP_MAX_ATTEMPTS = 3
SECRET_PREFIX = "enc1:"
LOT_PAGE_SIZE = 8
STOCK_PAGE_SIZE = 8
POLL_SECONDS = 90
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AMOUNT_RE = re.compile(
    r",\s*\d{1,3}(?:\s?\d{3})*\s*(?:шт|pcs)\.\s*$", re.IGNORECASE
)

CONFIRM_PAID_HINT = (
    "Подтвердите, пожалуйста, выполнение заказа: "
    "«Заказы» → «Подтвердить выполнение»."
)
CONFIRM_CLOSED_HINT = "Заказ уже подтверждён — спасибо!"

DEFAULT_DELIVERY_TEMPLATE = (
    "🎧 Ваш аккаунт Spotify готов!\n"
    "\n"
    "📧 Почта: {email}\n"
    "🔑 Пароль: {password}\n"
    "🗝 Ключ активации: {key}\n"
    "📅 Подписка: {days} дн. (до {until})\n"
    "\n"
    "Как войти:\n"
    "1. Откройте spotify.com или приложение Spotify.\n"
    "2. Войдите с указанными почтой и паролем.\n"
    "3. Не меняйте пароль и не отвязывайте почту — это может прервать подписку.\n"
    "\n"
    "Если что-то не работает, напишите в этот чат — поможем.\n"
    "Приятного прослушивания!"
)

DEFAULT_FOLLOWUP_TEMPLATE = (
    "🙌 Спасибо за покупку Spotify!\n"
    "\n"
    "{confirm_hint}\n"
    "\n"
    "Если всё работает, оставьте, пожалуйста, отзыв — это очень помогает магазину:\n"
    "{review_url}\n"
    "\n"
    "Заказ: #{order_id}\n"
    "Аккаунт: {email}\n"
    "Если появятся вопросы, просто ответьте на это сообщение."
)

NO_STOCK_MESSAGE = (
    "⏳ Заказ оплачен, но свободные аккаунты на складе закончились.\n"
    "Склад уже пополняется — аккаунт придёт в этот чат автоматически, "
    "ничего дополнительно делать не нужно."
)

logger = logging.getLogger("fpc_plugin.spotify_account_shop")

_cardinal: Any | None = None
_pending_input: tuple[str, int | None] | None = None
_stock_mode: dict[str, int] | None = None
_worker_future: Future[Any] | None = None
_futures: set[Future[Any]] = set()

# --------------------------------------------------------------------------- #
# Базовые помощники
# --------------------------------------------------------------------------- #

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
    service = (
        getattr(_cardinal.plugin_manager, "telethon_service", None)
        if _cardinal is not None
        else None
    )
    secrets = getattr(service, "secrets", None)
    if secrets is None:
        raise RuntimeError("хранилище секретов недоступно")
    return secrets

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
            logger.exception("Фоновая задача Spotify Account Shop завершилась с ошибкой")

    future.add_done_callback(done)
    return future

def _encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return SECRET_PREFIX + _secret_box().encrypt(value)
    except Exception:
        logger.warning(
            "Секрет склада сохранён без шифрования: хранилище недоступно",
            exc_info=True,
        )
        return value

def _decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    if not value.startswith(SECRET_PREFIX):
        return value
    return _secret_box().decrypt(value[len(SECRET_PREFIX):])

# --------------------------------------------------------------------------- #
# Схема БД
# --------------------------------------------------------------------------- #

async def _ensure_schema() -> None:
    await _db().execute(
        """
        CREATE TABLE IF NOT EXISTS spotify_accounts_settings (
            telegram_id BIGINT PRIMARY KEY
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            followup_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            followup_delay_minutes INTEGER NOT NULL DEFAULT 60,
            review_url TEXT NOT NULL DEFAULT 'https://funpay.com/orders/',
            delivery_template TEXT,
            followup_template TEXT,
            low_stock_threshold INTEGER NOT NULL DEFAULT 2,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS spotify_accounts_positions (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            subscription_days INTEGER NOT NULL DEFAULT 30,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            low_stock_notified BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS spotify_accounts_bindings (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            position_id BIGINT NOT NULL
                REFERENCES spotify_accounts_positions(id) ON DELETE CASCADE,
            lot_id TEXT NOT NULL,
            lot_title TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, lot_id)
        );

        CREATE TABLE IF NOT EXISTS spotify_accounts_items (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            position_id BIGINT NOT NULL
                REFERENCES spotify_accounts_positions(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            password TEXT NOT NULL,
            activation_key TEXT,
            status TEXT NOT NULL DEFAULT 'available',
            reserved_order_id TEXT,
            order_id TEXT,
            sold_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS spotify_accounts_orders (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            order_id TEXT NOT NULL,
            position_id BIGINT
                REFERENCES spotify_accounts_positions(id) ON DELETE SET NULL,
            lot_id TEXT,
            position_title TEXT,
            subscription_days INTEGER,
            amount INTEGER NOT NULL DEFAULT 1,
            item_ids BIGINT[] NOT NULL DEFAULT '{}',
            buyer_username TEXT,
            chat_id TEXT,
            chat_name TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            error_text TEXT,
            delivered_at TIMESTAMPTZ,
            followup_at TIMESTAMPTZ,
            followup_status TEXT NOT NULL DEFAULT 'none',
            followup_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, order_id)
        );

        CREATE INDEX IF NOT EXISTS spotify_accounts_items_stock_idx
            ON spotify_accounts_items (telegram_id, position_id, status, id);
        CREATE INDEX IF NOT EXISTS spotify_accounts_orders_status_idx
            ON spotify_accounts_orders (telegram_id, status, created_at);
        CREATE INDEX IF NOT EXISTS spotify_accounts_followup_idx
            ON spotify_accounts_orders (telegram_id, followup_status, followup_at);
        """
    )
    await _db().execute(
        """
        INSERT INTO spotify_accounts_settings (telegram_id)
        VALUES ($1)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        _telegram_id(),
    )

async def _settings() -> Any:
    await _ensure_schema()
    return await _db().fetchrow(
        "SELECT * FROM spotify_accounts_settings WHERE telegram_id=$1",
        _telegram_id(),
    )

async def _update_setting(column: str, value: Any) -> None:
    allowed = {
        "followup_enabled",
        "followup_delay_minutes",
        "review_url",
        "delivery_template",
        "followup_template",
        "low_stock_threshold",
    }
    if column not in allowed:
        raise ValueError("неизвестная настройка")
    await _db().execute(
        f"""
        UPDATE spotify_accounts_settings
           SET {column}=$2, updated_at=NOW()
         WHERE telegram_id=$1
        """,
        _telegram_id(),
        value,
    )

# --------------------------------------------------------------------------- #
# Позиции, привязки, склад
# --------------------------------------------------------------------------- #

_POSITION_SELECT = """
    SELECT pos.*,
           (
               SELECT COUNT(*) FROM spotify_accounts_items AS item
                WHERE item.telegram_id=pos.telegram_id
                  AND item.position_id=pos.id
                  AND item.status='available'
           ) AS available,
           (
               SELECT COUNT(*) FROM spotify_accounts_items AS item
                WHERE item.telegram_id=pos.telegram_id
                  AND item.position_id=pos.id
                  AND item.status='reserved'
           ) AS reserved,
           (
               SELECT COUNT(*) FROM spotify_accounts_items AS item
                WHERE item.telegram_id=pos.telegram_id
                  AND item.position_id=pos.id
                  AND item.status='sold'
           ) AS sold,
           (
               SELECT COUNT(*) FROM spotify_accounts_bindings AS binding
                WHERE binding.telegram_id=pos.telegram_id
                  AND binding.position_id=pos.id
           ) AS lots
      FROM spotify_accounts_positions AS pos
"""

async def _positions() -> list[Any]:
    await _ensure_schema()
    return list(
        await _db().fetch(
            _POSITION_SELECT
            + " WHERE pos.telegram_id=$1 ORDER BY pos.created_at, pos.id",
            _telegram_id(),
        )
    )

async def _position(position_id: int) -> Any | None:
    await _ensure_schema()
    return await _db().fetchrow(
        _POSITION_SELECT
        + " WHERE pos.telegram_id=$1 AND pos.id=$2",
        _telegram_id(),
        position_id,
    )

async def _create_position(title: str, days: int) -> Any:
    return await _db().fetchrow(
        """
        INSERT INTO spotify_accounts_positions
            (telegram_id, title, subscription_days)
        VALUES ($1, $2, $3)
        RETURNING *
        """,
        _telegram_id(),
        title,
        days,
    )

async def _update_position(position_id: int, column: str, value: Any) -> None:
    allowed = {"title", "subscription_days", "enabled", "low_stock_notified"}
    if column not in allowed:
        raise ValueError("неизвестное поле позиции")
    await _db().execute(
        f"""
        UPDATE spotify_accounts_positions
           SET {column}=$3, updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        position_id,
        value,
    )

async def _delete_position(position_id: int) -> None:
    busy = await _db().fetchrow(
        """
        SELECT 1 FROM spotify_accounts_items
         WHERE telegram_id=$1 AND position_id=$2 AND status='reserved'
         LIMIT 1
        """,
        _telegram_id(),
        position_id,
    )
    if busy:
        raise RuntimeError("по позиции идёт выдача; дождитесь её завершения")
    await _db().execute(
        """
        DELETE FROM spotify_accounts_positions
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        position_id,
    )

async def _position_bindings(position_id: int) -> list[Any]:
    await _ensure_schema()
    return list(
        await _db().fetch(
            """
            SELECT * FROM spotify_accounts_bindings
             WHERE telegram_id=$1 AND position_id=$2
             ORDER BY created_at, id
            """,
            _telegram_id(),
            position_id,
        )
    )

async def _all_bindings() -> list[Any]:
    await _ensure_schema()
    return list(
        await _db().fetch(
            "SELECT * FROM spotify_accounts_bindings WHERE telegram_id=$1",
            _telegram_id(),
        )
    )

async def _bind_lot(position_id: int, lot_id: str, lot_title: str) -> None:
    await _db().execute(
        """
        INSERT INTO spotify_accounts_bindings
            (telegram_id, position_id, lot_id, lot_title)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (telegram_id, lot_id) DO UPDATE
            SET position_id=EXCLUDED.position_id,
                lot_title=EXCLUDED.lot_title,
                updated_at=NOW()
        """,
        _telegram_id(),
        position_id,
        str(lot_id),
        lot_title,
    )

async def _unbind(binding_id: int) -> None:
    await _db().execute(
        """
        DELETE FROM spotify_accounts_bindings
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        binding_id,
    )

async def _available_emails(position_id: int) -> set[str]:
    rows = await _db().fetch(
        """
        SELECT lower(email) AS email FROM spotify_accounts_items
         WHERE telegram_id=$1 AND position_id=$2 AND status='available'
        """,
        _telegram_id(),
        position_id,
    )
    return {str(row["email"]) for row in rows}

async def _add_items(
    position_id: int, entries: list[tuple[str, str, str | None]]
) -> tuple[int, int]:
    existing = await _available_emails(position_id)
    added = 0
    skipped = 0
    for email, password, key in entries:
        normalized = email.casefold()
        if normalized in existing:
            skipped += 1
            continue
        await _db().execute(
            """
            INSERT INTO spotify_accounts_items
                (telegram_id, position_id, email, password, activation_key)
            VALUES ($1, $2, $3, $4, $5)
            """,
            _telegram_id(),
            position_id,
            email,
            _encrypt_secret(password),
            _encrypt_secret(key),
        )
        existing.add(normalized)
        added += 1
    if added:
        await _update_position(position_id, "low_stock_notified", False)
    return added, skipped

async def _clear_available(position_id: int) -> int:
    row = await _db().fetchrow(
        """
        SELECT COUNT(*) AS amount FROM spotify_accounts_items
         WHERE telegram_id=$1 AND position_id=$2 AND status='available'
        """,
        _telegram_id(),
        position_id,
    )
    amount = int(_row_get(row, "amount", 0) or 0)
    if amount:
        await _db().execute(
            """
            DELETE FROM spotify_accounts_items
             WHERE telegram_id=$1 AND position_id=$2 AND status='available'
            """,
            _telegram_id(),
            position_id,
        )
    return amount

async def _stock_page(
    position_id: int, offset: int, limit: int = STOCK_PAGE_SIZE
) -> tuple[list[Any], int]:
    row = await _db().fetchrow(
        """
        SELECT COUNT(*) AS amount FROM spotify_accounts_items
         WHERE telegram_id=$1 AND position_id=$2 AND status='available'
        """,
        _telegram_id(),
        position_id,
    )
    total = int(_row_get(row, "amount", 0) or 0)
    items = list(
        await _db().fetch(
            """
            SELECT id, email, activation_key, created_at
              FROM spotify_accounts_items
             WHERE telegram_id=$1 AND position_id=$2 AND status='available'
             ORDER BY id
             OFFSET $3 LIMIT $4
            """,
            _telegram_id(),
            position_id,
            max(0, int(offset)),
            limit,
        )
    )
    return items, total

async def _delete_item(item_id: int) -> int | None:
    row = await _db().fetchrow(
        """
        SELECT position_id FROM spotify_accounts_items
         WHERE telegram_id=$1 AND id=$2 AND status='available'
        """,
        _telegram_id(),
        item_id,
    )
    if not row:
        return None
    await _db().execute(
        """
        DELETE FROM spotify_accounts_items
         WHERE telegram_id=$1 AND id=$2 AND status='available'
        """,
        _telegram_id(),
        item_id,
    )
    return int(_row_get(row, "position_id") or 0) or None

async def _stock_preview(position_id: int, limit: int = 5) -> list[Any]:
    return list(
        await _db().fetch(
            """
            SELECT id, email, activation_key, created_at
              FROM spotify_accounts_items
             WHERE telegram_id=$1 AND position_id=$2 AND status='available'
             ORDER BY id DESC LIMIT $3
            """,
            _telegram_id(),
            position_id,
            limit,
        )
    )

# --------------------------------------------------------------------------- #
# Заказы и выдача
# --------------------------------------------------------------------------- #

async def _insert_order(
    order: dict[str, Any], position: Any | None, status: str = "queued"
) -> Any | None:
    position_id = int(position["id"]) if position else None
    title = (
        str(position["title"])
        if position
        else str(order.get("description") or "")[:300]
    )
    days = int(position["subscription_days"]) if position else 0
    return await _db().fetchrow(
        f"""
        INSERT INTO spotify_accounts_orders
            (telegram_id, order_id, position_id, lot_id, position_title,
             subscription_days, amount, buyer_username, chat_id, chat_name,
             status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (telegram_id, order_id) DO NOTHING
        RETURNING *
        """,
        _telegram_id(),
        str(order["id"]),
        position_id,
        str(order.get("lot_id") or ""),
        title,
        days,
        max(1, int(order.get("amount") or 1)),
        str(order.get("chat_name") or ""),
        str(order.get("chat_id") or ""),
        str(order.get("chat_name") or ""),
        status,
    )

async def _order_by_funpay_id(order_id: str) -> Any | None:
    await _ensure_schema()
    return await _db().fetchrow(
        """
        SELECT * FROM spotify_accounts_orders
         WHERE telegram_id=$1 AND order_id=$2
        """,
        _telegram_id(),
        str(order_id),
    )

async def _update_order(order_db_id: int, **values: Any) -> None:
    allowed = {
        "status",
        "item_ids",
        "error_text",
        "delivered_at",
        "followup_at",
        "followup_status",
        "followup_attempts",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(
        f"{key}=${index + 3}" for index, key in enumerate(values)
    )
    await _db().execute(
        f"""
        UPDATE spotify_accounts_orders
           SET {assignments}, updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        order_db_id,
        *values.values(),
    )

async def _recent_orders(limit: int = 6) -> list[Any]:
    await _ensure_schema()
    return list(
        await _db().fetch(
            """
            SELECT * FROM spotify_accounts_orders
             WHERE telegram_id=$1
             ORDER BY created_at DESC LIMIT $2
            """,
            _telegram_id(),
            limit,
        )
    )

async def _claim_items(
    position_id: int, order_id: str, amount: int
) -> list[Any]:
    rows = await _db().fetch(
        """
        WITH candidate AS (
            SELECT id FROM spotify_accounts_items
             WHERE telegram_id=$1 AND position_id=$2 AND status='available'
             ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT $4
        )
        UPDATE spotify_accounts_items AS item
           SET status='reserved', reserved_order_id=$3, updated_at=NOW()
          FROM candidate
         WHERE item.id=candidate.id
        RETURNING item.*
        """,
        _telegram_id(),
        position_id,
        str(order_id),
        max(1, int(amount)),
    )
    return list(rows)

async def _release_item(item_id: int) -> None:
    await _db().execute(
        """
        UPDATE spotify_accounts_items
           SET status='available', reserved_order_id=NULL, updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2 AND status='reserved'
        """,
        _telegram_id(),
        item_id,
    )

async def _mark_item_sold(item_id: int, order_id: str) -> None:
    await _db().execute(
        """
        UPDATE spotify_accounts_items
           SET status='sold', order_id=$3, reserved_order_id=NULL,
               sold_at=NOW(), updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        item_id,
        str(order_id),
    )

async def _funpay_send(order: Any, text: str) -> None:
    await asyncio.to_thread(
        _cardinal.account.send_message,
        _row_get(order, "chat_id"),
        text,
        _row_get(order, "chat_name") or None,
    )

async def _notify_owner(text: str, reply_markup: Any | None = None) -> None:
    try:
        await asyncio.to_thread(
            _cardinal.telegram.send_notification,
            text,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Не отправлено уведомление владельцу")

async def _fulfill_order(order_db_id: int) -> str:
    row = await _db().fetchrow(
        """
        SELECT * FROM spotify_accounts_orders
         WHERE telegram_id=$1 AND id=$2 AND status='queued'
        """,
        _telegram_id(),
        order_db_id,
    )
    if not row:
        return "handled"
    amount = max(1, int(_row_get(row, "amount", 1) or 1))
    items = await _claim_items(
        int(row["position_id"]), str(row["order_id"]), amount
    )
    if len(items) < amount:
        for item in items:
            await _release_item(int(item["id"]))
        return "no_stock"
    claimed = await _db().fetchrow(
        """
        UPDATE spotify_accounts_orders
           SET status='delivering', item_ids=$3, updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2 AND status='queued'
        RETURNING *
        """,
        _telegram_id(),
        order_db_id,
        [int(item["id"]) for item in items],
    )
    if not claimed:
        for item in items:
            await _release_item(int(item["id"]))
        return "handled"
    try:
        settings = await _settings()
        template = _delivery_template(settings)
        days = int(claimed["subscription_days"] or 0)
        blocks = [
            _render_delivery(template, item=item, days=days, order=claimed)
            for item in items
        ]
        text = "\n\n➖➖➖\n\n".join(blocks) if len(blocks) > 1 else blocks[0]
        await _funpay_send(claimed, text)
    except Exception as exc:
        logger.exception("Не выдана покупка по заказу %s", claimed["order_id"])
        for item in items:
            await _release_item(int(item["id"]))
        await _update_order(
            order_db_id, status="failed", error_text=str(exc)[:800]
        )
        await _notify_owner(
            "❌ <b>Не удалось выдать аккаунт</b>\n\n"
            f"Заказ: <code>#{html.escape(str(claimed['order_id']))}</code>\n"
            f"Позиция: <b>{html.escape(str(claimed['position_title'] or '—'))}</b>\n"
            f"Причина: <code>{html.escape(str(exc)[:500])}</code>\n\n"
            "Аккаунт возвращён на склад. Проверьте настройки и попробуйте "
            "отправить его вручную.",
            reply_markup=_markup(
                [("📦 Позиция", f"{CALLBACK_PREFIX}pos:{claimed['position_id']}")]
            ),
        )
        return "error"
    for item in items:
        await _mark_item_sold(int(item["id"]), str(claimed["order_id"]))
    settings = await _settings()
    delay = int(settings["followup_delay_minutes"] or 0)
    await _update_order(
        order_db_id,
        status="delivered",
        error_text=None,
        delivered_at=datetime.now(timezone.utc),
        followup_at=datetime.now(timezone.utc) + timedelta(minutes=delay),
        followup_status="pending",
    )
    await _notify_owner(
        "✅ <b>Аккаунт выдан со склада</b>\n\n"
        f"Заказ: <code>#{html.escape(str(claimed['order_id']))}</code>\n"
        f"Покупатель: <b>{html.escape(str(claimed['buyer_username'] or '—'))}</b>\n"
        f"Позиция: <b>{html.escape(str(claimed['position_title'] or '—'))}</b>\n"
        f"Аккаунтов: <b>{len(items)}</b>\n"
        f"Почта: <code>{html.escape(', '.join(str(item['email']) for item in items))}</code>\n"
        f"Подписка: <b>{int(claimed['subscription_days'] or 0)} дн.</b>\n"
        f"Напоминание покупателю: через <b>{delay} мин</b>",
        reply_markup=_markup(
            [("📦 Позиция", f"{CALLBACK_PREFIX}pos:{claimed['position_id']}")]
        ),
    )
    await _maybe_alert_low_stock(int(claimed["position_id"]))
    return "delivered"

async def _fulfill_queued(limit: int = 25) -> None:
    rows = await _db().fetch(
        """
        SELECT id FROM spotify_accounts_orders
         WHERE telegram_id=$1 AND status='queued'
         ORDER BY created_at LIMIT $2
        """,
        _telegram_id(),
        limit,
    )
    for row in rows:
        await _fulfill_order(int(row["id"]))

async def _maybe_alert_low_stock(position_id: int) -> None:
    position = await _position(position_id)
    if not position or position["low_stock_notified"]:
        return
    settings = await _settings()
    threshold = max(int(settings["low_stock_threshold"] or 0), 0)
    available = int(position["available"] or 0)
    if available > threshold:
        return
    await _update_position(position_id, "low_stock_notified", True)
    if available <= 0:
        headline = "🛑 <b>Склад пуст</b>"
        tail = "Заказы этого лота будут ждать пополнения."
    else:
        headline = "⚠️ <b>Склад заканчивается</b>"
        tail = "Пополните склад, чтобы выдача не остановилась."
    await _notify_owner(
        f"{headline}\n\n"
        f"Позиция: <b>{html.escape(str(position['title']))}</b>\n"
        f"Осталось: <b>{available}</b> аккаунтов\n\n{tail}",
        reply_markup=_markup(
            [
                ("➕ Пополнить", f"{CALLBACK_PREFIX}fill:{position_id}"),
                ("📦 Позиция", f"{CALLBACK_PREFIX}pos:{position_id}"),
            ]
        ),
    )

async def _return_item_to_stock(order_id: str) -> None:
    order = await _order_by_funpay_id(order_id)
    if not order:
        raise RuntimeError("заказ не найден")
    item_ids = [int(value) for value in (_row_get(order, "item_ids") or [])]
    if not item_ids:
        raise RuntimeError("в заказе нет выданного аккаунта")
    await _db().execute(
        """
        UPDATE spotify_accounts_items
           SET status='available', order_id=NULL, reserved_order_id=NULL,
               sold_at=NULL, updated_at=NOW()
         WHERE telegram_id=$1 AND id = ANY($2::bigint[]) AND status='sold'
        """,
        _telegram_id(),
        item_ids,
    )
    await _update_order(
        int(order["id"]), status="returned", followup_status="canceled"
    )
    if order["position_id"]:
        await _update_position(
            int(order["position_id"]), "low_stock_notified", False
        )
    await _notify_owner(
        f"↩️ Аккаунт по заказу <code>#{html.escape(str(order_id))}</code> "
        "снова доступен на складе."
    )
    await _fulfill_queued()

# --------------------------------------------------------------------------- #
# Разбор склада, шаблоны и текст
# --------------------------------------------------------------------------- #

def _parse_stock_line(raw: str) -> tuple[str, str, str | None] | None:
    line = raw.strip().lstrip("\ufeff").strip()
    if not line:
        return None
    if ":" not in line:
        raise ValueError("нет разделителя «:» между почтой и паролем")
    parts = [part.strip() for part in line.split(":")]
    if len(parts) == 2:
        email, password = parts
        key = None
    else:
        email, password = parts[0], ":".join(parts[1:-1])
        key = parts[-1] or None
    if not EMAIL_RE.match(email):
        raise ValueError("первое поле не похоже на почту")
    if not password:
        raise ValueError("пустой пароль")
    if len(password) > 200:
        raise ValueError("слишком длинный пароль")
    if key is not None and len(key) > 200:
        raise ValueError("слишком длинный ключ активации")
    return email, password, key

def _parse_stock_lines(
    text: str,
) -> tuple[list[tuple[str, str, str | None]], list[str], int]:
    entries: list[tuple[str, str, str | None]] = []
    errors: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for index, raw in enumerate(str(text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        if len(entries) + len(errors) >= MAX_STOCK_LINES:
            errors.append("• слишком много строк за один раз (максимум 500)")
            break
        try:
            parsed = _parse_stock_line(raw)
        except ValueError as exc:
            errors.append(f"• строка {index}: {html.escape(str(exc))}")
            continue
        if not parsed:
            continue
        normalized = parsed[0].casefold()
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        entries.append(parsed)
    return entries, errors, duplicates

def _validate_title(value: str) -> str:
    title = " ".join(str(value or "").split())
    if not 2 <= len(title) <= 60:
        raise ValueError("название должно быть от 2 до 60 символов")
    return title

def _positive_int(value: str, *, minimum: int, maximum: int, label: str) -> int:
    text = str(value or "").strip()
    if not text.isdigit():
        raise ValueError(f"{label}: нужно целое число")
    number = int(text)
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}: допустимо от {minimum} до {maximum}")
    return number

def _validate_review_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > 500:
        raise ValueError("ссылка слишком длинная")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("нужна ссылка http(s)://… без пробелов")
    return url

def _validate_delivery_template(value: str) -> str:
    template = str(value or "").strip()
    if not 20 <= len(template) <= MAX_TEMPLATE_LENGTH:
        raise ValueError(
            f"шаблон должен быть от 20 до {MAX_TEMPLATE_LENGTH} символов"
        )
    for name in ("email", "password"):
        if "{" + name + "}" not in template:
            raise ValueError(f"в шаблоне нет обязательного поля {{{name}}}")
    return template

def _validate_followup_template(value: str) -> str:
    template = str(value or "").strip()
    if not 20 <= len(template) <= MAX_TEMPLATE_LENGTH:
        raise ValueError(
            f"шаблон должен быть от 20 до {MAX_TEMPLATE_LENGTH} символов"
        )
    return template

def _delivery_template(settings: Any) -> str:
    stored = str(_row_get(settings, "delivery_template") or "").strip()
    return stored or DEFAULT_DELIVERY_TEMPLATE

def _followup_template(settings: Any) -> str:
    stored = str(_row_get(settings, "followup_template") or "").strip()
    return stored or DEFAULT_FOLLOWUP_TEMPLATE

def _render(
    template: str, values: dict[str, str], *, optional: tuple[str, ...] = ()
) -> str:
    text = str(template or "")
    lines: list[str] = []
    for line in text.splitlines():
        placeholder = next(
            (
                name
                for name in optional
                if "{" + name + "}" in line and not values.get(name)
            ),
            None,
        )
        if placeholder is not None:
            continue
        lines.append(line)
    text = "\n".join(lines)
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    return text.strip()

def _subscription_until(days: int) -> str:
    if days <= 0:
        return "без ограничения"
    date = datetime.now().astimezone() + timedelta(days=days)
    return date.strftime("%d.%m.%Y")

def _render_delivery(template: str, *, item: Any, days: int, order: Any) -> str:
    password = _decrypt_secret(str(_row_get(item, "password") or ""))
    if password is None:
        raise RuntimeError("не удалось прочитать пароль аккаунта")
    key = _decrypt_secret(str(_row_get(item, "activation_key") or "") or "") or ""
    return _render(
        template,
        {
            "email": str(_row_get(item, "email") or ""),
            "password": password,
            "key": key,
            "days": str(days),
            "until": _subscription_until(days),
            "order_id": str(_row_get(order, "order_id") or ""),
            "buyer": str(_row_get(order, "buyer_username") or ""),
        },
        optional=("key",),
    )

def _render_followup(
    template: str, *, order: Any, email: str, closed: bool, settings: Any
) -> str:
    days = int(_row_get(order, "subscription_days", 0) or 0)
    return _render(
        template,
        {
            "buyer": str(_row_get(order, "buyer_username") or ""),
            "order_id": str(_row_get(order, "order_id") or ""),
            "email": email,
            "days": str(days),
            "until": _subscription_until(days),
            "review_url": str(_row_get(settings, "review_url") or ""),
            "confirm_hint": CONFIRM_CLOSED_HINT if closed else CONFIRM_PAID_HINT,
        },
    )

def _delivery_preview(template: str, days: int, *, with_key: bool) -> str:
    key = "SPOT-2026-0001" if with_key else ""
    return _render(
        template,
        {
            "email": "buyer@example.com",
            "password": "Passw0rd!",
            "key": key,
            "days": str(days),
            "until": _subscription_until(days),
            "order_id": "ABC12345",
            "buyer": "buyer",
        },
        optional=("key",),
    )

# --------------------------------------------------------------------------- #
# Сопоставление заказа с позицией
# --------------------------------------------------------------------------- #

def _normalize_title(value: str) -> str:
    text = AMOUNT_RE.sub("", str(value or ""))
    return " ".join(text.split()).casefold()

OFFER_RE = re.compile(
    r"(?:lots/offer\?id=|/offer\?id=|offer=|data-offer=[\"']|"
    r"data-lot=[\"']|offerId[\"']?\s*[:=]\s*[\"']?)(\d{3,})",
    re.I,
)

def _order_lot_id(order: Any) -> str | None:
    """ID лота из события заказа.

    У ``OrderShortcut`` атрибута ``lot_id`` нет, поэтому дополнительно
    разбираются ссылка и HTML-виджет заказа.
    """
    for attribute in ("lot_id", "offer_id", "lotId"):
        direct = getattr(order, attribute, None)
        if direct not in (None, "", 0):
            return str(direct)
    for source in (getattr(order, "html", ""), getattr(order, "link", "")):
        match = OFFER_RE.search(str(source or ""))
        if match:
            return match.group(1)
    return None

async def _match_position(lot_id: str | None, description: str) -> Any | None:
    positions = {int(position["id"]): position for position in await _positions()}
    enabled = {
        key: value for key, value in positions.items() if value["enabled"]
    }
    if not enabled:
        return None
    bindings = await _all_bindings()
    if lot_id is not None:
        for binding in bindings:
            if str(binding["lot_id"]) == str(lot_id):
                position = enabled.get(int(binding["position_id"]))
                if position:
                    return position
    normalized = _normalize_title(description)
    if not normalized:
        return None
    titles = [
        (int(binding["position_id"]), _normalize_title(str(binding["lot_title"])))
        for binding in bindings
        if _normalize_title(str(binding["lot_title"]))
    ]

    def collect(predicate: Any) -> dict[int, Any]:
        found: dict[int, Any] = {}
        for position_id, title in titles:
            if not predicate(title):
                continue
            position = enabled.get(position_id)
            if position:
                found[int(position["id"])] = position
        return found

    exact = collect(lambda title: title == normalized)
    if len(exact) == 1:
        return next(iter(exact.values()))
    if exact:
        return None
    # Название в заказе и в лоте FunPay иногда отличается приставкой или
    # хвостом (локализация, количество, пометки) — сравниваем вхождением.
    fuzzy = collect(
        lambda title: len(title) >= 8
        and (title in normalized or normalized in title)
    )
    if len(fuzzy) == 1:
        return next(iter(fuzzy.values()))
    return None

def _builtin_delivery_conflict(
    description: str, lot_id: str | None = None
) -> bool:
    """Проверяет, сработает ли встроенная автовыдача по тому же лоту."""
    try:
        db = _db()
        get_user = getattr(db, "get_user", None)
        find_rule = getattr(db, "find_delivery_rule", None)
        if get_user is None or find_rule is None:
            return False
        row = _sync(get_user(_telegram_id()))
        if not row or not _row_get(row, "auto_delivery_enabled", False):
            return False
        rule = _sync(find_rule(_telegram_id(), str(description or "")))
        if not rule or not _row_get(rule, "enabled", False):
            return False
        rule_lot = _row_get(rule, "lot_id")
        if rule_lot not in (None, "") and lot_id not in (None, ""):
            # Встроенное правило FunPay ищет лот по подстроке и может зацепить
            # чужой лот. Сверяем точный ID, чтобы не блокировать выдачу.
            return str(rule_lot) == str(lot_id)
        rule_title = _normalize_title(
            str(_row_get(rule, "lot_title", "") or "")
        )
        order_title = _normalize_title(str(description or ""))
        if rule_title and order_title:
            return (
                rule_title == order_title
                or rule_title in order_title
                or order_title in rule_title
            )
        return True
    except Exception:
        logger.warning(
            "Не удалось проверить встроенную автовыдачу FunPay", exc_info=True
        )
        return False

# --------------------------------------------------------------------------- #
# Обработка заказов
# --------------------------------------------------------------------------- #

async def _record_unmatched(order: dict[str, Any]) -> None:
    inserted = await _insert_order(order, None, status="unmatched")
    if not inserted:
        return
    logger.warning(
        "Заказ %s не сопоставлен ни с одной позицией: lot_id=%s description=%r",
        order.get("id"),
        order.get("lot_id"),
        str(order.get("description"))[:200],
    )
    await _notify_owner(
        "🟡 <b>Оплаченный заказ не сопоставлен с позицией</b>\n\n"
        f"Заказ: <code>#{html.escape(str(order.get('id')))}</code>\n"
        f"Лот FunPay: <b>{html.escape(str(order.get('description'))[:300])}</b>\n"
        f"Покупатель: <b>{html.escape(str(order.get('chat_name') or '—'))}</b>\n\n"
        "Аккаунт не выдан. Привяжите этот лот к позиции и нажмите «Выдать» "
        "в разделе «📋 Заказы».",
        reply_markup=_markup(
            [("📋 Заказы", f"{CALLBACK_PREFIX}orders")],
            [("📦 Позиции", f"{CALLBACK_PREFIX}pos")],
        ),
    )

async def _process_new_order(order: dict[str, Any]) -> None:
    try:
        await _process_order_safe(order)
    except Exception as exc:
        logger.exception(
            "Не обработан заказ %s Spotify Account Shop", order.get("id")
        )
        await _notify_owner(
            "❌ <b>Ошибка выдачи заказа</b>\n\n"
            f"Заказ: <code>#{html.escape(str(order.get('id')))}</code>\n"
            f"Лот: <b>{html.escape(str(order.get('description'))[:300])}</b>\n"
            f"Причина: <code>{html.escape(str(exc)[:400])}</code>\n\n"
            "Проверьте раздел «📋 Заказы» и попробуйте выдать вручную.",
            reply_markup=_markup([("📋 Заказы", f"{CALLBACK_PREFIX}orders")]),
        )

async def _process_order_safe(order: dict[str, Any]) -> None:
    position = await _match_position(order.get("lot_id"), order["description"])
    if position is None:
        await _record_unmatched(order)
        return
    if order.get("builtin_conflict"):
        await _notify_owner(
            "⚠️ <b>Заказ не выдан со склада: конфликт с автовыдачей</b>\n\n"
            f"Заказ: <code>#{html.escape(str(order['id']))}</code>\n"
            f"Лот: <b>{html.escape(str(order['description'])[:700])}</b>\n"
            f"Позиция: <b>{html.escape(str(position['title']))}</b>\n\n"
            "У этого лота включена встроенная автовыдача FunPay. Отключите её "
            "для лота, иначе покупатель получит два сообщения."
        )
        return
    inserted = await _insert_order(order, position)
    if not inserted:
        return
    result = await _fulfill_order(int(inserted["id"]))
    if result != "no_stock":
        return
    try:
        await _funpay_send(inserted, NO_STOCK_MESSAGE)
    except Exception:
        logger.exception("Не отправлено сообщение об отсутствии товара")
    await _notify_owner(
        "🛑 <b>На складе нет аккаунтов для заказа</b>\n\n"
        f"Заказ: <code>#{html.escape(str(order['id']))}</code>\n"
        f"Позиция: <b>{html.escape(str(position['title']))}</b>\n\n"
        "Заказ сохранён в очереди и будет выдан автоматически после пополнения.",
        reply_markup=_markup(
            [("➕ Пополнить", f"{CALLBACK_PREFIX}fill:{position['id']}")]
        ),
    )

async def _process_order_status(order_id: str, status: str) -> None:
    if status not in {"REFUNDED", "PARTIALLY_REFUNDED", "UNPAID"}:
        return
    order = await _order_by_funpay_id(order_id)
    if not order or order["status"] in {"refunded", "returned"}:
        return
    await _update_order(
        int(order["id"]), status="refunded", followup_status="canceled"
    )
    buttons = []
    if order["position_id"]:
        buttons.append(
            ("📦 Позиция", f"{CALLBACK_PREFIX}pos:{order['position_id']}")
        )
    buttons.append(
        ("↩️ Вернуть в склад", f"{CALLBACK_PREFIX}back:{order_id}")
    )
    await _notify_owner(
        "⚠️ <b>Возврат средств по заказу</b>\n\n"
        f"Заказ: <code>#{html.escape(str(order_id))}</code>\n"
        f"Позиция: <b>{html.escape(str(order['position_title'] or '—'))}</b>\n"
        f"Статус FunPay: <b>{html.escape(str(status))}</b>\n\n"
        "Если аккаунт не использовался, верните его на склад.",
        reply_markup=_markup(*[buttons]),
    )

async def _retry_order(order_db_id: int) -> str:
    """Повторная выдача заказа (кнопка «Выдать» в разделе «Заказы»)."""
    row = await _db().fetchrow(
        """
        SELECT * FROM spotify_accounts_orders
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        order_db_id,
    )
    if not row:
        raise RuntimeError("заказ не найден")
    if _row_get(row, "status") in {"delivered", "refunded", "returned"}:
        return str(_row_get(row, "status"))
    position = None
    if _row_get(row, "position_id"):
        position = await _position(int(row["position_id"]))
    if position is None:
        position = await _match_position(
            _row_get(row, "lot_id"), str(_row_get(row, "position_title") or "")
        )
    if position is None:
        raise RuntimeError(
            "позиция для этого лота не найдена — привяжите лот к позиции"
        )
    await _db().execute(
        """
        UPDATE spotify_accounts_orders
           SET status='queued', position_id=$3, position_title=$4,
               subscription_days=$5, error_text=NULL, updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        order_db_id,
        int(position["id"]),
        str(position["title"]),
        int(position["subscription_days"]),
    )
    return await _fulfill_order(order_db_id)

async def _poll_paid_orders() -> None:
    """Страховка: выдаёт оплаченные заказы даже без события NEW_ORDER.

    Старые сборки бота могут не доставлять хук о новом заказе в загруженный
    плагин. Тогда продавец узнаёт о продаже только из FunPay. Здесь мы сами
    смотрим список оплаченных заказов и выдаём всё, чего ещё нет в базе.
    """
    account = getattr(_cardinal, "account", None)
    get_sales = getattr(account, "get_sales", None)
    if get_sales is None:
        return
    try:
        data = await asyncio.to_thread(
            get_sales,
            include_paid=True,
            include_closed=False,
            include_refunded=False,
        )
    except Exception:
        logger.warning("Не получены оплаченные заказы FunPay", exc_info=True)
        return
    orders = []
    if isinstance(data, tuple):
        for chunk in data:
            if isinstance(chunk, list):
                orders = chunk
                break
    elif isinstance(data, list):
        orders = data
    processed = 0
    for order in orders:
        if processed >= 20:
            break
        status = getattr(getattr(order, "status", None), "name", "")
        if status and status != "PAID":
            continue
        order_id = str(getattr(order, "id", "") or "")
        if not order_id:
            continue
        existing = await _order_by_funpay_id(order_id)
        if existing:
            continue
        description = str(getattr(order, "description", "") or "")
        processed += 1
        await _process_new_order(
            {
                "id": order_id,
                "chat_id": getattr(order, "chat_id", None),
                "chat_name": str(getattr(order, "buyer_username", "") or ""),
                "description": description,
                "amount": max(1, int(getattr(order, "amount", None) or 1)),
                "lot_id": _order_lot_id(order),
                "builtin_conflict": _builtin_delivery_conflict(
                    description, _order_lot_id(order)
                ),
            }
        )

async def _resume_state() -> None:
    await _ensure_schema()
    stale = list(
        await _db().fetch(
            """
            SELECT * FROM spotify_accounts_orders
             WHERE telegram_id=$1 AND status IN ('processing', 'delivering')
            """,
            _telegram_id(),
        )
    )
    await _db().execute(
        """
        UPDATE spotify_accounts_orders
           SET status='failed', followup_status='canceled',
               error_text='плагин остановлен во время выдачи', updated_at=NOW()
         WHERE telegram_id=$1 AND status IN ('processing', 'delivering')
        """,
        _telegram_id(),
    )
    if stale:
        lines = [
            "⚠️ <b>Незавершённые выдачи после перезапуска</b>",
            "",
            "Проверьте заказы вручную: сообщение могло уйти покупателю.",
            "",
        ]
        for row in stale[:10]:
            lines.append(
                f"• <code>#{html.escape(str(row['order_id']))}</code> — "
                f"{html.escape(str(row['position_title'] or '—'))}"
            )
        await _notify_owner("\n".join(lines))
    await _db().execute(
        """
        UPDATE spotify_accounts_items AS item
           SET status='available', reserved_order_id=NULL, updated_at=NOW()
         WHERE item.telegram_id=$1 AND item.status='reserved'
           AND NOT EXISTS (
               SELECT 1 FROM spotify_accounts_orders AS ord
                WHERE ord.telegram_id=$1 AND item.id = ANY(ord.item_ids)
                  AND ord.status='delivering'
           )
        """,
        _telegram_id(),
    )
    await _fulfill_queued()

async def _run_followups() -> None:
    rows = list(
        await _db().fetch(
            """
            SELECT ord.*,
                   (
                       SELECT string_agg(item.email, ', ')
                         FROM spotify_accounts_items AS item
                        WHERE item.telegram_id=ord.telegram_id
                          AND item.id = ANY(ord.item_ids)
                   ) AS email
              FROM spotify_accounts_orders AS ord
             WHERE ord.telegram_id=$1
               AND ord.followup_status='pending'
               AND ord.followup_at IS NOT NULL
               AND ord.followup_at <= NOW()
             ORDER BY ord.followup_at
             LIMIT 20
            """,
            _telegram_id(),
        )
    )
    if not rows:
        return
    settings = await _settings()
    if not settings["followup_enabled"]:
        for row in rows:
            await _update_order(int(row["id"]), followup_status="disabled")
        return
    for row in rows:
        if row["status"] != "delivered":
            await _update_order(int(row["id"]), followup_status="canceled")
            continue
        status = ""
        review = None
        try:
            full = await asyncio.to_thread(
                _cardinal.account.get_order, str(row["order_id"])
            )
            status = getattr(getattr(full, "status", None), "name", "")
            review = getattr(full, "review", None)
        except Exception:
            logger.warning(
                "Не проверен статус заказа %s перед напоминанием",
                row["order_id"],
                exc_info=True,
            )
        if status in {"REFUNDED", "PARTIALLY_REFUNDED", "UNPAID"}:
            await _update_order(int(row["id"]), followup_status="canceled")
            continue
        if review:
            await _update_order(int(row["id"]), followup_status="skipped")
            continue
        text = _render_followup(
            _followup_template(settings),
            order=row,
            email=str(row["email"] or ""),
            closed=status == "CLOSED",
            settings=settings,
        )
        try:
            await _funpay_send(row, text)
        except Exception:
            attempts = int(row["followup_attempts"] or 0) + 1
            if attempts >= FOLLOWUP_MAX_ATTEMPTS:
                logger.exception(
                    "Напоминание по заказу %s не отправлено", row["order_id"]
                )
                await _update_order(
                    int(row["id"]),
                    followup_status="failed",
                    followup_attempts=attempts,
                )
                await _notify_owner(
                    "⚠️ Не удалось отправить напоминание покупателю по заказу "
                    f"<code>#{html.escape(str(row['order_id']))}</code>."
                )
            else:
                await _update_order(
                    int(row["id"]),
                    followup_attempts=attempts,
                    followup_at=datetime.now(timezone.utc)
                    + timedelta(minutes=FOLLOWUP_RETRY_MINUTES),
                )
            continue
        await _update_order(
            int(row["id"]), followup_status="sent", followup_attempts=0
        )

_last_poll: datetime | None = None

async def _worker_loop() -> None:
    global _last_poll
    while True:
        try:
            if (
                _cardinal is not None
                and _cardinal.plugin_manager.is_enabled(_telegram_id(), UUID)
            ):
                await _run_followups()
                now = datetime.now(timezone.utc)
                if (
                    _last_poll is None
                    or (now - _last_poll).total_seconds() >= POLL_SECONDS
                ):
                    _last_poll = now
                    await _poll_paid_orders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой задачи Spotify Account Shop")
        await asyncio.sleep(WORKER_SECONDS)

# --------------------------------------------------------------------------- #
# Telegram-интерфейс
# --------------------------------------------------------------------------- #

def _status_label(status: str) -> str:
    return {
        "queued": "ожидает склад",
        "delivering": "выдаётся",
        "delivered": "выдан",
        "failed": "ошибка",
        "refunded": "возврат",
        "returned": "возвращён на склад",
        "unmatched": "не сопоставлен",
    }.get(status, status)

def _followup_label(status: str) -> str:
    return {
        "pending": "запланировано",
        "sent": "отправлено",
        "skipped": "не нужно",
        "disabled": "выключено",
        "canceled": "отменено",
        "failed": "ошибка",
        "none": "—",
    }.get(status, status)

def _short(value: Any, limit: int = 40) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"

def _mask_email(email: str) -> str:
    if "@" not in email:
        return email[:2] + "•••"
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}•••@{domain}"

def _is_own_callback(data: str) -> bool:
    return (
        data.startswith(CALLBACK_PREFIX)
        or data.startswith(f"{PLUGIN_SETTINGS_PREFIX}:{UUID}:")
    )

def _reset_input_state() -> None:
    global _pending_input, _stock_mode
    _pending_input = None
    _stock_mode = None

def _show_home(chat_id: int) -> None:
    _reset_input_state()
    settings = _sync(_settings())
    positions = _sync(_positions())
    orders = _sync(_recent_orders())
    available = sum(int(position["available"] or 0) for position in positions)
    sold = sum(int(position["sold"] or 0) for position in positions)
    bound = sum(int(position["lots"] or 0) for position in positions)
    enabled = sum(1 for position in positions if position["enabled"])
    delay = int(settings["followup_delay_minutes"] or 0)
    followup_state = (
        f"✅ через {delay} мин" if settings["followup_enabled"] else "⏸ выключено"
    )
    lines = [
        "🎧 <b>Spotify Account Shop</b>",
        "",
        f"Позиций: <b>{enabled}/{len(positions)}</b> включено · "
        f"лотов привязано: <b>{bound}</b>",
        f"Склад: <b>{available}</b> доступно · <b>{sold}</b> продано",
        f"Напоминание об отзыве: {followup_state}",
        "",
        "При оплате привязанного лота бот выдаёт аккаунт со склада. Через заданное "
        "время он напишет покупателю и попросит подтвердить заказ и оставить отзыв.",
    ]
    if positions:
        lines.extend(["", "<b>Позиции</b>"])
        for position in positions[:6]:
            mark = "✅" if position["enabled"] else "⏸"
            lines.append(
                f"{mark} {html.escape(_short(position['title'], 45))} — "
                f"<b>{int(position['available'] or 0)}</b> шт · "
                f"{int(position['subscription_days'] or 0)} дн."
            )
    if orders:
        lines.extend(["", "<b>Последние заказы</b>"])
        for order in orders:
            lines.append(
                f"• <code>#{html.escape(str(order['order_id']))}</code> — "
                f"{html.escape(_status_label(str(order['status'])))}; "
                f"напоминание: "
                f"{html.escape(_followup_label(str(order['followup_status'])))}"
            )
    rows: list[list[tuple[str, str]]] = [
        [("📦 Позиции и склад", f"{CALLBACK_PREFIX}pos")],
        [("📋 Заказы", f"{CALLBACK_PREFIX}orders")],
        [("➕ Новая позиция", f"{CALLBACK_PREFIX}new")],
        [("⏱ Напоминание об отзыве", f"{CALLBACK_PREFIX}fw")],
        [("📝 Шаблон выдачи", f"{CALLBACK_PREFIX}tpl")],
        [("🔄 Обновить", SETTINGS_CALLBACK)],
    ]
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))

def _show_positions(chat_id: int) -> None:
    _reset_input_state()
    positions = _sync(_positions())
    rows = [
        [
            (
                f"{'✅' if position['enabled'] else '⏸'} "
                f"{_short(position['title'], 30)} · "
                f"{int(position['available'] or 0)} шт · "
                f"{int(position['subscription_days'] or 0)} дн.",
                f"{CALLBACK_PREFIX}pos:{position['id']}",
            )
        ]
        for position in positions
    ]
    rows.append([("➕ Новая позиция", f"{CALLBACK_PREFIX}new")])
    rows.append([("⬅️ Назад", SETTINGS_CALLBACK)])
    _bot().send_message(
        chat_id,
        "📦 <b>Позиции склада</b>\n\n"
        + (
            "Выберите позицию, чтобы привязать лоты и пополнить склад."
            if positions
            else "Пока нет ни одной позиции. Создайте первую — она объединит "
            "лоты и срок подписки."
        ),
        reply_markup=_markup(*rows),
    )

def _show_position(chat_id: int, position_id: int) -> None:
    _reset_input_state()
    position = _sync(_position(position_id))
    if not position:
        raise RuntimeError("позиция не найдена")
    bindings = _sync(_position_bindings(position_id))
    preview = _sync(_stock_preview(position_id))
    lines = [
        f"🛒 <b>{html.escape(str(position['title']))}</b>",
        "",
        f"Срок подписки: <b>{int(position['subscription_days'] or 0)} дн.</b>",
        f"Склад: <b>{int(position['available'] or 0)}</b> доступно · "
        f"{int(position['reserved'] or 0)} резерв · "
        f"{int(position['sold'] or 0)} продано",
        f"Состояние: "
        f"<b>{'включена' if position['enabled'] else 'выключена'}</b>",
        "",
        "<b>Лоты</b>",
    ]
    if bindings:
        lines.extend(
            f"• {html.escape(_short(binding['lot_title'], 60))} "
            f"(<code>{html.escape(str(binding['lot_id']))}</code>)"
            for binding in bindings
        )
    else:
        lines.append(
            "— не привязано ни одного лота. Пока лот не привязан, выдачи не будет."
        )
    if preview:
        lines.extend(["", "<b>Свежие аккаунты</b>"])
        for item in preview:
            key = " · ключ" if item["activation_key"] else ""
            lines.append(
                f"• <code>{html.escape(_mask_email(str(item['email'])))}</code>{key}"
            )
    rows: list[list[tuple[str, str]]] = [
        [("➕ Пополнить склад", f"{CALLBACK_PREFIX}fill:{position_id}")],
        [
            (
                f"📦 Склад ({int(position['available'] or 0)})",
                f"{CALLBACK_PREFIX}stk:{position_id}:0",
            )
        ],
        [("➕ Добавить лот", f"{CALLBACK_PREFIX}bind:{position_id}:0")],
        [("📅 Срок подписки", f"{CALLBACK_PREFIX}days:{position_id}")],
        [("✏️ Название", f"{CALLBACK_PREFIX}name:{position_id}")],
        [
            (
                "⏸ Выключить" if position["enabled"] else "▶️ Включить",
                f"{CALLBACK_PREFIX}on:{position_id}",
            )
        ],
    ]
    rows.extend(
        [
            [("✕ " + _short(binding["lot_title"], 45), f"{CALLBACK_PREFIX}ub:{binding['id']}")]
            for binding in bindings
        ]
    )
    rows.extend(
        [
            [("🧹 Убрать остатки", f"{CALLBACK_PREFIX}clr:{position_id}")],
            [("🗑 Удалить позицию", f"{CALLBACK_PREFIX}del:{position_id}")],
            [("⬅️ Все позиции", f"{CALLBACK_PREFIX}pos")],
        ]
    )
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))

def _show_stock(chat_id: int, position_id: int, offset: int) -> None:
    _reset_input_state()
    position = _sync(_position(position_id))
    if not position:
        raise RuntimeError("позиция не найдена")
    items, total = _sync(_stock_page(position_id, offset))
    if not total:
        _bot().send_message(
            chat_id,
            f"📦 <b>Склад «{html.escape(_short(position['title'], 40))}»</b>\n\n"
            "Свободных аккаунтов нет. Нажмите «Пополнить склад», чтобы добавить "
            "строки <code>почта:пароль</code>.",
            reply_markup=_markup(
                [("➕ Пополнить склад", f"{CALLBACK_PREFIX}fill:{position_id}")],
                [("⬅️ Позиция", f"{CALLBACK_PREFIX}pos:{position_id}")],
            ),
        )
        return
    offset = max(0, min(offset, total - 1))
    pages = (total + STOCK_PAGE_SIZE - 1) // STOCK_PAGE_SIZE
    page_no = offset // STOCK_PAGE_SIZE + 1
    rows: list[list[tuple[str, str]]] = []
    for item in items:
        key = " 🔑" if item["activation_key"] else ""
        rows.append(
            [
                (
                    f"🗑 {_mask_email(str(item['email']))}{key}",
                    f"{CALLBACK_PREFIX}id:{item['id']}",
                )
            ]
        )
    nav: list[tuple[str, str]] = []
    if offset > 0:
        nav.append(
            (
                "⬅️",
                f"{CALLBACK_PREFIX}stk:{position_id}:"
                f"{max(0, offset - STOCK_PAGE_SIZE)}",
            )
        )
    if offset + STOCK_PAGE_SIZE < total:
        nav.append(
            (
                "➡️",
                f"{CALLBACK_PREFIX}stk:{position_id}:"
                f"{offset + STOCK_PAGE_SIZE}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [("➕ Пополнить склад", f"{CALLBACK_PREFIX}fill:{position_id}")]
    )
    rows.append([("⬅️ Позиция", f"{CALLBACK_PREFIX}pos:{position_id}")])
    _bot().send_message(
        chat_id,
        f"📦 <b>Склад «{html.escape(_short(position['title'], 40))}»</b>\n\n"
        f"Страница <b>{page_no}/{pages}</b> · свободных аккаунтов: "
        f"<b>{total}</b>\n\n"
        "Нажмите на аккаунт, чтобы удалить его со склада.",
        reply_markup=_markup(*rows),
    )

def _show_orders(chat_id: int) -> None:
    _reset_input_state()
    orders = _sync(_recent_orders(10))
    if not orders:
        _bot().send_message(
            chat_id,
            "📋 <b>Заказы</b>\n\nПока ни одного заказа.",
            reply_markup=_markup([("⬅️ Назад", SETTINGS_CALLBACK)]),
        )
        return
    lines = ["📋 <b>Заказы</b>", ""]
    rows: list[list[tuple[str, str]]] = []
    for order in orders:
        status = str(order["status"])
        lines.append(
            f"• <code>#{html.escape(str(order['order_id']))}</code> — "
            f"<b>{html.escape(_status_label(status))}</b> · "
            f"{html.escape(_short(str(order['position_title'] or '—'), 40))}"
        )
        if order["error_text"]:
            lines.append(
                f"   ↳ <code>{html.escape(_short(str(order['error_text']), 120))}</code>"
            )
        if status in {"queued", "failed", "unmatched"}:
            rows.append(
                [
                    (
                        f"▶️ Выдать #{_short(str(order['order_id']), 18)}",
                        f"{CALLBACK_PREFIX}go:{order['id']}",
                    )
                ]
            )
    lines.extend(
        [
            "",
            "«Выдать» выдаёт аккаунт со склада повторно: полезно, если позиция "
            "была не привязана или склад был пуст.",
        ]
    )
    rows.append([("⬅️ Назад", SETTINGS_CALLBACK)])
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))

def _load_lots() -> dict[str, str]:
    profile = _cardinal.account.get_user(_cardinal.account.id)
    lots: dict[str, str] = {}
    for lot in profile.get_lots():
        title = str(getattr(lot, "description", None) or f"Лот {lot.id}")
        lots[str(lot.id)] = title
    return lots

def _show_bind_lots(chat_id: int, position_id: int, offset: int) -> None:
    _reset_input_state()
    lots = _load_lots()
    bound = _sync(_all_bindings())
    busy = {str(binding["lot_id"]) for binding in bound}
    position = _sync(_position(position_id))
    if not position:
        raise RuntimeError("позиция не найдена")
    free = [
        (lot_id, title)
        for lot_id, title in sorted(
            lots.items(),
            key=lambda pair: int(pair[0]) if pair[0].isdigit() else 0,
        )
        if lot_id not in busy
    ]
    if not free:
        _bot().send_message(
            chat_id,
            "✅ Все найденные лоты уже привязаны к позициям. "
            "Чтобы перенести лот, сначала открепите его.",
            reply_markup=_markup(
                [("⬅️ Позиция", f"{CALLBACK_PREFIX}pos:{position_id}")]
            ),
        )
        return
    offset = max(0, min(offset, len(free) - 1))
    page = free[offset : offset + LOT_PAGE_SIZE]
    rows = [
        [
            (
                f"{_short(title, 45)} · {lot_id}",
                f"{CALLBACK_PREFIX}bl:{position_id}:{lot_id}",
            )
        ]
        for lot_id, title in page
    ]
    nav: list[tuple[str, str]] = []
    if offset > 0:
        nav.append(
            (
                "⬅️",
                f"{CALLBACK_PREFIX}bind:{position_id}:"
                f"{max(0, offset - LOT_PAGE_SIZE)}",
            )
        )
    if offset + LOT_PAGE_SIZE < len(free):
        nav.append(
            (
                "➡️",
                f"{CALLBACK_PREFIX}bind:{position_id}:"
                f"{offset + LOT_PAGE_SIZE}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append([("⬅️ Позиция", f"{CALLBACK_PREFIX}pos:{position_id}")])
    pages = (len(free) + LOT_PAGE_SIZE - 1) // LOT_PAGE_SIZE
    page_no = offset // LOT_PAGE_SIZE + 1
    _bot().send_message(
        chat_id,
        f"➕ <b>Добавить лот в «{html.escape(_short(position['title'], 40))}»</b>\n\n"
        f"Страница <b>{page_no}/{pages}</b> · всего свободных лотов: "
        f"<b>{len(free)}</b>\n"
        "Нажмите лот, чтобы привязать его к этой позиции: при оплате его "
        "заказов аккаунт будет выдаваться с этого склада.\n\n"
        "Лоты, уже занятые другой позицией, в списке не показываются.",
        reply_markup=_markup(*rows),
    )

def _show_followup(chat_id: int) -> None:
    _reset_input_state()
    settings = _sync(_settings())
    delay = int(settings["followup_delay_minutes"] or 0)
    template = _followup_template(settings)
    lines = [
        "⏱ <b>Напоминание после выдачи</b>",
        "",
        f"Включено: <b>{'да' if settings['followup_enabled'] else 'нет'}</b>",
        f"Задержка: <b>{delay} мин</b>",
        f"Ссылка на отзывы: "
        f"<code>{html.escape(str(settings['review_url'] or '—'))}</code>",
        "",
        "<b>Текст напоминания</b>",
        "<blockquote>" + html.escape(_short(template, 1200)) + "</blockquote>",
    ]
    rows = [
        [
            (
                "⏸ Выключить" if settings["followup_enabled"] else "▶️ Включить",
                f"{CALLBACK_PREFIX}fwt",
            )
        ],
        [("🕒 Изменить задержку", f"{CALLBACK_PREFIX}fwd")],
        [("✏️ Изменить текст", f"{CALLBACK_PREFIX}fwm")],
        [("🔗 Ссылка на отзывы", f"{CALLBACK_PREFIX}fwu")],
        [("↩️ Стандартный текст", f"{CALLBACK_PREFIX}fwr")],
        [("⬅️ Назад", SETTINGS_CALLBACK)],
    ]
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))

def _show_template(chat_id: int) -> None:
    _reset_input_state()
    settings = _sync(_settings())
    template = _delivery_template(settings)
    custom = bool(str(settings["delivery_template"] or "").strip())
    lines = [
        "📝 <b>Шаблон выдачи</b>",
        "",
        f"Источник: <b>{'свой шаблон' if custom else 'стандартный'}</b>",
        "Поля: <code>{email}</code>, <code>{password}</code>, "
        "<code>{key}</code>, <code>{days}</code>, <code>{until}</code>, "
        "<code>{order_id}</code>, <code>{buyer}</code>",
        "",
        "<blockquote>" + html.escape(_short(template, 1500)) + "</blockquote>",
    ]
    rows = [
        [("✏️ Изменить шаблон", f"{CALLBACK_PREFIX}tple")],
        [("👁 Пример сообщения", f"{CALLBACK_PREFIX}tplp")],
        [("↩️ Стандартный", f"{CALLBACK_PREFIX}tplr")],
        [("⬅️ Назад", SETTINGS_CALLBACK)],
    ]
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))

def _prompt(chat_id: int, key: str, text: str, context: int | None = None) -> None:
    global _pending_input, _stock_mode
    _pending_input = (key, context)
    _stock_mode = None
    _bot().send_message(chat_id, text)

def _enter_stock(chat_id: int, position_id: int) -> None:
    global _pending_input, _stock_mode
    position = _sync(_position(position_id))
    if not position:
        raise RuntimeError("позиция не найдена")
    _pending_input = (STOCK_PENDING_KEY, position_id)
    _stock_mode = {"position_id": position_id, "added": 0}
    _bot().send_message(
        chat_id,
        f"📦 <b>Пополнение склада "
        f"«{html.escape(_short(position['title'], 40))}»</b>\n\n"
        "Отправляйте аккаунты сообщением, по одному в строке:\n"
        "<code>почта:пароль</code>\n"
        "<code>почта:пароль:ключ_активации</code>\n\n"
        "Можно отправить сразу несколько строк. Повторы по почте пропускаются. "
        "Когда закончите — нажмите «Готово».",
        reply_markup=_markup(
            [
                ("✅ Готово", f"{CALLBACK_PREFIX}stockdone"),
                ("⬅️ Позиция", f"{CALLBACK_PREFIX}pos:{position_id}"),
            ]
        ),
    )

def _finish_stock(chat_id: int) -> None:
    global _stock_mode
    if not _stock_mode:
        _show_positions(chat_id)
        return
    position_id = int(_stock_mode["position_id"])
    added = int(_stock_mode.get("added", 0))
    _stock_mode = None
    _bot().send_message(
        chat_id, f"✅ Склад пополнен: добавлено <b>{added}</b> аккаунтов."
    )
    _show_position(chat_id, position_id)
    _spawn(_fulfill_queued())

def _handle_stock_input(message: Any) -> None:
    global _stock_mode
    if not _stock_mode:
        return
    chat_id = int(message.chat.id)
    text = str(message.text or "")
    if text.strip().casefold() in {"готово", "done", "стоп", "хватит"}:
        _finish_stock(chat_id)
        return
    position_id = int(_stock_mode["position_id"])
    entries, errors, duplicates = _parse_stock_lines(text)
    if not entries:
        _bot().send_message(
            chat_id,
            "❌ Не найдено ни одной корректной строки.\n\n"
            + "\n".join(errors[:5])
            + "\n\nФормат: <code>почта:пароль</code> или "
            "<code>почта:пароль:ключ</code>.",
        )
        return
    added, skipped = _sync(_add_items(position_id, entries))
    _stock_mode["added"] = int(_stock_mode.get("added", 0)) + added
    sample = entries[-1]
    lines = [f"✅ Добавлено: <b>{added}</b>"]
    if skipped or duplicates:
        lines.append(f"⏭ Пропущено повторов: <b>{skipped + duplicates}</b>")
    lines.append(
        f"Пример разбора: <code>{html.escape(sample[0])}</code> · "
        f"пароль {len(sample[1])} симв. · ключ: {'да' if sample[2] else 'нет'}"
    )
    if errors:
        lines.extend(["", "<b>Проблемные строки</b>", *errors[:5]])
    lines.extend(
        [
            "",
            f"Добавлено за сессию: <b>{_stock_mode['added']}</b>",
            "Отправьте следующую партию или нажмите «Готово».",
        ]
    )
    _bot().send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=_markup(
            [
                ("✅ Готово", f"{CALLBACK_PREFIX}stockdone"),
                ("⬅️ Позиция", f"{CALLBACK_PREFIX}pos:{position_id}"),
            ]
        ),
    )
    _spawn(_fulfill_queued())

def _handle_pending_input(message: Any) -> None:
    global _pending_input
    if _pending_input is None:
        return
    key, context = _pending_input
    _pending_input = None
    if key == STOCK_PENDING_KEY:
        _handle_stock_input(message)
        if _stock_mode is not None:
            _pending_input = (
                STOCK_PENDING_KEY,
                int(_stock_mode["position_id"]),
            )
        return
    value = str(message.text or "").strip()
    chat_id = int(message.chat.id)
    try:
        if key == "new_title":
            title = _validate_title(value)
            position = _sync(_create_position(title, DEFAULT_POSITION_DAYS))
            _prompt(
                chat_id,
                "new_days",
                "Шаг 2/2. На сколько дней подписка в этой позиции?\n"
                "Число от 1 до 3650 (по умолчанию 30). Оно попадёт в сообщение "
                "о выдаче.",
                int(position["id"]),
            )
            return
        if key == "new_days":
            days = _positive_int(
                value, minimum=1, maximum=3650, label="срок подписки"
            )
            if context is None:
                raise RuntimeError("позиция не выбрана")
            _sync(_update_position(context, "subscription_days", days))
            _bot().send_message(
                chat_id,
                "✅ Позиция создана. Привяжите лот и пополните склад — "
                "после этого выдача заработает автоматически.",
            )
            _show_position(chat_id, context)
            return
        if key == "title":
            title = _validate_title(value)
            if context is None:
                raise RuntimeError("позиция не выбрана")
            _sync(_update_position(context, "title", title))
            _show_position(chat_id, context)
            return
        if key == "days":
            days = _positive_int(
                value, minimum=1, maximum=3650, label="срок подписки"
            )
            if context is None:
                raise RuntimeError("позиция не выбрана")
            _sync(_update_position(context, "subscription_days", days))
            _bot().send_message(chat_id, "✅ Срок подписки обновлён.")
            _show_position(chat_id, context)
            return
        if key == "followup_delay":
            minutes = _positive_int(
                value,
                minimum=1,
                maximum=MAX_FOLLOWUP_MINUTES,
                label="задержка",
            )
            _sync(_update_setting("followup_delay_minutes", minutes))
            _bot().send_message(
                chat_id,
                f"✅ Напоминание будет отправляться через {minutes} мин.",
            )
            _show_followup(chat_id)
            return
        if key == "followup_url":
            url = _validate_review_url(value)
            _sync(_update_setting("review_url", url))
            _bot().send_message(chat_id, "✅ Ссылка сохранена.")
            _show_followup(chat_id)
            return
        if key == "followup_text":
            template = _validate_followup_template(value)
            _sync(_update_setting("followup_template", template))
            _bot().send_message(chat_id, "✅ Текст напоминания обновлён.")
            _show_followup(chat_id)
            return
        if key == "delivery_template":
            template = _validate_delivery_template(value)
            _sync(_update_setting("delivery_template", template))
            _bot().send_message(chat_id, "✅ Шаблон выдачи обновлён.")
            _show_template(chat_id)
            return
        raise RuntimeError("неизвестный запрос")
    except Exception as exc:
        _pending_input = (key, context)
        logger.exception("Не сохранён ввод Spotify Account Shop")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")

def _answer(call: Any) -> None:
    try:
        _bot().answer_callback_query(call.id)
    except Exception:
        logger.warning("Не удалось ответить на callback", exc_info=True)

def _on_callback(call: Any) -> None:
    global _stock_mode
    data = str(call.data or "")
    chat_id = int(call.message.chat.id)
    try:
        _answer(call)
        if data == f"{CALLBACK_PREFIX}home" or data.startswith(
            f"{PLUGIN_SETTINGS_PREFIX}:{UUID}:"
        ):
            _show_home(chat_id)
        elif data == f"{CALLBACK_PREFIX}pos":
            _stock_mode = None
            _show_positions(chat_id)
        elif data == f"{CALLBACK_PREFIX}orders":
            _show_orders(chat_id)
        elif data.startswith(f"{CALLBACK_PREFIX}stk:"):
            _, _, tail = data.split(":", 2)
            position_id, offset = tail.split(":")
            _show_stock(chat_id, int(position_id), int(offset))
        elif data.startswith(f"{CALLBACK_PREFIX}idy:"):
            item_id = int(data.rsplit(":", 1)[1])
            position_id = _sync(_delete_item(item_id))
            if position_id is None:
                raise RuntimeError("аккаунт уже выдан или удалён")
            _bot().send_message(chat_id, "🗑 Аккаунт удалён со склада.")
            _show_stock(chat_id, position_id, 0)
        elif data.startswith(f"{CALLBACK_PREFIX}id:"):
            item_id = int(data.rsplit(":", 1)[1])
            _bot().send_message(
                chat_id,
                "Удалить этот аккаунт со склада? Действие необратимо.",
                reply_markup=_markup(
                    [("Да, удалить", f"{CALLBACK_PREFIX}idy:{item_id}")],
                    [("Отмена", SETTINGS_CALLBACK)],
                ),
            )
        elif data.startswith(f"{CALLBACK_PREFIX}go:"):
            order_db_id = int(data.rsplit(":", 1)[1])
            result = _sync(_retry_order(order_db_id))
            _bot().send_message(
                chat_id,
                {
                    "delivered": "✅ Аккаунт выдан покупателю.",
                    "no_stock": "🛑 На складе нет свободных аккаунтов. "
                    "Пополните склад — заказ выдастся автоматически.",
                    "handled": "Заказ уже обработан.",
                    "error": "❌ Выдача завершилась ошибкой, подробности ушли "
                    "в уведомления.",
                }.get(str(result), f"Состояние заказа: {html.escape(str(result))}"),
            )
            _show_orders(chat_id)
        elif data == f"{CALLBACK_PREFIX}new":
            _prompt(
                chat_id,
                "new_title",
                "Шаг 1/2. Отправьте название позиции, например "
                "<code>Spotify Premium 30 дней</code>.",
            )
        elif data.startswith(f"{CALLBACK_PREFIX}pos:"):
            _show_position(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith(f"{CALLBACK_PREFIX}fill:"):
            _enter_stock(chat_id, int(data.rsplit(":", 1)[1]))
        elif data == f"{CALLBACK_PREFIX}stockdone":
            _finish_stock(chat_id)
        elif data.startswith(f"{CALLBACK_PREFIX}bind:"):
            _, _, tail = data.split(":", 2)
            position_id, offset = tail.split(":")
            _show_bind_lots(chat_id, int(position_id), int(offset))
        elif data.startswith(f"{CALLBACK_PREFIX}bl:"):
            _, _, tail = data.split(":", 2)
            position_id, lot_id = tail.split(":")
            lots = _load_lots()
            title = lots.get(lot_id)
            if not title:
                raise RuntimeError("лот не найден; обновите список")
            _sync(_bind_lot(int(position_id), lot_id, title))
            _bot().send_message(
                chat_id, f"✅ Лот привязан: {html.escape(_short(title, 60))}"
            )
            _show_position(chat_id, int(position_id))
        elif data.startswith(f"{CALLBACK_PREFIX}ub:"):
            binding_id = int(data.rsplit(":", 1)[1])
            _sync(_unbind(binding_id))
            _bot().send_message(chat_id, "✅ Лот откреплён от позиции.")
            _show_positions(chat_id)
        elif data.startswith(f"{CALLBACK_PREFIX}days:"):
            position_id = int(data.rsplit(":", 1)[1])
            _prompt(
                chat_id,
                "days",
                "Отправьте новый срок подписки в днях (1–3650).",
                position_id,
            )
        elif data.startswith(f"{CALLBACK_PREFIX}name:"):
            position_id = int(data.rsplit(":", 1)[1])
            _prompt(
                chat_id, "title", "Отправьте новое название позиции.", position_id
            )
        elif data.startswith(f"{CALLBACK_PREFIX}on:"):
            position_id = int(data.rsplit(":", 1)[1])
            position = _sync(_position(position_id))
            if not position:
                raise RuntimeError("позиция не найдена")
            _sync(
                _update_position(
                    position_id, "enabled", not bool(position["enabled"])
                )
            )
            _show_position(chat_id, position_id)
        elif data.startswith(f"{CALLBACK_PREFIX}clr:"):
            position_id = int(data.rsplit(":", 1)[1])
            position = _sync(_position(position_id))
            if not position:
                raise RuntimeError("позиция не найдена")
            _bot().send_message(
                chat_id,
                f"Убрать со склада <b>{int(position['available'] or 0)}</b> "
                "непроданных аккаунтов? Действие необратимо.",
                reply_markup=_markup(
                    [("Да, убрать", f"{CALLBACK_PREFIX}clry:{position_id}")],
                    [("Отмена", f"{CALLBACK_PREFIX}pos:{position_id}")],
                ),
            )
        elif data.startswith(f"{CALLBACK_PREFIX}clry:"):
            position_id = int(data.rsplit(":", 1)[1])
            amount = _sync(_clear_available(position_id))
            _bot().send_message(chat_id, f"🧹 Убрано аккаунтов: <b>{amount}</b>.")
            _show_position(chat_id, position_id)
        elif data.startswith(f"{CALLBACK_PREFIX}del:"):
            position_id = int(data.rsplit(":", 1)[1])
            _bot().send_message(
                chat_id,
                "Удалить позицию вместе с непроданным складом и привязками "
                "лотов?",
                reply_markup=_markup(
                    [("Да, удалить", f"{CALLBACK_PREFIX}dely:{position_id}")],
                    [("Отмена", f"{CALLBACK_PREFIX}pos:{position_id}")],
                ),
            )
        elif data.startswith(f"{CALLBACK_PREFIX}dely:"):
            position_id = int(data.rsplit(":", 1)[1])
            _sync(_delete_position(position_id))
            _bot().send_message(chat_id, "🗑 Позиция удалена.")
            _show_positions(chat_id)
        elif data == f"{CALLBACK_PREFIX}fw":
            _show_followup(chat_id)
        elif data == f"{CALLBACK_PREFIX}fwt":
            settings = _sync(_settings())
            _sync(
                _update_setting(
                    "followup_enabled", not bool(settings["followup_enabled"])
                )
            )
            _show_followup(chat_id)
        elif data == f"{CALLBACK_PREFIX}fwd":
            _prompt(
                chat_id,
                "followup_delay",
                "Через сколько минут после выдачи напоминать покупателю? "
                f"От 1 до {MAX_FOLLOWUP_MINUTES}.",
            )
        elif data == f"{CALLBACK_PREFIX}fwm":
            _prompt(
                chat_id,
                "followup_text",
                "Отправьте текст напоминания.\n\nПоля: <code>{buyer}</code>, "
                "<code>{order_id}</code>, <code>{email}</code>, "
                "<code>{days}</code>, <code>{until}</code>, "
                "<code>{review_url}</code>, <code>{confirm_hint}</code>.",
            )
        elif data == f"{CALLBACK_PREFIX}fwu":
            _prompt(
                chat_id,
                "followup_url",
                "Отправьте ссылку на страницу отзывов (используется как "
                "<code>{review_url}</code>).",
            )
        elif data == f"{CALLBACK_PREFIX}fwr":
            _sync(_update_setting("followup_template", None))
            _bot().send_message(chat_id, "✅ Возвращён стандартный текст.")
            _show_followup(chat_id)
        elif data == f"{CALLBACK_PREFIX}tpl":
            _show_template(chat_id)
        elif data == f"{CALLBACK_PREFIX}tple":
            _prompt(
                chat_id,
                "delivery_template",
                "Отправьте новый шаблон выдачи. Обязательны поля "
                "<code>{email}</code> и <code>{password}</code>; строка с "
                "<code>{key}</code> убирается, если ключа нет.",
            )
        elif data == f"{CALLBACK_PREFIX}tplr":
            _sync(_update_setting("delivery_template", None))
            _bot().send_message(chat_id, "✅ Возвращён стандартный шаблон.")
            _show_template(chat_id)
        elif data == f"{CALLBACK_PREFIX}tplp":
            settings = _sync(_settings())
            preview = _delivery_preview(
                _delivery_template(settings), DEFAULT_POSITION_DAYS, with_key=True
            )
            _bot().send_message(
                chat_id,
                "👁 <b>Пример сообщения покупателю</b>\n\n<pre>"
                + html.escape(preview)
                + "</pre>",
            )
        elif data.startswith(f"{CALLBACK_PREFIX}back:"):
            order_id = data.split(":", 2)[2]
            _spawn(_return_item_to_stock(order_id))
            _bot().send_message(chat_id, "↩️ Аккаунт возвращается на склад.")
        else:
            logger.warning("Неизвестный callback Spotify Account Shop: %s", data)
    except Exception as exc:
        logger.exception("Ошибка callback Spotify Account Shop")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")

def _on_setting_message(message: Any) -> None:
    """Обработчик ввода настроек плагина.

    Имя важно: старые сборки FunPayCardinal вызывают именно
    ``_on_setting_message`` напрямую, когда у плагина заполнен
    ``_pending_input``. Иначе сообщение проходит через общий telebot-dispatch
    и может не дойти до плагина — тогда после отправки названия позиции
    визуально ничего не происходит.
    """
    try:
        if _pending_input is not None:
            _handle_pending_input(message)
            return
        if _stock_mode is not None:
            _handle_stock_input(message)
    except Exception:
        logger.exception("Ошибка обработки текста Spotify Account Shop")

def _on_text_message(message: Any) -> None:
    # Совместимый алиас для регистраций, сохранённых старой версией плагина.
    _on_setting_message(message)

# --------------------------------------------------------------------------- #
# Хуки плагина
# --------------------------------------------------------------------------- #

def pre_init(cardinal: Any) -> None:
    global _cardinal
    _cardinal = cardinal
    _sync(_ensure_schema())
    bot = cardinal.telegram.bot
    bot.register_callback_query_handler(
        _on_callback,
        func=lambda call: _is_own_callback(str(call.data or "")),
    )
    bot.register_message_handler(
        _on_setting_message,
        content_types=["text"],
        func=lambda _message: _pending_input is not None
        or _stock_mode is not None,
    )

def post_start(cardinal: Any) -> None:
    global _cardinal, _worker_future
    _cardinal = cardinal
    _spawn(_resume_state())
    _spawn(_poll_paid_orders())
    if _worker_future is None or _worker_future.done():
        _worker_future = _spawn(_worker_loop())

def new_order(cardinal: Any, event: Any) -> None:
    order = event.order
    status = getattr(getattr(order, "status", None), "name", "")
    if status and status != "PAID":
        return
    description = str(getattr(order, "description", "") or "")
    _spawn(
        _process_new_order(
            {
                "id": str(order.id),
                "chat_id": getattr(order, "chat_id", None),
                "chat_name": str(getattr(order, "buyer_username", "") or ""),
                "description": description,
                "amount": max(1, int(getattr(order, "amount", None) or 1)),
                "lot_id": _order_lot_id(order),
                "builtin_conflict": _builtin_delivery_conflict(
                    description, _order_lot_id(order)
                ),
            }
        )
    )

def order_status_changed(cardinal: Any, event: Any) -> None:
    status = getattr(getattr(event.order, "status", None), "name", "")
    _spawn(_process_order_status(str(event.order.id), status))

def pre_stop(cardinal: Any) -> None:
    global _pending_input, _stock_mode, _worker_future
    _pending_input = None
    _stock_mode = None
    _worker_future = None
    for future in list(_futures):
        future.cancel()

def on_delete(cardinal: Any, callback: Any) -> None:
    pre_stop(cardinal)
    telegram_id = _telegram_id()
    for table in (
        "spotify_accounts_orders",
        "spotify_accounts_items",
        "spotify_accounts_bindings",
        "spotify_accounts_positions",
        "spotify_accounts_settings",
    ):
        _sync(
            _db().execute(
                f"DELETE FROM {table} WHERE telegram_id=$1",
                telegram_id,
            )
        )

BIND_TO_PRE_INIT = [pre_init]
BIND_TO_POST_INIT = []
BIND_TO_PRE_START = []
BIND_TO_POST_START = [post_start]
BIND_TO_PRE_STOP = [pre_stop]
BIND_TO_POST_STOP = []
BIND_TO_INIT_MESSAGE = []
BIND_TO_MESSAGES_LIST_CHANGED = []
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = []
BIND_TO_NEW_MESSAGE = []
BIND_TO_INIT_ORDER = []
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_ORDERS_LIST_CHANGED = []
BIND_TO_ORDER_STATUS_CHANGED = [order_status_changed]
BIND_TO_PRE_DELIVERY = []
BIND_TO_POST_DELIVERY = []
BIND_TO_PRE_LOTS_RAISE = []
BIND_TO_POST_LOTS_RAISE = []
BIND_TO_TELETHON_READY = []
BIND_TO_TELETHON_DISCONNECTED = []
BIND_TO_DELETE = on_delete
