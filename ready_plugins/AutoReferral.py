from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
from concurrent.futures import CancelledError, Future
from typing import Any
from urllib.parse import parse_qs, urlparse

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

try:
    import anthropic
except ImportError:
    anthropic = None

NAME = "АвтоРеферал"
VERSION = "1.1.0"
DESCRIPTION = "AI-автоматизация реферальных Telegram-заданий для выбранных FunPay-лотов"
CREDITS = "FunPay aiogram bot"
SETTINGS_PAGE = True
TELETHON = True
UUID = "a7f3e9d2-1b8c-4e5f-9a2d-3c6b7e8f9a1b"

CALLBACK_PREFIX = "ar:"
SETTINGS_CALLBACK = f"47:{UUID}:0"
DEFAULT_API_URL = "https://api.anthropic.com"
MAX_STEPS = 20
MAX_AI_TEXT = 1800

logger = logging.getLogger("fpc_plugin.auto_referral")
_cardinal: Any | None = None
_pending_input: tuple[str, int | None] | None = None
_lot_cache: dict[str, tuple[str, str]] = {}
_draft_lot: dict[str, str] = {}
_futures: set[Future[Any]] = set()
_tasks: set[asyncio.Task[Any]] = set()

BASE_PROMPT = """Ты управляешь Telegram-ботом строго по инструкции покупателя.
Тебе каждый шаг передают: инструкцию, текст последнего сообщения бота, список кнопок и результаты предыдущих действий.
Отвечай ровно ОДНОЙ командой:
CLICK_BUTTON:<номер> — нажать обычную inline-кнопку по номеру из списка.
TYPE_TEXT:<текст> — отправить текст боту.
WAIT — подождать следующее сообщение.
TASK_COMPLETED — цель инструкции полностью достигнута.
CAPTCHA_DETECTED — если видна CAPTCHA/проверка \"я не робот\", код с картинки или иная антибот-проверка. CAPTCHA нельзя обходить и нельзя нажимать кнопки для её обхода.
MINI_APP_UNSUPPORTED — если следующий шаг требует WebView, mini app, открытия сайта/приложения или URL-кнопки.
ERROR:<причина> — если продолжить невозможно.
Никогда не выдумывай кнопки. Не объявляй TASK_COMPLETED без подтверждающего результата от Telegram-бота."""


