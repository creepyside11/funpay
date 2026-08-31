from __future__ import annotations

import asyncio
import html
import logging
from concurrent.futures import CancelledError, Future
from typing import Any
from urllib.parse import urlsplit

import requests
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


NAME = "AutoSmm"
VERSION = "1.0.0"
DESCRIPTION = "Автоматические SMM-заказы для нескольких лотов FunPay"
CREDITS = "FunPay aiogram bot"
SETTINGS_PAGE = True
TELETHON = False
UUID = "6a76248a-f44d-4fc3-98d5-d40c0a2663b7"

CALLBACK_PREFIX = "asm:"
SETTINGS_CALLBACK = f"47:{UUID}:0"
DEFAULT_API_URL = "https://smmway.ru/api/v2"
POLL_SECONDS = 30

logger = logging.getLogger("fpc_plugin.auto_smm")

_cardinal: Any | None = None
_pending_input: tuple[str, int | None] | None = None
_draft_rule: dict[str, Any] = {}
_lot_cache: dict[str, tuple[str, str]] = {}
_futures: set[Future[Any]] = set()
_running_job_ids: set[int] = set()
_submitting_job_ids: set[int] = set()


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
    if service is None:
        raise RuntimeError("хранилище секретов недоступно")
    return service.secrets


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
            logger.exception("Фоновая задача AutoSmm завершилась с ошибкой")

    future.add_done_callback(done)
    return future


async def _ensure_schema() -> None:
    await _db().execute(
        """
        CREATE TABLE IF NOT EXISTS autosmm_settings (
            telegram_id BIGINT PRIMARY KEY
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            api_base_url TEXT NOT NULL DEFAULT 'https://smmway.ru/api/v2',
            api_token_enc TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS autosmm_lot_rules (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            lot_id TEXT NOT NULL,
            lot_title TEXT NOT NULL,
            service_id BIGINT NOT NULL,
            quantity_per_unit INTEGER NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, lot_id)
        );

        CREATE TABLE IF NOT EXISTS autosmm_jobs (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            order_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            buyer_id BIGINT,
            rule_id BIGINT REFERENCES autosmm_lot_rules(id) ON DELETE SET NULL,
            lot_title TEXT NOT NULL,
            service_id BIGINT NOT NULL,
            quantity_per_unit INTEGER NOT NULL,
            purchased_units INTEGER NOT NULL,
            total_quantity INTEGER NOT NULL,
            target_url TEXT,
            smm_order_id TEXT,
            smm_status TEXT,
            status TEXT NOT NULL DEFAULT 'awaiting_link',
            error_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, order_id)
        );

        CREATE INDEX IF NOT EXISTS autosmm_jobs_status_idx
            ON autosmm_jobs (telegram_id, status, updated_at DESC);
        """
    )
    await _db().execute(
        """
        INSERT INTO autosmm_settings (telegram_id)
        VALUES ($1)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        _telegram_id(),
    )


async def _settings() -> Any:
    await _ensure_schema()
    return await _db().fetchrow(
        "SELECT * FROM autosmm_settings WHERE telegram_id=$1",
        _telegram_id(),
    )


async def _set_setting(column: str, value: Any) -> None:
    if column not in {"api_base_url", "api_token_enc"}:
        raise ValueError("неизвестная настройка")
    await _ensure_schema()
    await _db().execute(
        f"""
        UPDATE autosmm_settings
           SET {column}=$2, updated_at=NOW()
         WHERE telegram_id=$1
        """,
        _telegram_id(),
        value,
    )


async def _rules() -> list[Any]:
    await _ensure_schema()
    return list(
        await _db().fetch(
            """
            SELECT * FROM autosmm_lot_rules
             WHERE telegram_id=$1
             ORDER BY created_at, id
            """,
            _telegram_id(),
        )
    )


async def _rule(rule_id: int) -> Any | None:
    return await _db().fetchrow(
        """
        SELECT * FROM autosmm_lot_rules
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        rule_id,
    )


async def _upsert_rule(
    lot_id: str, lot_title: str, service_id: int, quantity_per_unit: int
) -> None:
    await _ensure_schema()
    await _db().execute(
        """
        INSERT INTO autosmm_lot_rules
            (telegram_id, lot_id, lot_title, service_id, quantity_per_unit)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (telegram_id, lot_id) DO UPDATE
            SET lot_title=EXCLUDED.lot_title,
                service_id=EXCLUDED.service_id,
                quantity_per_unit=EXCLUDED.quantity_per_unit,
                enabled=TRUE,
                updated_at=NOW()
        """,
        _telegram_id(),
        lot_id,
        lot_title,
        service_id,
        quantity_per_unit,
    )


async def _update_rule(rule_id: int, column: str, value: Any) -> None:
    if column not in {"service_id", "quantity_per_unit", "enabled"}:
        raise ValueError("неизвестное поле привязки")
    await _db().execute(
        f"""
        UPDATE autosmm_lot_rules
           SET {column}=$3, updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2
        """,
        _telegram_id(),
        rule_id,
        value,
    )


async def _delete_rule(rule_id: int) -> None:
    await _db().execute(
        "DELETE FROM autosmm_lot_rules WHERE telegram_id=$1 AND id=$2",
        _telegram_id(),
        rule_id,
    )


async def _recent_jobs(limit: int = 5) -> list[Any]:
    await _ensure_schema()
    return list(
        await _db().fetch(
            """
            SELECT * FROM autosmm_jobs
             WHERE telegram_id=$1
             ORDER BY created_at DESC LIMIT $2
            """,
            _telegram_id(),
            limit,
        )
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


def _validate_api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("нужен HTTPS URL без логина и пароля")
    if parsed.fragment or parsed.query:
        raise ValueError("Base URL не должен содержать query или fragment")
    return value


def _validate_target_url(value: str) -> str:
    value = value.strip()
    if len(value) > 2048:
        raise ValueError("ссылка слишком длинная")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("отправьте полную ссылку, начинающуюся с http:// или https://")
    if parsed.username or parsed.password:
        raise ValueError("ссылка с логином или паролем не поддерживается")
    return value


def _calculate_total_quantity(quantity_per_unit: int, purchased_units: int | None) -> int:
    quantity = int(quantity_per_unit)
    units = max(1, int(purchased_units or 1))
    total = quantity * units
    if quantity < 1 or total > 1_000_000_000:
        raise ValueError("итоговое количество выходит за допустимые пределы")
    return total


def _smm_request(settings: Any, **payload: Any) -> dict[str, Any]:
    token = _api_token(settings)
    if not token:
        raise RuntimeError("API-токен не задан")
    response = requests.post(
        str(settings["api_base_url"]),
        data={"key": token, **payload},
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


def _status_label(status: str) -> str:
    return {
        "awaiting_link": "ожидается ссылка",
        "link_confirmation": "ожидается подтверждение",
        "submitted": "накрутка идёт",
        "completed": "выполнен",
        "canceled": "отменён",
        "failed": "ошибка",
    }.get(status, status)


def _show_settings(chat_id: int) -> None:
    settings = _sync(_settings())
    rules = _sync(_rules())
    jobs = _sync(_recent_jobs())
    enabled_rules = sum(1 for rule in rules if rule["enabled"])
    lines = [
        "⚙️ <b>AutoSmm</b>",
        "",
        f"Base URL: <code>{html.escape(str(settings['api_base_url']))}</code>",
        f"API-токен: <b>{html.escape(_token_label(settings))}</b>",
        f"Привязки лотов: <b>{enabled_rules}/{len(rules)} включено</b>",
        "",
        "Для каждого лота отдельно задаются ID услуги и количество на одну купленную единицу.",
    ]
    if rules:
        lines.extend(["", "<b>Лоты</b>"])
        for rule in rules[:12]:
            marker = "✅" if rule["enabled"] else "⏸"
            lines.append(
                f"{marker} {html.escape(str(rule['lot_title'])[:70])}\n"
                f"   услуга <code>{rule['service_id']}</code> · {rule['quantity_per_unit']} за 1 шт."
            )
        if len(rules) > 12:
            lines.append(f"…ещё {len(rules) - 12}")
    if jobs:
        lines.extend(["", "<b>Последние задания</b>"])
        for job in jobs:
            lines.append(
                f"• <code>#{html.escape(str(job['order_id']))}</code> — "
                f"{html.escape(_status_label(str(job['status'])))}; "
                f"{job['total_quantity']} шт.; SMM {html.escape(str(job['smm_status'] or '—'))}"
            )
    rows: list[list[tuple[str, str]]] = [
        [("🌐 Base URL API", f"{CALLBACK_PREFIX}set:base")],
        [("🔑 API-токен", f"{CALLBACK_PREFIX}set:token")],
        [("➕ Добавить лот", f"{CALLBACK_PREFIX}lots")],
        [("🧩 Управление лотами", f"{CALLBACK_PREFIX}rules")],
        [("🧪 Проверить API", f"{CALLBACK_PREFIX}api")],
        [("🔄 Обновить", SETTINGS_CALLBACK)],
    ]
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))