def _markup(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for row in rows:
        markup.row(*[InlineKeyboardButton(text=t, callback_data=c) for t, c in row])
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
    service = getattr(getattr(_cardinal, "plugin_manager", None), "telethon_service", None)
    if service is None:
        raise RuntimeError("хранилище секретов недоступно")
    return service.secrets


def _sync(awaitable: Any, timeout: float = 60) -> Any:
    return asyncio.run_coroutine_threadsafe(awaitable, _cardinal.telegram.loop).result(timeout=timeout)


def _spawn(awaitable: Any) -> Future[Any]:
    future = asyncio.run_coroutine_threadsafe(awaitable, _cardinal.telegram.loop)
    _futures.add(future)

    def done(f: Future[Any]) -> None:
        _futures.discard(f)
        if f.cancelled():
            return
        try:
            f.result()
        except (asyncio.CancelledError, CancelledError):
            pass
        except Exception:
            logger.exception("Фоновая задача АвтоРеферал завершилась с ошибкой")

    future.add_done_callback(done)
    return future


async def _ensure_schema() -> None:
    await _db().execute("""
        CREATE TABLE IF NOT EXISTS auto_referral_settings (
            telegram_id BIGINT PRIMARY KEY REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            api_base_url TEXT NOT NULL DEFAULT 'https://api.anthropic.com',
            api_token_enc TEXT,
            model_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS auto_referral_lots (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            lot_id TEXT NOT NULL,
            lot_title TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, lot_id)
        );
        CREATE TABLE IF NOT EXISTS auto_referral_sessions (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            order_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            buyer_id BIGINT,
            rule_id BIGINT NOT NULL REFERENCES auto_referral_lots(id) ON DELETE CASCADE,
            stage TEXT NOT NULL DEFAULT 'awaiting_referral',
            referral_link TEXT,
            bot_username TEXT,
            instruction TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            telethon_session_id BIGINT,
            action_log TEXT NOT NULL DEFAULT '',
            error_text TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, order_id)
        );
        CREATE INDEX IF NOT EXISTS auto_referral_pending_idx
            ON auto_referral_sessions (telegram_id, buyer_id, status, updated_at DESC);
    """)
    await _db().execute(
        "INSERT INTO auto_referral_settings (telegram_id) VALUES ($1) ON CONFLICT DO NOTHING",
        _telegram_id(),
    )


async def _settings() -> Any:
    await _ensure_schema()
    return await _db().fetchrow("SELECT * FROM auto_referral_settings WHERE telegram_id=$1", _telegram_id())


async def _set_setting(column: str, value: Any) -> None:
    if column not in {"api_base_url", "api_token_enc", "model_id"}:
        raise ValueError("неизвестная настройка")
    await _db().execute(
        f"UPDATE auto_referral_settings SET {column}=$2, updated_at=NOW() WHERE telegram_id=$1",
        _telegram_id(), value,
    )


async def _lots() -> list[Any]:
    await _ensure_schema()
    return list(await _db().fetch(
        "SELECT * FROM auto_referral_lots WHERE telegram_id=$1 ORDER BY created_at, id",
        _telegram_id(),
    ))


async def _lot(row_id: int) -> Any | None:
    return await _db().fetchrow(
        "SELECT * FROM auto_referral_lots WHERE telegram_id=$1 AND id=$2",
        _telegram_id(), row_id,
    )


async def _lot_by_funpay_id(lot_id: str) -> Any | None:
    return await _db().fetchrow(
        "SELECT * FROM auto_referral_lots WHERE telegram_id=$1 AND lot_id=$2 AND enabled=TRUE",
        _telegram_id(), str(lot_id),
    )


async def _save_lot(lot_id: str, title: str, prompt: str) -> None:
    await _db().execute("""
        INSERT INTO auto_referral_lots (telegram_id, lot_id, lot_title, system_prompt)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (telegram_id, lot_id) DO UPDATE SET
            lot_title=EXCLUDED.lot_title, system_prompt=EXCLUDED.system_prompt,
            enabled=TRUE, updated_at=NOW()
    """, _telegram_id(), lot_id, title, prompt)


async def _update_lot(row_id: int, column: str, value: Any) -> None:
    if column not in {"system_prompt", "enabled"}:
        raise ValueError("неизвестное поле лота")
    await _db().execute(
        f"UPDATE auto_referral_lots SET {column}=$3, updated_at=NOW() WHERE telegram_id=$1 AND id=$2",
        _telegram_id(), row_id, value,
    )


def _api_token(settings: Any) -> str:
    encrypted = settings["api_token_enc"] if settings else None
    return _secret_box().decrypt(encrypted) if encrypted else ""


def _token_label(settings: Any) -> str:
    try:
        token = _api_token(settings)
    except Exception:
        return "ошибка расшифровки"
    return "не задан" if not token else "••••" + token[-4:]


def _ready(settings: Any) -> bool:
    return bool(settings and settings["api_base_url"] and settings["api_token_enc"] and settings["model_id"])


def _accounts() -> list[tuple[int, Any]]:
    service = getattr(getattr(_cardinal, "plugin_manager", None), "telethon_service", None)
    if service is None:
        return []
    result: list[tuple[int, Any]] = []
    for client in service.get_clients(_telegram_id(), UUID):
        if not client.is_connected():
            continue
        sid = service.session_id_for_client(_telegram_id(), UUID, client)
        if sid is not None:
            result.append((int(sid), client))
    return result


def _show_settings(chat_id: int) -> None:
    settings = _sync(_settings())
    lots = _sync(_lots())
    account_count = len(_accounts())
    enabled = sum(1 for x in lots if x["enabled"])
    text = (
        "🤖 <b>АвтоРеферал</b>\n\n"
        f"Base URL: <code>{html.escape(str(settings['api_base_url']))}</code>\n"
        f"API-токен: <b>{html.escape(_token_label(settings))}</b>\n"
        f"Модель: <code>{html.escape(str(settings['model_id'] or 'не задана'))}</code>\n"
        f"Telegram-аккаунты: <b>{account_count}</b>\n"
        f"Лоты: <b>{enabled}/{len(lots)}</b>\n\n"
        f"Готовность: <b>{'✅ настроено' if _ready(settings) and account_count and enabled else '⚠️ требуется настройка'}</b>"
    )
    rows = [
        [("🌐 Base URL", f"{CALLBACK_PREFIX}set:base")],
        [("🔑 API-токен", f"{CALLBACK_PREFIX}set:token")],
        [("🧠 ID модели", f"{CALLBACK_PREFIX}set:model")],
        [("📱 Telegram-аккаунты", f"plugin_telethon:{UUID}")],
        [("➕ Добавить лот", f"{CALLBACK_PREFIX}lots")],
        [("🧩 Управление лотами", f"{CALLBACK_PREFIX}rules")],
        [("🧪 Проверить AI", f"{CALLBACK_PREFIX}test")],
        [("🔄 Обновить", SETTINGS_CALLBACK)],
    ]
    _bot().send_message(chat_id, text, reply_markup=_markup(*rows))


def _prompt(chat_id: int, key: str, text: str, context: int | None = None) -> None:
    global _pending_input
    _pending_input = (key, context)
    _bot().send_message(chat_id, text)


def _load_lots(chat_id: int) -> None:
    global _lot_cache
    profile = _cardinal.account.get_user(_cardinal.account.id)
    existing = {str(x["lot_id"]) for x in _sync(_lots())}
    available = [x for x in profile.get_lots() if str(x.id) not in existing]
    _lot_cache = {
        str(i): (str(x.id), str(getattr(x, "description", None) or f"Лот {x.id}"))
        for i, x in enumerate(available[:40], 1)
    }
    if not _lot_cache:
        _bot().send_message(chat_id, "❌ Нет свободных лотов.")
        return
    rows = [[(f"{title[:45]} · ID {lid}", f"{CALLBACK_PREFIX}add:{key}")]
            for key, (lid, title) in _lot_cache.items()]
    rows.append([("⬅️ Настройки", SETTINGS_CALLBACK)])
    _bot().send_message(chat_id, "🛒 Выберите лот. После выбора отправьте его системный промпт.", reply_markup=_markup(*rows))


def _show_rules(chat_id: int) -> None:
    lots = _sync(_lots())
    rows = [[(f"{'✅' if x['enabled'] else '⏸'} {str(x['lot_title'])[:42]}", f"{CALLBACK_PREFIX}r:{x['id']}")]
            for x in lots]
    rows += [[("➕ Добавить лот", f"{CALLBACK_PREFIX}lots")], [("⬅️ Настройки", SETTINGS_CALLBACK)]]
    _bot().send_message(chat_id, "🧩 <b>Лоты АвтоРеферал</b>", reply_markup=_markup(*rows))


def _show_rule(chat_id: int, row_id: int) -> None:
    rule = _sync(_lot(row_id))
    if not rule:
        raise RuntimeError("лот не найден")
    prompt = html.escape(str(rule["system_prompt"])[:1200])
    text = (
        f"🛒 <b>{html.escape(str(rule['lot_title']))}</b>\n"
        f"ID: <code>{rule['lot_id']}</code>\n"
        f"Статус: <b>{'включён' if rule['enabled'] else 'выключен'}</b>\n\n"
        f"<b>Системный промпт</b>\n<blockquote>{prompt}</blockquote>"
    )
    rows = [
        [("✏️ Изменить промпт", f"{CALLBACK_PREFIX}rp:{row_id}")],
        [("⏸ Выключить" if rule["enabled"] else "▶️ Включить", f"{CALLBACK_PREFIX}rt:{row_id}")],
        [("⬅️ Лоты", f"{CALLBACK_PREFIX}rules")],
    ]
    _bot().send_message(chat_id, text, reply_markup=_markup(*rows))


def _validate_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("нужен HTTPS URL без логина/пароля")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL не должен содержать query/fragment")
    return value


def _validate_model(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{2,200}", value):
        raise ValueError("некорректный ID модели")
    return value


def _validate_prompt(value: str) -> str:
    value = value.strip()
    if not 20 <= len(value) <= 8000:
        raise ValueError("промпт должен быть от 20 до 8000 символов")
    return value


def _ai(settings: Any, system: str, user_text: str, image: bytes | None = None) -> str:
    if anthropic is None:
        raise RuntimeError("пакет anthropic не установлен")
    token = _api_token(settings)
    if not token:
        raise RuntimeError("API-токен не задан")
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image:
        media_type = "image/png" if image.startswith(b"\x89PNG") else "image/jpeg"
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image).decode("ascii")},
        })
    client = anthropic.Anthropic(
        api_key=token, base_url=str(settings["api_base_url"]), timeout=45, max_retries=1,
    )
    try:
        response = client.messages.create(
            model=str(settings["model_id"]), max_tokens=350, system=system,
            messages=[{"role": "user", "content": content}],
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    text = "\n".join(str(getattr(x, "text", "")).strip() for x in response.content
                     if getattr(x, "type", None) == "text").strip()
    if not text:
        raise RuntimeError("AI вернул пустой ответ")
    return text[:MAX_AI_TEXT]


async def _api_test(chat_id: int) -> None:
    try:
        settings = await _settings()
        if not _ready(settings):
            raise RuntimeError("заполните Base URL, API-токен и ID модели")
        result = await asyncio.to_thread(_ai, settings, "Ответь только OK", "Проверка")
        text = f"✅ AI отвечает: <code>{html.escape(result[:100])}</code>"
    except Exception as exc:
        text = f"❌ AI API: <code>{html.escape(str(exc)[:500])}</code>"
    await asyncio.to_thread(_bot().send_message, chat_id, text)


def _on_callback(call: Any) -> None:
    data = str(call.data or "")
    chat_id = int(call.message.chat.id)
    try:
        _bot().answer_callback_query(call.id)
        if data in {SETTINGS_CALLBACK, f"{CALLBACK_PREFIX}open"}:
            _show_settings(chat_id)
        elif data == f"{CALLBACK_PREFIX}set:base":
            _prompt(chat_id, "base", "Отправьте HTTPS Base URL AI API.")
        elif data == f"{CALLBACK_PREFIX}set:token":
            _prompt(chat_id, "token", "Отправьте API-токен. Сообщение будет удалено.")
        elif data == f"{CALLBACK_PREFIX}set:model":
            _prompt(chat_id, "model", "Отправьте ID модели.")
        elif data == f"{CALLBACK_PREFIX}lots":
            _load_lots(chat_id)
        elif data == f"{CALLBACK_PREFIX}rules":
            _show_rules(chat_id)
        elif data.startswith(f"{CALLBACK_PREFIX}add:"):
            selected = _lot_cache.get(data.rsplit(":", 1)[1])
            if not selected:
                raise RuntimeError("список лотов устарел")
            _draft_lot.clear()
            _draft_lot.update(lot_id=selected[0], title=selected[1])
            _prompt(chat_id, "new_prompt", "Отправьте системный промпт этого лота.")
        elif data.startswith(f"{CALLBACK_PREFIX}r:"):
            _show_rule(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith(f"{CALLBACK_PREFIX}rp:"):
            _prompt(chat_id, "edit_prompt", "Отправьте новый системный промпт.", int(data.rsplit(":", 1)[1]))
        elif data.startswith(f"{CALLBACK_PREFIX}rt:"):
            rid = int(data.rsplit(":", 1)[1])
            rule = _sync(_lot(rid))
            _sync(_update_lot(rid, "enabled", not bool(rule["enabled"])))
            _show_rule(chat_id, rid)
        elif data == f"{CALLBACK_PREFIX}test":
            _spawn(_api_test(chat_id))
    except Exception as exc:
        logger.exception("Ошибка callback АвтоРеферал")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


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
                pass
        if key == "base":
            _sync(_set_setting("api_base_url", _validate_url(value)))
        elif key == "token":
            if not 8 <= len(value) <= 1024:
                raise ValueError("длина токена должна быть 8–1024")
            _sync(_set_setting("api_token_enc", _secret_box().encrypt(value)))
        elif key == "model":
            _sync(_set_setting("model_id", _validate_model(value)))
        elif key == "new_prompt":
            if not {"lot_id", "title"}.issubset(_draft_lot):
                raise RuntimeError("мастер добавления устарел")
            _sync(_save_lot(_draft_lot["lot_id"], _draft_lot["title"], _validate_prompt(value)))
            _draft_lot.clear()
        elif key == "edit_prompt" and context is not None:
            _sync(_update_lot(context, "system_prompt", _validate_prompt(value)))
        _bot().send_message(chat_id, "✅ Настройка сохранена.")
        _show_settings(chat_id)
    except Exception as exc:
        logger.exception("Настройка АвтоРеферал не сохранена")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


def _parse_ref(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("@"):
        return value[1:], ""
    parsed = urlparse(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        raise ValueError("нужна ссылка t.me/бот или @username")
    username = parsed.path.strip("/").split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("не удалось извлечь username бота")
    query = parse_qs(parsed.query)
    start = (query.get("start") or [""])[0]
    return username, start


async def _bot_was_used(client: Any, username: str) -> bool:
    try:
        messages = await client.get_messages(username, limit=1)
        return bool(messages)
    except Exception:
        return False


async def _check_history(username: str) -> bool:
    for _sid, client in _accounts():
        if await _bot_was_used(client, username):
            return True
    return False


async def _funpay_send(chat_id: str, chat_name: str | None, text: str) -> None:
    await asyncio.to_thread(_cardinal.account.send_message, chat_id, text, chat_name)


async def _pending_session(chat_id: str, buyer_id: int | None, canonical: str | None) -> Any | None:
    return await _db().fetchrow("""
        SELECT * FROM auto_referral_sessions
         WHERE telegram_id=$1 AND status='pending'
           AND (chat_id=$2 OR ($3::TEXT IS NOT NULL AND chat_id=$3)
                OR ($4::BIGINT IS NOT NULL AND buyer_id=$4))
         ORDER BY updated_at DESC LIMIT 1
    """, _telegram_id(), chat_id, canonical, buyer_id)


async def _append_log(session_id: int, line: str) -> None:
    await _db().execute("""
        UPDATE auto_referral_sessions
           SET action_log=RIGHT(COALESCE(action_log,'') || $3 || E'\\n', 12000), updated_at=NOW()
         WHERE telegram_id=$1 AND id=$2
    """, _telegram_id(), session_id, line[:1500])


async def _update_session(session_id: int, **values: Any) -> None:
    allowed = {"stage", "referral_link", "bot_username", "instruction", "status",
               "telethon_session_id", "error_text"}
    values = {k: v for k, v in values.items() if k in allowed}
    if not values:
        return
    assignments = ", ".join(f"{k}=${i+3}" for i, k in enumerate(values))
    await _db().execute(
        f"UPDATE auto_referral_sessions SET {assignments}, updated_at=NOW() WHERE telegram_id=$1 AND id=$2",
        _telegram_id(), session_id, *values.values(),
    )


async def _refund_and_stop(session: Any, reason: str) -> None:
    try:
        await asyncio.to_thread(_cardinal.account.refund, str(session["order_id"]))
        refund_text = " Возврат оформлен."
    except Exception:
        logger.exception("Не удалось оформить возврат %s", session["order_id"])
        refund_text = " Автовозврат не прошёл; продавец уведомлён."
    await _update_session(int(session["id"]), status="failed", error_text=reason[:900])
    await _funpay_send(str(session["chat_id"]), session["chat_name"], f"⚠️ {reason}.{refund_text}")
    await asyncio.to_thread(
        _cardinal.telegram.send_notification,
        f"⚠️ <b>АвтоРеферал</b> · заказ <code>#{html.escape(str(session['order_id']))}</code>\n{html.escape(reason)}",
    )


async def _snapshot(client: Any, username: str) -> tuple[Any | None, str, list[dict[str, Any]], bytes | None]:
    messages = await client.get_messages(username, limit=1)
    if not messages:
        return None, "", [], None
    message = messages[0]
    text = str(getattr(message, "message", "") or "")
    buttons: list[dict[str, Any]] = []
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            buttons.append({
                "text": str(getattr(button, "text", "") or ""),
                "url": str(getattr(button, "url", "") or ""),
            })
    image = None
    if getattr(message, "photo", None):
        try:
            image = await client.download_media(message, file=bytes)
        except Exception:
            logger.debug("Не удалось скачать изображение бота", exc_info=True)
    return message, text, buttons, image


def _snapshot_text(instruction: str, text: str, buttons: list[dict[str, Any]], log: str) -> str:
    button_lines = [
        f"{i}. {b['text']}" + (" [URL/WEBVIEW]" if b["url"] else "")
        for i, b in enumerate(buttons, 1)
    ]
    return (
        f"ИНСТРУКЦИЯ ПОКУПАТЕЛЯ:\n{instruction}\n\n"
        f"ПОСЛЕДНЕЕ СООБЩЕНИЕ TELEGRAM-БОТА:\n{text or '(без текста)'}\n\n"
        f"КНОПКИ:\n" + ("\n".join(button_lines) if button_lines else "(нет)") +
        f"\n\nРЕЗУЛЬТАТЫ ПРЕДЫДУЩИХ ШАГОВ:\n{log or '(пока нет)'}"
    )


async def _execute(order_id: str) -> None:
    session = await _db().fetchrow(
        "SELECT * FROM auto_referral_sessions WHERE telegram_id=$1 AND order_id=$2",
        _telegram_id(), order_id,
    )
    if not session:
        return
    settings = await _settings()
    rule = await _lot(int(session["rule_id"]))
    if not _ready(settings) or not rule:
        await _refund_and_stop(session, "Плагин настроен не полностью")
        return
    username, start_param = _parse_ref(str(session["referral_link"]))
    candidates = _accounts()
    if not candidates:
        await _refund_and_stop(session, "Нет подключённых Telegram-аккаунтов")
        return
    selected: tuple[int, Any] | None = None
    for sid, client in candidates:
        if not await _bot_was_used(client, username):
            selected = (sid, client)
            break
    if selected is None:
        await _refund_and_stop(session, "Все подключённые Telegram-аккаунты уже использовали этого бота")
        return
    sid, client = selected
    await _update_session(int(session["id"]), bot_username="@" + username, telethon_session_id=sid)
    entity = await client.get_entity(username)
    if start_param:
        from telethon.tl.functions.messages import StartBotRequest
        await client(StartBotRequest(entity, entity, start_param))
    else:
        await client.send_message(entity, "/start")
    await _append_log(int(session["id"]), f"START @{username} start={start_param or '-'}")

    fixed_prompt = BASE_PROMPT + "\n\nДополнительные правила продавца для этого лота:\n" + str(rule["system_prompt"])
    for _step in range(MAX_STEPS):
        await asyncio.sleep(2)
        session = await _db().fetchrow(
            "SELECT * FROM auto_referral_sessions WHERE telegram_id=$1 AND id=$2",
            _telegram_id(), int(session["id"]),
        )
        message, text, buttons, image = await _snapshot(client, username)
        log = str(session["action_log"] or "")
        prompt = _snapshot_text(str(session["instruction"] or ""), text, buttons, log)
        command = (await asyncio.to_thread(_ai, settings, fixed_prompt, prompt, image)).strip()
        await _append_log(int(session["id"]), f"AI: {command}")

        if command == "CAPTCHA_DETECTED":
            await _refund_and_stop(session, "Telegram-бот запросил CAPTCHA; автоматизация её не обходит")
            return
        if command == "MINI_APP_UNSUPPORTED":
            await _refund_and_stop(session, "Следующий шаг требует mini app/WebView/внешнюю ссылку")
            return
        if command == "TASK_COMPLETED":
            await _update_session(int(session["id"]), status="completed", stage="completed", error_text=None)
            await _funpay_send(str(session["chat_id"]), session["chat_name"], "✅ Задание выполнено.")
            return
        if command == "WAIT":
            continue
        if command.startswith("ERROR:"):
            await _refund_and_stop(session, command.split(":", 1)[1].strip() or "AI остановил выполнение")
            return
        if command.startswith("TYPE_TEXT:"):
            outgoing = command.split(":", 1)[1].strip()
            if not outgoing:
                await _refund_and_stop(session, "AI вернул пустой текст для отправки")
                return
            await client.send_message(entity, outgoing)
            await _append_log(int(session["id"]), f"SENT TEXT: {outgoing[:300]}")
            continue
        if command.startswith("CLICK_BUTTON:"):
            raw = command.split(":", 1)[1].strip()
            if not raw.isdigit():
                await _refund_and_stop(session, "AI вернул некорректный номер кнопки")
                return
            index = int(raw) - 1
            if not message or index < 0 or index >= len(buttons):
                await _refund_and_stop(session, "Запрошенной кнопки нет в сообщении")
                return
            if buttons[index]["url"]:
                await _refund_and_stop(session, "Выбранная кнопка открывает URL/WebView; mini app не поддерживается")
                return
            await message.click(index)
            await _append_log(int(session["id"]), f"CLICKED #{index + 1}: {buttons[index]['text'][:200]}")
            continue
        await _refund_and_stop(session, "AI вернул неизвестную команду")
        return
    await _refund_and_stop(session, "Превышено максимальное число шагов")


def _order_lot_id(order: Any) -> str | None:
    direct = getattr(order, "lot_id", None)
    if direct is not None:
        return str(direct)
    widget = str(getattr(order, "html", "") or "")
    match = re.search(r"(?:lots/offer\?id=|offer=|data-offer=[\"'])(\d+)", widget, re.I)
    return match.group(1) if match else None


async def _process_new_order(order: dict[str, Any]) -> None:
    rule = await _lot_by_funpay_id(str(order["lot_id"])) if order.get("lot_id") else None
    if not rule:
        description = str(order.get("description") or "").casefold()
        for candidate in await _lots():
            title = str(candidate["lot_title"] or "").strip().casefold()
            if candidate["enabled"] and title and title in description:
                rule = candidate
                break
    if not rule:
        return
    await _db().execute("""
        INSERT INTO auto_referral_sessions
            (telegram_id, order_id, chat_id, chat_name, buyer_id, rule_id)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (telegram_id, order_id) DO UPDATE SET
            chat_id=EXCLUDED.chat_id, chat_name=EXCLUDED.chat_name,
            buyer_id=EXCLUDED.buyer_id, rule_id=EXCLUDED.rule_id,
            stage='awaiting_referral', status='pending', referral_link=NULL,
            instruction=NULL, action_log='', error_text=NULL, updated_at=NOW()
    """, _telegram_id(), order["id"], order["chat_id"], order["chat_name"], order.get("buyer_id"), int(rule["id"]))
    await _funpay_send(order["chat_id"], order["chat_name"],
        "✅ Заказ получен. Отправьте одним сообщением вашу реферальную ссылку Telegram-бота. После неё я отдельно попрошу инструкцию.")


async def _process_inquiry(message: dict[str, Any]) -> None:
    if not message.get("viewing_lot_id"):
        return
    rule = await _lot_by_funpay_id(str(message["viewing_lot_id"]))
    if not rule:
        return
    text = str(message["text"] or "")
    if not any(x in text.casefold() for x in ("был ли", "был в боте", "заходил", "использовал")):
        return
    try:
        username, _ = _parse_ref(text)
    except Exception:
        match = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})", text, re.I)
        if not match:
            match = re.search(r"@([A-Za-z0-9_]{5,32})", text)
        if not match:
            return
        username = match.group(1)
    used = await _check_history(username)
    answer = "✅ Да, в подключённых аккаунтах уже есть история с этим ботом." if used else "❌ Нет, в подключённых аккаунтах истории с этим ботом не найдено."
    await _funpay_send(message["send_chat_id"], message["chat_name"], answer)


async def _process_buyer(message: dict[str, Any]) -> None:
    session = await _pending_session(message["chat_id"], message.get("buyer_id"), message.get("send_chat_id"))
    if not session:
        return
    text = str(message["text"] or "").strip()
    if not text:
        return
    if session["stage"] == "awaiting_referral":
        try:
            username, _ = _parse_ref(text)
        except Exception as exc:
            await _funpay_send(message["send_chat_id"], message["chat_name"], f"❌ {exc}")
            return
        await _update_session(int(session["id"]), stage="awaiting_instruction", referral_link=text, bot_username="@" + username)
        await _funpay_send(message["send_chat_id"], message["chat_name"], "✅ Ссылка принята. Теперь отправьте инструкцию, что именно нужно выполнить в боте.")
        return
    if session["stage"] == "awaiting_instruction":
        await _update_session(int(session["id"]), stage="executing", instruction=text)
        await _funpay_send(message["send_chat_id"], message["chat_name"], "🤖 Инструкция принята. Начинаю выполнение.")
        task = asyncio.create_task(_execute(str(session["order_id"])))
        _tasks.add(task)
        task.add_done_callback(lambda t: _tasks.discard(t))


def pre_init(cardinal: Any) -> None:
    global _cardinal
    _cardinal = cardinal
    _sync(_ensure_schema())
    bot = cardinal.telegram.bot
    bot.register_callback_query_handler(
        _on_callback,
        func=lambda call: str(call.data or "") == SETTINGS_CALLBACK or str(call.data or "").startswith(CALLBACK_PREFIX),
    )
    bot.register_message_handler(
        _on_setting_message, content_types=["text"], func=lambda _message: _pending_input is not None,
    )


def new_order(cardinal: Any, event: Any) -> None:
    order = event.order
    status = getattr(getattr(order, "status", None), "name", "")
    if status and status != "PAID":
        return
    _spawn(_process_new_order({
        "id": str(order.id),
        "chat_id": str(order.chat_id),
        "chat_name": str(order.buyer_username or "Покупатель"),
        "buyer_id": int(order.buyer_id) if getattr(order, "buyer_id", None) else None,
        "lot_id": _order_lot_id(order),
        "description": str(getattr(order, "description", "") or ""),
    }))


def new_message(cardinal: Any, event: Any) -> None:
    message = event.message
    if (getattr(message, "author_id", None) in {0, cardinal.account.id}
            or getattr(message, "by_bot", False) or getattr(message, "by_vertex", False)):
        return
    buyer_id = int(message.interlocutor_id) if getattr(message, "interlocutor_id", None) else None
    canonical = None
    if buyer_id:
        a, b = sorted((buyer_id, int(cardinal.account.id)))
        canonical = f"users-{a}-{b}"
    viewing = getattr(message, "buyer_viewing", None)
    viewing_lot_id = (
        str(viewing.lot_id) if viewing is not None and getattr(viewing, "is_viewing_lot", False)
        and getattr(viewing, "lot_id", None) else None
    )
    payload = {
        "chat_id": str(message.chat_id),
        "send_chat_id": canonical or str(message.chat_id),
        "chat_name": str(message.chat_name or ""),
        "buyer_id": buyer_id,
        "viewing_lot_id": viewing_lot_id,
        "text": str(message.text or ""),
    }
    _spawn(_process_inquiry(payload))
    _spawn(_process_buyer(payload))


def pre_stop(cardinal: Any) -> None:
    global _pending_input
    _pending_input = None
    for future in list(_futures):
        future.cancel()
    for task in list(_tasks):
        task.cancel()


def on_delete(cardinal: Any, callback: Any) -> None:
    pre_stop(cardinal)
    for table in ("auto_referral_sessions", "auto_referral_lots", "auto_referral_settings"):
        _sync(_db().execute(f"DELETE FROM {table} WHERE telegram_id=$1", _telegram_id()))


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
BIND_TO_ORDER_STATUS_CHANGED = []
BIND_TO_PRE_DELIVERY = []
BIND_TO_POST_DELIVERY = []
BIND_TO_PRE_LOTS_RAISE = []
BIND_TO_POST_LOTS_RAISE = []
BIND_TO_TELETHON_READY = []
BIND_TO_TELETHON_DISCONNECTED = []
BIND_TO_DELETE = on_delete