def _show_rules(chat_id: int) -> None:
    rules = _sync(_rules())
    rows = [
        [
            (
                f"{'✅' if rule['enabled'] else '⏸'} {str(rule['lot_title'])[:42]}",
                f"{CALLBACK_PREFIX}r:{rule['id']}",
            )
        ]
        for rule in rules
    ]
    rows.extend(
        [
            [("➕ Добавить лот", f"{CALLBACK_PREFIX}lots")],
            [("⬅️ Настройки", SETTINGS_CALLBACK)],
        ]
    )
    text = (
        "🧩 <b>Привязки AutoSmm</b>\n\nВыберите лот для изменения."
        if rules
        else "🧩 Привязок пока нет. Добавьте первый лот."
    )
    _bot().send_message(chat_id, text, reply_markup=_markup(*rows))


def _show_rule(chat_id: int, rule_id: int) -> None:
    rule = _sync(_rule(rule_id))
    if not rule:
        raise RuntimeError("привязка не найдена")
    state = "включена" if rule["enabled"] else "выключена"
    text = (
        f"🛒 <b>{html.escape(str(rule['lot_title']))}</b>\n\n"
        f"ID лота: <code>{html.escape(str(rule['lot_id']))}</code>\n"
        f"ID услуги: <code>{rule['service_id']}</code>\n"
        f"Количество за 1 шт.: <b>{rule['quantity_per_unit']}</b>\n"
        f"Статус: <b>{state}</b>"
    )
    rows = [
        [("🧩 Изменить ID услуги", f"{CALLBACK_PREFIX}rs:{rule_id}")],
        [("📦 Изменить количество", f"{CALLBACK_PREFIX}rq:{rule_id}")],
        [
            (
                "⏸ Выключить" if rule["enabled"] else "▶️ Включить",
                f"{CALLBACK_PREFIX}rt:{rule_id}",
            )
        ],
        [("🗑 Удалить привязку", f"{CALLBACK_PREFIX}rd:{rule_id}")],
        [("⬅️ Все лоты", f"{CALLBACK_PREFIX}rules")],
    ]
    _bot().send_message(chat_id, text, reply_markup=_markup(*rows))


def _prompt(chat_id: int, key: str, text: str, context: int | None = None) -> None:
    global _pending_input
    _pending_input = (key, context)
    _bot().send_message(chat_id, text)


def _load_lots(chat_id: int) -> None:
    global _lot_cache
    profile = _cardinal.account.get_user(_cardinal.account.id)
    lots = list(profile.get_lots())
    existing_ids = {str(rule["lot_id"]) for rule in _sync(_rules())}
    available = [lot for lot in lots if str(lot.id) not in existing_ids]
    if not available:
        _bot().send_message(
            chat_id,
            "❌ Нет свободных лотов: все найденные лоты уже привязаны либо профиль пуст.",
        )
        return
    _lot_cache = {
        str(index): (
            str(lot.id),
            str(getattr(lot, "description", None) or f"Лот {lot.id}"),
        )
        for index, lot in enumerate(available[:40], start=1)
    }
    rows = [
        [
            (
                f"{title[:45]} · ID {lot_id}",
                f"{CALLBACK_PREFIX}lot:{key}",
            )
        ]
        for key, (lot_id, title) in _lot_cache.items()
    ]
    rows.append([("⬅️ Настройки", SETTINGS_CALLBACK)])
    _bot().send_message(
        chat_id,
        "🛒 <b>Выберите лот</b>\n\nПосле выбора укажите ID услуги и количество на одну купленную единицу.",
        reply_markup=_markup(*rows),
    )


async def _api_test(chat_id: int) -> None:
    try:
        settings = await _settings()
        result = await asyncio.to_thread(_smm_request, settings, action="balance")
        text = (
            "✅ API отвечает. Баланс: "
            f"<b>{html.escape(str(result.get('balance', '—')))} "
            f"{html.escape(str(result.get('currency', '')))}</b>"
        )
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
            _prompt(chat_id, "base", "Отправьте HTTPS Base URL API, например <code>https://smmway.ru/api/v2</code>.")
        elif data == f"{CALLBACK_PREFIX}set:token":
            _prompt(chat_id, "token", "Отправьте API-токен. Сообщение будет удалено, токен сохранится зашифрованным.")
        elif data == f"{CALLBACK_PREFIX}lots":
            _load_lots(chat_id)
        elif data == f"{CALLBACK_PREFIX}rules":
            _show_rules(chat_id)
        elif data.startswith(f"{CALLBACK_PREFIX}lot:"):
            selected = _lot_cache.get(data.rsplit(":", 1)[1])
            if not selected:
                raise RuntimeError("список лотов устарел; откройте его повторно")
            _draft_rule.clear()
            _draft_rule.update(lot_id=selected[0], lot_title=selected[1])
            _prompt(chat_id, "new_service", "Отправьте числовой ID SMM-услуги для выбранного лота.")
        elif data.startswith(f"{CALLBACK_PREFIX}r:"):
            _show_rule(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith(f"{CALLBACK_PREFIX}rs:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_service", "Отправьте новый ID SMM-услуги.", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rq:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_quantity", "Отправьте новое количество на одну купленную единицу.", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rt:"):
            rule_id = int(data.rsplit(":", 1)[1])
            rule = _sync(_rule(rule_id))
            if not rule:
                raise RuntimeError("привязка не найдена")
            _sync(_update_rule(rule_id, "enabled", not bool(rule["enabled"])))
            _show_rule(chat_id, rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rd:"):
            rule_id = int(data.rsplit(":", 1)[1])
            rule = _sync(_rule(rule_id))
            if not rule:
                raise RuntimeError("привязка не найдена")
            _bot().send_message(
                chat_id,
                f"Удалить привязку <b>{html.escape(str(rule['lot_title']))}</b>? Текущие SMM-заказы сохранятся.",
                reply_markup=_markup(
                    [("Да, удалить", f"{CALLBACK_PREFIX}rx:{rule_id}")],
                    [("Отмена", f"{CALLBACK_PREFIX}r:{rule_id}")],
                ),
            )
        elif data.startswith(f"{CALLBACK_PREFIX}rx:"):
            _sync(_delete_rule(int(data.rsplit(":", 1)[1])))
            _bot().send_message(chat_id, "✅ Привязка удалена.")
            _show_rules(chat_id)
        elif data == f"{CALLBACK_PREFIX}api":
            _spawn(_api_test(chat_id))
    except Exception as exc:
        logger.exception("Ошибка callback AutoSmm")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


def _positive_number(value: str, *, maximum: int = 1_000_000_000) -> int:
    if not value.isdigit():
        raise ValueError("нужно отправить целое положительное число")
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f"значение должно быть от 1 до {maximum:,}".replace(",", " "))
    return number


def _on_setting_message(message: Any) -> None:
    global _pending_input
    if _pending_input is None:
        return
    key, context = _pending_input
    _pending_input = None
    value = str(message.text or "").strip()
    chat_id = int(message.chat.id)
    try:
        if key == "token":
            try:
                _bot().delete_message(chat_id, message.message_id)
            except Exception:
                logger.warning("Не удалось удалить сообщение с API-токеном", exc_info=True)
        if key == "base":
            _sync(_set_setting("api_base_url", _validate_api_url(value)))
        elif key == "token":
            if not 8 <= len(value) <= 1024:
                raise ValueError("длина API-токена должна быть от 8 до 1024 символов")
            _sync(_set_setting("api_token_enc", _secret_box().encrypt(value)))
        elif key == "new_service":
            _draft_rule["service_id"] = _positive_number(value)
            _prompt(
                chat_id,
                "new_quantity",
                "Теперь отправьте количество для одного купленного товара. Например, 100: при покупке 3 шт. API получит 300.",
            )
            return
        elif key == "new_quantity":
            quantity = _positive_number(value)
            required = {"lot_id", "lot_title", "service_id"}
            if not required.issubset(_draft_rule):
                raise RuntimeError("мастер добавления устарел; выберите лот заново")
            _sync(
                _upsert_rule(
                    str(_draft_rule["lot_id"]),
                    str(_draft_rule["lot_title"]),
                    int(_draft_rule["service_id"]),
                    quantity,
                )
            )
            _draft_rule.clear()
        elif key == "edit_service" and context is not None:
            _sync(_update_rule(context, "service_id", _positive_number(value)))
        elif key == "edit_quantity" and context is not None:
            _sync(_update_rule(context, "quantity_per_unit", _positive_number(value)))
        _bot().send_message(chat_id, "✅ Настройка сохранена.")
        _show_settings(chat_id)
    except Exception as exc:
        logger.exception("Настройка AutoSmm не сохранена")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


def _match_rule(description: str, rules: list[Any]) -> Any | None:
    haystack = description.casefold()
    enabled = [rule for rule in rules if rule["enabled"]]
    enabled.sort(key=lambda rule: len(str(rule["lot_title"])), reverse=True)
    return next(
        (
            rule
            for rule in enabled
            if str(rule["lot_title"]).strip().casefold() in haystack
        ),
        None,
    )


async def _insert_job(order: dict[str, Any], rule: Any) -> Any | None:
    units = max(1, int(order.get("amount") or 1))
    total = _calculate_total_quantity(int(rule["quantity_per_unit"]), units)
    return await _db().fetchrow(
        """
        INSERT INTO autosmm_jobs
            (telegram_id, order_id, chat_id, chat_name, buyer_id, rule_id,
             lot_title, service_id, quantity_per_unit, purchased_units,
             total_quantity)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (telegram_id, order_id) DO NOTHING
        RETURNING *
        """,
        _telegram_id(),
        order["id"],
        str(order["chat_id"]),
        order["chat_name"],
        order["buyer_id"],
        rule["id"],
        rule["lot_title"],
        int(rule["service_id"]),
        int(rule["quantity_per_unit"]),
        units,
        total,
    )


async def _job(job_id: int) -> Any | None:
    return await _db().fetchrow(
        "SELECT * FROM autosmm_jobs WHERE telegram_id=$1 AND id=$2",
        _telegram_id(),
        job_id,
    )


async def _update_job(job_id: int, **values: Any) -> None:
    allowed = {
        "chat_id",
        "target_url",
        "smm_order_id",
        "smm_status",
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
        UPDATE autosmm_jobs
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


async def _notify_owner(text: str) -> None:
    await asyncio.to_thread(_cardinal.telegram.send_notification, text)


async def _active_buyer_job(
    chat_id: str,
    buyer_id: int | None,
    chat_name: str | None,
    order_chat_id: str | None,
) -> Any | None:
    normalized_name = (chat_name or "").strip() or None
    job = await _db().fetchrow(
        """
        SELECT * FROM autosmm_jobs
         WHERE telegram_id=$1
           AND (
               chat_id=$2
               OR ($3::BIGINT IS NOT NULL AND buyer_id=$3)
               OR ($4::TEXT IS NOT NULL AND LOWER(chat_name)=LOWER($4))
               OR ($5::TEXT IS NOT NULL AND chat_id=$5)
           )
           AND status IN ('awaiting_link', 'link_confirmation')
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


async def _submit_job(job_id: int) -> None:
    if job_id in _submitting_job_ids:
        return
    _submitting_job_ids.add(job_id)
    job: Any | None = None
    try:
        job = await _job(job_id)
        if not job or job["status"] != "link_confirmation":
            return
        settings = await _settings()
        result = await asyncio.to_thread(
            _smm_request,
            settings,
            action="add",
            service=int(job["service_id"]),
            link=str(job["target_url"]),
            quantity=int(job["total_quantity"]),
        )
        smm_order_id = result.get("order")
        if not smm_order_id:
            raise RuntimeError("SMM API не вернул ID заказа")
        await _update_job(
            job_id,
            smm_order_id=str(smm_order_id),
            smm_status="Pending",
            status="submitted",
            error_text=None,
        )
        job = await _job(job_id)
        await _funpay_send(
            job,
            "🚀 Накрутка начата.\n"
            f"Ссылка: {job['target_url']}\n"
            f"Количество: {job['total_quantity']}\n"
            "Статус будет проверяться автоматически.",
        )
        await _notify_owner(
            "🚀 <b>AutoSmm запустил заказ</b>\n\n"
            f"FunPay: <code>#{html.escape(str(job['order_id']))}</code>\n"
            f"SMM: <code>{html.escape(str(job['smm_order_id']))}</code>\n"
            f"Количество: <b>{job['total_quantity']}</b>"
        )
        _spawn(_monitor_job(job_id))
    except Exception as exc:
        logger.exception("AutoSmm не создал SMM-заказ %s", job_id)
        await _update_job(job_id, status="failed", error_text=str(exc)[:1000])
        job = await _job(job_id)
        if job:
            await _funpay_send(
                job,
                "❌ Не удалось запустить накрутку. Продавец уже уведомлён и проверяет заказ.",
            )
            await _notify_owner(
                "❌ <b>AutoSmm: ошибка создания заказа</b>\n\n"
                f"FunPay: <code>#{html.escape(str(job['order_id']))}</code>\n"
                f"Ошибка: <code>{html.escape(str(exc)[:500])}</code>"
            )
    finally:
        _submitting_job_ids.discard(job_id)


async def _monitor_job(job_id: int) -> None:
    if job_id in _running_job_ids:
        return
    _running_job_ids.add(job_id)
    try:
        while True:
            job = await _job(job_id)
            if not job or job["status"] != "submitted":
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
                    await _update_job(
                        job_id, status="canceled", error_text=order_status
                    )
                    await _notify_owner(
                        f"⚠️ AutoSmm-задание <code>#{html.escape(str(job['order_id']))}</code> остановлено: FunPay {order_status}."
                    )
                    return
            except Exception:
                logger.warning("Не проверен FunPay-заказ %s", job["order_id"], exc_info=True)
            try:
                settings = await _settings()
                result = await asyncio.to_thread(
                    _smm_request,
                    settings,
                    action="status",
                    order=job["smm_order_id"],
                )
                status = str(result.get("status", job["smm_status"] or "Unknown"))
                await _update_job(job_id, smm_status=status)
                normalized = status.strip().casefold()
                if normalized in {"completed", "complete", "done"}:
                    await _update_job(job_id, status="completed", error_text=None)
                    job = await _job(job_id)
                    await _funpay_send(
                        job,
                        "✅ Накрутка завершена. Проверьте результат по ссылке:\n"
                        f"{job['target_url']}",
                    )
                    await _notify_owner(
                        f"✅ AutoSmm-заказ <code>#{html.escape(str(job['order_id']))}</code> выполнен."
                    )
                    return
                if normalized in {
                    "partial",
                    "canceled",
                    "cancelled",
                    "error",
                    "failed",
                    "refunded",
                }:
                    await _update_job(
                        job_id,
                        status="failed",
                        error_text=f"SMM status: {status}",
                    )
                    await _notify_owner(
                        f"❌ AutoSmm-заказ <code>#{html.escape(str(job['order_id']))}</code> завершился со статусом <b>{html.escape(status)}</b>."
                    )
                    return
            except Exception:
                logger.warning("Не проверен SMM-заказ %s", job["smm_order_id"], exc_info=True)
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Ошибка мониторинга AutoSmm job %s", job_id)
    finally:
        _running_job_ids.discard(job_id)


async def _process_new_order(order: dict[str, Any]) -> None:
    rules = await _rules()
    rule = _match_rule(str(order.get("description") or ""), rules)
    if not rule:
        return
    settings = await _settings()
    if not settings["api_token_enc"] or not settings["api_base_url"]:
        await _notify_owner(
            "❌ Получен заказ для AutoSmm, но общие настройки API не заполнены. "
            f"Заказ: <code>#{html.escape(str(order['id']))}</code>"
        )
        return
    job = await _insert_job(order, rule)
    if not job:
        return
    await _funpay_send(
        job,
        "🔗 Отправьте ссылку для накрутки одним сообщением.\n\n"
        f"Будет заказано: {job['total_quantity']} ("
        f"{job['quantity_per_unit']} × {job['purchased_units']} шт. товара).",
    )


async def _process_funpay_message(message: dict[str, Any]) -> None:
    job = await _active_buyer_job(
        message["chat_id"],
        message.get("buyer_id"),
        message.get("chat_name"),
        message.get("order_chat_id"),
    )
    if not job:
        return
    text = str(message.get("text") or "").strip()
    lowered = text.casefold()
    if lowered == "#изменить":
        await _update_job(job["id"], status="awaiting_link", target_url=None)
        await _funpay_send(job, "Отправьте новую ссылку одним сообщением.")
        return
    if lowered == "#да" and job["status"] == "link_confirmation":
        await _submit_job(int(job["id"]))
        return
    if job["status"] not in {"awaiting_link", "link_confirmation"}:
        return
    try:
        target_url = _validate_target_url(text)
    except ValueError as exc:
        await _funpay_send(
            job,
            f"❌ {exc}. Отправьте ссылку ещё раз.",
        )
        return
    await _update_job(
        job["id"], target_url=target_url, status="link_confirmation"
    )
    await _funpay_send(
        job,
        f"Вы указали ссылку:\n{target_url}\n\n"
        "Всё верно? Отправьте #да для запуска или #изменить, чтобы указать другую ссылку.",
    )


async def _resume_jobs() -> None:
    await _ensure_schema()
    rows = await _db().fetch(
        """
        SELECT id FROM autosmm_jobs
         WHERE telegram_id=$1 AND status='submitted'
         ORDER BY created_at
        """,
        _telegram_id(),
    )
    for row in rows:
        _spawn(_monitor_job(int(row["id"])))


async def _process_order_status(order_id: str, status: str) -> None:
    if status not in {"REFUNDED", "UNPAID"}:
        return
    job = await _db().fetchrow(
        """
        SELECT * FROM autosmm_jobs
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
        f"⚠️ AutoSmm-задание <code>#{html.escape(order_id)}</code> отменено из-за статуса FunPay {status}."
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


def post_start(cardinal: Any) -> None:
    global _cardinal
    _cardinal = cardinal
    _spawn(_resume_jobs())


def new_order(cardinal: Any, event: Any) -> None:
    order = event.order
    status = getattr(getattr(order, "status", None), "name", "")
    if status and status != "PAID":
        return
    _spawn(
        _process_new_order(
            {
                "id": str(order.id),
                "chat_id": str(order.chat_id),
                "chat_name": str(order.buyer_username or "Покупатель"),
                "buyer_id": int(order.buyer_id)
                if getattr(order, "buyer_id", None)
                else None,
                "description": str(order.description or ""),
                "amount": max(1, int(getattr(order, "amount", None) or 1)),
            }
        )
    )


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
    global _pending_input
    _pending_input = None
    for future in list(_futures):
        future.cancel()


def on_delete(cardinal: Any, callback: Any) -> None:
    pre_stop(cardinal)
    _sync(
        _db().execute(
            "DELETE FROM autosmm_jobs WHERE telegram_id=$1", _telegram_id()
        )
    )
    _sync(
        _db().execute(
            "DELETE FROM autosmm_lot_rules WHERE telegram_id=$1", _telegram_id()
        )
    )
    _sync(
        _db().execute(
            "DELETE FROM autosmm_settings WHERE telegram_id=$1", _telegram_id()
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
BIND_TO_NEW_MESSAGE = [new_message]
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
