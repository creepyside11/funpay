from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from concurrent.futures import CancelledError, Future
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from FunPayAPI.common.utils import MONTHS


NAME = "Emerald Promo"
VERSION = "1.0.3"
DESCRIPTION = "Автоматическая продажа, бесплатная выдача и бонусные промокоды EmeraldAI"
CREDITS = "FunPay aiogram bot"
SETTINGS_PAGE = True
TELETHON = False
UUID = "9ee0d7d1-2cef-45c5-b1ac-67c4c1f3ef8a"

CALLBACK_PREFIX = "emp:"
SETTINGS_CALLBACK = f"47:{UUID}:0"
DEFAULT_API_URL = "https://www.emeraldai.sbs/seller/v1"
LEGACY_API_URL = "https://emeraldai.sbs/seller/v1"
ACTIVATION_URL = "https://emeraldai.sbs"
MIN_TOKEN_AMOUNT = 10_000
MAX_TOKEN_AMOUNT = 1_000_000_000
DEFAULT_FREE_TOKENS = 200_000
DEFAULT_REVIEW_TOKENS = 1_000_000
DEFAULT_MIN_AGE_DAYS = 7

logger = logging.getLogger("fpc_plugin.emerald_promo")

_cardinal: Any | None = None
_pending_input: tuple[str, int | None] | None = None
_draft_rule: dict[str, Any] = {}
_lot_cache: dict[str, tuple[str, str]] = {}
_futures: set[Future[Any]] = set()
_running_issue_ids: set[int] = set()


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _markup(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for row in rows:
        markup.row(*[
            InlineKeyboardButton(text=text, callback_data=callback)
            for text, callback in row
        ])
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
        if _cardinal is not None else None
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
            logger.exception("Фоновая задача Emerald Promo завершилась с ошибкой")

    future.add_done_callback(done)
    return future


async def _ensure_schema() -> None:
    await _db().execute(
        """
        CREATE TABLE IF NOT EXISTS emerald_promo_settings (
            telegram_id BIGINT PRIMARY KEY
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            api_base_url TEXT NOT NULL DEFAULT 'https://www.emeraldai.sbs/seller/v1',
            api_token_enc TEXT,
            free_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            free_token_amount BIGINT NOT NULL DEFAULT 200000,
            min_account_age_days INTEGER NOT NULL DEFAULT 7,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS emerald_promo_lot_rules (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            lot_id TEXT NOT NULL,
            lot_title TEXT NOT NULL,
            tokens_per_unit BIGINT NOT NULL,
            review_bonus_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            review_bonus_tokens BIGINT NOT NULL DEFAULT 1000000,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, lot_id)
        );

        CREATE TABLE IF NOT EXISTS emerald_promo_issues (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            buyer_id BIGINT,
            order_id TEXT,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            rule_id BIGINT REFERENCES emerald_promo_lot_rules(id) ON DELETE SET NULL,
            lot_title TEXT,
            purchased_units INTEGER NOT NULL DEFAULT 1,
            token_amount BIGINT NOT NULL,
            review_bonus_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            review_bonus_tokens BIGINT,
            prefix TEXT NOT NULL,
            promo_code TEXT,
            api_promo_id TEXT,
            api_status TEXT,
            status TEXT NOT NULL DEFAULT 'creating',
            error_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (telegram_id, kind, source_id)
        );

        CREATE INDEX IF NOT EXISTS emerald_promo_issues_status_idx
            ON emerald_promo_issues (telegram_id, status, updated_at DESC);
        """
    )
    await _db().execute(
        """INSERT INTO emerald_promo_settings (telegram_id)
            VALUES ($1) ON CONFLICT (telegram_id) DO NOTHING""",
        _telegram_id(),
    )
    await _db().execute(
        """UPDATE emerald_promo_settings
              SET api_base_url=$2, updated_at=NOW()
            WHERE telegram_id=$1 AND api_base_url=$3""",
        _telegram_id(), DEFAULT_API_URL, LEGACY_API_URL,
    )


async def _settings() -> Any:
    await _ensure_schema()
    return await _db().fetchrow(
        "SELECT * FROM emerald_promo_settings WHERE telegram_id=$1",
        _telegram_id(),
    )


async def _set_setting(column: str, value: Any) -> None:
    if column not in {
        "api_base_url", "api_token_enc", "free_enabled",
        "free_token_amount", "min_account_age_days",
    }:
        raise ValueError("неизвестная настройка")
    await _ensure_schema()
    await _db().execute(
        f"""UPDATE emerald_promo_settings
               SET {column}=$2, updated_at=NOW() WHERE telegram_id=$1""",
        _telegram_id(), value,
    )


async def _rules() -> list[Any]:
    await _ensure_schema()
    return list(await _db().fetch(
        """SELECT * FROM emerald_promo_lot_rules
             WHERE telegram_id=$1 ORDER BY created_at, id""",
        _telegram_id(),
    ))


async def _rule(rule_id: int) -> Any | None:
    await _ensure_schema()
    return await _db().fetchrow(
        """SELECT * FROM emerald_promo_lot_rules
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), rule_id,
    )


async def _upsert_rule(lot_id: str, lot_title: str, tokens_per_unit: int) -> Any:
    return await _db().fetchrow(
        """INSERT INTO emerald_promo_lot_rules
               (telegram_id, lot_id, lot_title, tokens_per_unit)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (telegram_id, lot_id) DO UPDATE SET
                lot_title=EXCLUDED.lot_title,
                tokens_per_unit=EXCLUDED.tokens_per_unit,
                enabled=TRUE, updated_at=NOW()
            RETURNING *""",
        _telegram_id(), lot_id, lot_title, tokens_per_unit,
    )


async def _update_rule(rule_id: int, column: str, value: Any) -> None:
    if column not in {
        "tokens_per_unit", "review_bonus_enabled", "review_bonus_tokens", "enabled"
    }:
        raise ValueError("неизвестное поле лота")
    await _db().execute(
        f"""UPDATE emerald_promo_lot_rules
               SET {column}=$3, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), rule_id, value,
    )


async def _delete_rule(rule_id: int) -> None:
    await _db().execute(
        """DELETE FROM emerald_promo_lot_rules
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), rule_id,
    )


async def _issue(issue_id: int) -> Any | None:
    return await _db().fetchrow(
        """SELECT * FROM emerald_promo_issues
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), issue_id,
    )


async def _issue_by_source(kind: str, source_id: str) -> Any | None:
    return await _db().fetchrow(
        """SELECT * FROM emerald_promo_issues
             WHERE telegram_id=$1 AND kind=$2 AND source_id=$3""",
        _telegram_id(), kind, source_id,
    )


async def _reserve_issue(
    *, kind: str, source_id: str, buyer_id: int | None, order_id: str | None,
    chat_id: str, chat_name: str | None, rule: Any | None, token_amount: int,
    purchased_units: int = 1, review_bonus_enabled: bool = False,
    review_bonus_tokens: int | None = None,
) -> tuple[Any, bool]:
    prefix = _promo_prefix(kind, source_id)
    row = await _db().fetchrow(
        """INSERT INTO emerald_promo_issues
               (telegram_id, kind, source_id, buyer_id, order_id, chat_id,
                chat_name, rule_id, lot_title, purchased_units, token_amount,
                review_bonus_enabled, review_bonus_tokens, prefix)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            ON CONFLICT (telegram_id, kind, source_id) DO NOTHING
            RETURNING *""",
        _telegram_id(), kind, source_id, buyer_id, order_id, chat_id, chat_name,
        int(rule["id"]) if rule else None,
        str(rule["lot_title"]) if rule else None,
        purchased_units, token_amount, review_bonus_enabled,
        review_bonus_tokens, prefix,
    )
    if row:
        return row, True
    existing = await _issue_by_source(kind, source_id)
    if not existing:
        raise RuntimeError("не удалось зарезервировать выдачу промокода")
    return existing, False


async def _update_issue(issue_id: int, **values: Any) -> None:
    allowed = {
        "promo_code", "api_promo_id", "api_status", "status", "error_text",
        "chat_id", "chat_name",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(
        f"{key}=${index + 3}" for index, key in enumerate(values)
    )
    await _db().execute(
        f"""UPDATE emerald_promo_issues SET {assignments}, updated_at=NOW()
             WHERE telegram_id=$1 AND id=$2""",
        _telegram_id(), issue_id, *values.values(),
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
    return "••••" + token[-4:]


def _validate_api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("нужен HTTPS URL без логина и пароля")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL не должен содержать query или fragment")
    if value == LEGACY_API_URL:
        return DEFAULT_API_URL
    return value


def _safe_redirect_url(source_url: str, location: str) -> str:
    target_url = urljoin(source_url, location)
    source = urlsplit(source_url)
    target = urlsplit(target_url)
    source_host = (source.hostname or "").casefold().removeprefix("www.")
    target_host = (target.hostname or "").casefold().removeprefix("www.")
    if (
        target.scheme != "https"
        or target.username
        or target.password
        or not source_host
        or source_host != target_host
        or target.port not in {None, 443}
    ):
        raise RuntimeError("Emerald API попытался перенаправить запрос на небезопасный адрес")
    return target_url


def _validate_token_amount(value: str | int) -> int:
    raw = str(value).replace(" ", "").strip()
    if not raw.isdigit():
        raise ValueError("нужно отправить целое количество токенов")
    amount = int(raw)
    if not MIN_TOKEN_AMOUNT <= amount <= MAX_TOKEN_AMOUNT:
        raise ValueError(
            f"номинал должен быть от {MIN_TOKEN_AMOUNT:,} до {MAX_TOKEN_AMOUNT:,} токенов".replace(",", " ")
        )
    return amount


def _calculate_total(tokens_per_unit: int, purchased_units: int | None) -> int:
    units = max(1, int(purchased_units or 1))
    total = int(tokens_per_unit) * units
    if total > MAX_TOKEN_AMOUNT:
        raise ValueError("итоговый номинал превышает 1 000 000 000 токенов")
    return _validate_token_amount(total)


def _api_request(
    settings: Any, method: str, path: str = "", *,
    json_payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = _api_token(settings)
    if not token:
        raise RuntimeError("API-токен не задан")
    url = f"{str(settings['api_base_url']).rstrip('/')}/{path.lstrip('/')}".rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    response = requests.request(
        method, url, headers=headers, json=json_payload, params=params,
        timeout=25, allow_redirects=False,
    )
    if response.status_code in {307, 308} and response.headers.get("Location"):
        url = _safe_redirect_url(url, response.headers["Location"])
        response = requests.request(
            method, url, headers=headers, json=json_payload, params=params,
            timeout=25, allow_redirects=False,
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"Emerald API вернул HTTP {response.status_code} без JSON") from exc
    if not response.ok:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = str(error.get("code") or f"http_{response.status_code}")
        message = str(error.get("message") or "неизвестная ошибка")
        raise RuntimeError(f"{code}: {message}")
    if not isinstance(payload, dict):
        raise RuntimeError("Emerald API вернул неожиданный ответ")
    return payload


def _account_data(settings: Any) -> dict[str, Any]:
    payload = _api_request(settings, "GET", "account")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Emerald API не вернул данные аккаунта")
    return data


def _server_minimum(account_data: dict[str, Any]) -> int:
    pricing = account_data.get("pricing")
    raw = pricing.get("minimum_token_promo") if isinstance(pricing, dict) else None
    try:
        return max(MIN_TOKEN_AMOUNT, int(raw or MIN_TOKEN_AMOUNT))
    except (TypeError, ValueError):
        return MIN_TOKEN_AMOUNT


def _create_promo_code(settings: Any, amount: int, prefix: str) -> dict[str, Any]:
    account = _account_data(settings)
    minimum = _server_minimum(account)
    if amount < minimum:
        raise RuntimeError(
            f"текущий минимум Emerald API — {minimum:,} токенов".replace(",", " ")
        )
    balance = int(account.get("balance_tokens") or 0)
    if balance < amount:
        raise RuntimeError(
            f"недостаточно баланса Emerald: доступно {balance:,}, требуется {amount:,}".replace(",", " ")
        )
    payload = _api_request(
        settings, "POST", "promo-codes",
        json_payload={
            "type": "tokens", "token_amount": amount,
            "quantity": 1, "prefix": prefix,
        },
    )
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise RuntimeError("Emerald API не вернул созданный промокод")
    code = str(data[0].get("code") or "").strip()
    if not code:
        raise RuntimeError("Emerald API вернул промокод без значения code")
    return data[0]


def _recover_promo_code(settings: Any, amount: int, prefix: str) -> dict[str, Any] | None:
    for offset in range(0, 500, 100):
        payload = _api_request(
            settings, "GET", "promo-codes", params={"limit": 100, "offset": offset}
        )
        data = payload.get("data")
        if not isinstance(data, list):
            return None
        for promo in data:
            if not isinstance(promo, dict):
                continue
            code = str(promo.get("code") or "").upper()
            if (
                code.startswith(prefix.upper() + "-")
                and str(promo.get("type") or "").casefold() == "tokens"
                and int(promo.get("token_amount") or 0) == amount
            ):
                return promo
        if len(data) < 100:
            return None
    return None


def _promo_prefix(kind: str, source_id: str) -> str:
    marker = {"free": "F", "sale": "S", "review": "R"}.get(kind, "E")
    digest = hashlib.sha256(
        f"{_telegram_id()}:{kind}:{source_id}".encode("utf-8")
    ).hexdigest().upper()
    return marker + digest[:9]


def _format_tokens(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _free_message(issue: Any, *, repeated: bool = False) -> str:
    heading = "ВАШ БЕСПЛАТНЫЙ ДОСТУП" if not repeated else "ВАШ ТЕСТОВЫЙ КОД"
    return (
        "╔══════════════════════════╗\n"
        f"║  🎁 {heading}\n"
        "╚══════════════════════════╝\n\n"
        f"🔑 Промокод: {issue['promo_code']}\n"
        f"🪙 Номинал: {_format_tokens(issue['token_amount'])} токенов\n\n"
        f"🌐 Активировать: {ACTIVATION_URL}\n\n"
        "Промокод одноразовый и предназначен для тестирования моделей EmeraldAI. "
        "Бесплатный доступ выдаётся одному FunPay-аккаунту только один раз."
    )


def _sale_message(issue: Any) -> str:
    text = (
        "╔══════════════════════════╗\n"
        "║  💎 ПРОМОКОД EMERALDAI\n"
        "╚══════════════════════════╝\n\n"
        f"🔑 Промокод: {issue['promo_code']}\n"
        f"🪙 Номинал: {_format_tokens(issue['token_amount'])} токенов\n"
        f"📦 Куплено единиц: {issue['purchased_units']}\n\n"
        f"🌐 Активировать: {ACTIVATION_URL}\n\n"
        "Код можно активировать один раз. Никому не передавайте его до активации."
    )
    if issue["review_bonus_enabled"] and int(issue["review_bonus_tokens"] or 0) > 0:
        text += (
            "\n\n┏━━━━━━━━ 🎁 БОНУС ЗА ОТЗЫВ ━━━━━━━━┓\n"
            "Оставьте этому заказу отзыв ровно на 5 звёзд — бот автоматически "
            f"выдаст ещё один промокод на {_format_tokens(issue['review_bonus_tokens'])} токенов.\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
        )
    return text


def _review_message(issue: Any) -> str:
    return (
        "╔══════════════════════════╗\n"
        "║  ⭐ БОНУС ЗА ОТЗЫВ 5★\n"
        "╚══════════════════════════╝\n\n"
        "Спасибо за отличную оценку!\n"
        f"🔑 Промокод: {issue['promo_code']}\n"
        f"🪙 Номинал: {_format_tokens(issue['token_amount'])} токенов\n\n"
        f"🌐 Активировать: {ACTIVATION_URL}\n"
        "Промокод одноразовый."
    )


def _issue_message(issue: Any, *, repeated: bool = False) -> str:
    if issue["kind"] == "free":
        return _free_message(issue, repeated=repeated)
    if issue["kind"] == "review":
        return _review_message(issue)
    return _sale_message(issue)


async def _funpay_send(issue: Any, text: str) -> None:
    await asyncio.to_thread(
        _cardinal.account.send_message,
        issue["chat_id"], text, issue["chat_name"],
    )


async def _notify_owner(text: str) -> None:
    await asyncio.to_thread(_cardinal.telegram.send_notification, text)


async def _fulfill_issue(issue_id: int) -> None:
    if issue_id in _running_issue_ids:
        return
    _running_issue_ids.add(issue_id)
    issue: Any | None = None
    try:
        issue = await _issue(issue_id)
        if not issue or issue["status"] == "sent":
            return
        settings = await _settings()
        promo: dict[str, Any] | None = None
        if issue["promo_code"]:
            promo = {
                "code": issue["promo_code"], "id": issue["api_promo_id"],
                "status": issue["api_status"] or "available",
            }
        else:
            try:
                promo = await asyncio.to_thread(
                    _recover_promo_code, settings,
                    int(issue["token_amount"]), str(issue["prefix"]),
                )
            except Exception:
                logger.warning("Не выполнено предварительное восстановление Emerald-кода", exc_info=True)
            if promo is None:
                try:
                    promo = await asyncio.to_thread(
                        _create_promo_code, settings,
                        int(issue["token_amount"]), str(issue["prefix"]),
                    )
                except Exception:
                    try:
                        promo = await asyncio.to_thread(
                            _recover_promo_code, settings,
                            int(issue["token_amount"]), str(issue["prefix"]),
                        )
                    except Exception:
                        promo = None
                    if promo is None:
                        raise
            await _update_issue(
                issue_id,
                promo_code=str(promo["code"]),
                api_promo_id=str(promo.get("id") or "") or None,
                api_status=str(promo.get("status") or "available"),
                status="created", error_text=None,
            )
            issue = await _issue(issue_id)
        await _funpay_send(issue, _issue_message(issue))
        await _update_issue(issue_id, status="sent", error_text=None)
        await _notify_owner(
            "✅ <b>Emerald Promo выдал промокод</b>\n\n"
            f"Тип: <b>{html.escape(str(issue['kind']))}</b>\n"
            f"Заказ/источник: <code>{html.escape(str(issue['source_id']))}</code>\n"
            f"Номинал: <b>{_format_tokens(issue['token_amount'])}</b> токенов"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Не выдан Emerald-промокод для issue %s", issue_id)
        await _update_issue(issue_id, status="failed", error_text=str(exc)[:1000])
        if issue:
            try:
                await _funpay_send(
                    issue,
                    "❌ Промокод пока не удалось выпустить. Продавец уже уведомлён; "
                    "повторная попытка не создаст второй код.",
                )
            except Exception:
                logger.exception("Не отправлено сообщение об ошибке Emerald Promo")
            await _notify_owner(
                "❌ <b>Emerald Promo: ошибка выдачи</b>\n\n"
                f"Источник: <code>{html.escape(str(issue['source_id']))}</code>\n"
                f"Ошибка: <code>{html.escape(str(exc)[:700])}</code>"
            )
    finally:
        _running_issue_ids.discard(issue_id)


def _normalized_order_title(value: str) -> str:
    value = re.sub(
        r",\s*\d{1,3}(?:\s?\d{3})*\s*(?:шт|pcs)\.\s*$",
        "",
        str(value or ""),
        flags=re.I,
    )
    return " ".join(value.split()).casefold()


def _order_lot_id(order: Any) -> str | None:
    direct = getattr(order, "lot_id", None)
    if direct is not None:
        return str(direct)
    widget_html = str(getattr(order, "html", "") or "")
    match = re.search(
        r"(?:lots/offer\?id=|offer=|data-offer=[\"'])(\d+)",
        widget_html,
        re.I,
    )
    return match.group(1) if match else None


def _match_rule(
    description: str, rules: list[Any], lot_id: str | None = None
) -> Any | None:
    enabled = [rule for rule in rules if bool(rule["enabled"])]
    if lot_id is not None:
        return next(
            (rule for rule in enabled if str(rule["lot_id"]) == str(lot_id)),
            None,
        )
    normalized = _normalized_order_title(description)
    matches = [
        rule for rule in enabled
        if _normalized_order_title(str(rule["lot_title"])) == normalized
    ]
    return matches[0] if len(matches) == 1 else None


def _registration_date(profile: Any) -> datetime | None:
    soup = BeautifulSoup(str(getattr(profile, "html", "") or ""), "lxml")
    candidates: list[str] = []
    params = soup.select("div.profile-header div.param-item")
    for item in params:
        text_value = " ".join(item.stripped_strings)
        lowered = text_value.casefold()
        if any(
            marker in lowered
            for marker in ("дата регистрации", "registration", "зареєстр")
        ):
            candidates.insert(0, text_value)
        else:
            candidates.append(text_value)
    month_names = {str(name).casefold(): number for name, number in MONTHS.items()}
    date_pattern = re.compile(
        r"(?<!\d)(\d{1,2})\s+([A-Za-zА-Яа-яЁёІіЇїЄє]+)\s+(\d{4})(?!\d)",
        re.I,
    )
    for candidate in candidates:
        match = date_pattern.search(candidate)
        if not match:
            continue
        month = month_names.get(match.group(2).casefold())
        if month is None:
            continue
        try:
            # FunPay показывает даты профиля по московскому времени.
            local = datetime(
                int(match.group(3)), month, int(match.group(1)),
                tzinfo=timezone(timedelta(hours=3)),
            )
            return local.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _profile_age_days(profile: Any, now: datetime | None = None) -> int | None:
    registered = _registration_date(profile)
    if registered is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, int((current - registered).total_seconds() // 86400))


def _extract_order_id(message: Any) -> str | None:
    direct = getattr(message, "order_id", None)
    if direct:
        return str(direct)
    match = re.search(r"#([A-Za-z0-9_-]{4,40})", str(getattr(message, "text", "") or message))
    return match.group(1) if match else None


async def _process_new_order(order: dict[str, Any]) -> None:
    rule = _match_rule(
        str(order.get("description") or ""),
        await _rules(),
        order.get("lot_id"),
    )
    if not rule:
        return
    settings = await _settings()
    if not settings["api_token_enc"]:
        await _notify_owner(
            "❌ Заказ относится к Emerald Promo, но API-токен не настроен.\n"
            f"Заказ: <code>#{html.escape(str(order['id']))}</code>"
        )
        return
    try:
        units = max(1, int(order.get("amount") or 1))
        total = _calculate_total(int(rule["tokens_per_unit"]), units)
    except Exception as exc:
        await _notify_owner(
            "❌ Emerald Promo не может рассчитать номинал.\n"
            f"Заказ: <code>#{html.escape(str(order['id']))}</code>\n"
            f"Ошибка: <code>{html.escape(str(exc))}</code>"
        )
        return
    issue, created = await _reserve_issue(
        kind="sale",
        source_id=str(order["id"]),
        buyer_id=order.get("buyer_id"),
        order_id=str(order["id"]),
        chat_id=str(order["chat_id"]),
        chat_name=order.get("chat_name"),
        rule=rule,
        token_amount=total,
        purchased_units=units,
        review_bonus_enabled=bool(rule["review_bonus_enabled"]),
        review_bonus_tokens=int(rule["review_bonus_tokens"]),
    )
    if not created and issue["status"] == "sent":
        return
    await _fulfill_issue(int(issue["id"]))


async def _process_free(message: dict[str, Any]) -> None:
    buyer_id = message.get("buyer_id")
    if not buyer_id:
        return
    source_id = str(buyer_id)
    existing = await _issue_by_source("free", source_id)
    if existing:
        if existing["status"] == "sent" and existing["promo_code"]:
            await _funpay_send(existing, _free_message(existing, repeated=True))
        else:
            await _fulfill_issue(int(existing["id"]))
        return
    settings = await _settings()
    if not bool(settings["free_enabled"]):
        await asyncio.to_thread(
            _cardinal.account.send_message,
            message["chat_id"],
            "ℹ️ Бесплатный тестовый промокод сейчас недоступен.",
            message.get("chat_name"),
        )
        return
    try:
        profile = await asyncio.to_thread(_cardinal.account.get_user, int(buyer_id))
        age_days = _profile_age_days(profile)
    except Exception:
        logger.exception("Не проверен возраст FunPay-профиля %s", buyer_id)
        age_days = None
    minimum_days = max(0, int(settings["min_account_age_days"]))
    if age_days is None:
        await asyncio.to_thread(
            _cardinal.account.send_message,
            message["chat_id"],
            "❌ Не удалось подтвердить дату регистрации FunPay-аккаунта. "
            "Промокод не выдан; попробуйте позже.",
            message.get("chat_name"),
        )
        return
    if age_days < minimum_days:
        wait_days = minimum_days - age_days
        await asyncio.to_thread(
            _cardinal.account.send_message,
            message["chat_id"],
            "⏳ Бесплатный тест доступен аккаунтам FunPay старше "
            f"{minimum_days} дней. Попробуйте примерно через {wait_days} дн.",
            message.get("chat_name"),
        )
        return
    issue, _created = await _reserve_issue(
        kind="free",
        source_id=source_id,
        buyer_id=int(buyer_id),
        order_id=None,
        chat_id=str(message["chat_id"]),
        chat_name=message.get("chat_name"),
        rule=None,
        token_amount=int(settings["free_token_amount"]),
    )
    await _fulfill_issue(int(issue["id"]))


async def _process_review(message: Any) -> None:
    order_id = _extract_order_id(message)
    if not order_id:
        return
    sale = await _issue_by_source("sale", order_id)
    if (
        not sale
        or not bool(sale["review_bonus_enabled"])
        or int(sale["review_bonus_tokens"] or 0) < MIN_TOKEN_AMOUNT
    ):
        return
    try:
        order = await asyncio.to_thread(_cardinal.account.get_order, order_id)
    except Exception:
        logger.exception("Не получен FunPay-заказ для отзыва %s", order_id)
        return
    review = getattr(order, "review", None)
    if (
        int(getattr(review, "stars", 0) or 0) != 5
        or int(getattr(order, "seller_id", 0) or 0) != int(_cardinal.account.id)
    ):
        return
    issue, created = await _reserve_issue(
        kind="review",
        source_id=order_id,
        buyer_id=_row_get(sale, "buyer_id"),
        order_id=order_id,
        chat_id=str(sale["chat_id"]),
        chat_name=_row_get(sale, "chat_name"),
        rule=None,
        token_amount=int(sale["review_bonus_tokens"]),
    )
    if not created and issue["status"] == "sent":
        return
    await _fulfill_issue(int(issue["id"]))


async def _resume_issues() -> None:
    await _ensure_schema()
    rows = await _db().fetch(
        """SELECT id FROM emerald_promo_issues
             WHERE telegram_id=$1 AND status IN ('creating', 'created')
             ORDER BY created_at""",
        _telegram_id(),
    )
    for row in rows:
        _spawn(_fulfill_issue(int(row["id"])))


async def _issue_stats() -> Any:
    await _ensure_schema()
    return await _db().fetchrow(
        """SELECT COUNT(*) FILTER (WHERE status='sent') AS sent,
                  COUNT(*) FILTER (WHERE status='failed') AS failed
             FROM emerald_promo_issues WHERE telegram_id=$1""",
        _telegram_id(),
    )


def _show_settings(chat_id: int) -> None:
    settings = _sync(_settings())
    rules = _sync(_rules())
    stats = _sync(_issue_stats())
    active = sum(1 for rule in rules if rule["enabled"])
    free_state = "включена" if settings["free_enabled"] else "выключена"
    lines = [
        "⚙️ <b>Emerald Promo</b>",
        "",
        f"Base URL: <code>{html.escape(str(settings['api_base_url']))}</code>",
        f"API-токен: <b>{html.escape(_token_label(settings))}</b>",
        f"Команда #free: <b>{free_state}</b>",
        f"Бесплатный номинал: <b>{_format_tokens(settings['free_token_amount'])}</b>",
        f"Мин. возраст FunPay: <b>{settings['min_account_age_days']} дн.</b>",
        f"Привязки: <b>{active}/{len(rules)} включено</b>",
        f"Выдано: <b>{int(_row_get(stats, 'sent', 0) or 0)}</b> · "
        f"ошибок: <b>{int(_row_get(stats, 'failed', 0) or 0)}</b>",
        "",
        "Допустимый номинал: от 10 000 до 1 000 000 000 токенов. "
        "Для лота задаётся номинал за одну купленную единицу.",
    ]
    if rules:
        lines.extend(["", "<b>Лоты</b>"])
        for rule in rules[:12]:
            state = "✅" if rule["enabled"] else "⏸"
            bonus = (
                f" · бонус {_format_tokens(rule['review_bonus_tokens'])}"
                if rule["review_bonus_enabled"] else ""
            )
            lines.append(
                f"{state} {html.escape(str(rule['lot_title'])[:65])}\n"
                f"   {_format_tokens(rule['tokens_per_unit'])} токенов за 1 шт.{bonus}"
            )
    rows: list[list[tuple[str, str]]] = [
        [("🌐 Base URL API", f"{CALLBACK_PREFIX}set:base")],
        [("🔑 API-токен", f"{CALLBACK_PREFIX}set:token")],
        [("🎁 Вкл/выкл #free", f"{CALLBACK_PREFIX}free")],
        [("🪙 Номинал #free", f"{CALLBACK_PREFIX}set:free")],
        [("📅 Возраст для #free", f"{CALLBACK_PREFIX}set:age")],
        [("➕ Добавить лот", f"{CALLBACK_PREFIX}lots")],
        [("🧩 Управление лотами", f"{CALLBACK_PREFIX}rules")],
        [("🧪 Проверить API", f"{CALLBACK_PREFIX}api")],
    ]
    if int(_row_get(stats, "failed", 0) or 0):
        rows.append([("🔁 Повторить ошибки", f"{CALLBACK_PREFIX}retry")])
    rows.append([("🔄 Обновить", SETTINGS_CALLBACK)])
    _bot().send_message(chat_id, "\n".join(lines), reply_markup=_markup(*rows))


def _show_rules(chat_id: int) -> None:
    rules = _sync(_rules())
    rows = [
        [(
            f"{'✅' if rule['enabled'] else '⏸'} {str(rule['lot_title'])[:42]}",
            f"{CALLBACK_PREFIX}r:{rule['id']}",
        )]
        for rule in rules
    ]
    rows.extend([
        [("➕ Добавить лот", f"{CALLBACK_PREFIX}lots")],
        [("⬅️ Настройки", SETTINGS_CALLBACK)],
    ])
    text = (
        "🧩 <b>Привязки Emerald Promo</b>\n\nВыберите лот."
        if rules else "🧩 Привязок пока нет. Добавьте первый лот."
    )
    _bot().send_message(chat_id, text, reply_markup=_markup(*rows))


def _show_rule(chat_id: int, rule_id: int) -> None:
    rule = _sync(_rule(rule_id))
    if not rule:
        raise RuntimeError("привязка не найдена")
    text = (
        f"🛒 <b>{html.escape(str(rule['lot_title']))}</b>\n\n"
        f"ID лота: <code>{html.escape(str(rule['lot_id']))}</code>\n"
        f"За 1 шт.: <b>{_format_tokens(rule['tokens_per_unit'])}</b> токенов\n"
        f"Бонус за 5★: <b>{'включён' if rule['review_bonus_enabled'] else 'выключен'}</b>\n"
        f"Номинал бонуса: <b>{_format_tokens(rule['review_bonus_tokens'])}</b>\n"
        f"Статус: <b>{'включён' if rule['enabled'] else 'выключен'}</b>"
    )
    rows = [
        [("🪙 Изменить номинал", f"{CALLBACK_PREFIX}rt:{rule_id}")],
        [(
            "🚫 Выключить бонус 5★" if rule["review_bonus_enabled"]
            else "⭐ Включить бонус 5★",
            f"{CALLBACK_PREFIX}rb:{rule_id}",
        )],
        [("🎁 Изменить бонус", f"{CALLBACK_PREFIX}rbt:{rule_id}")],
        [(
            "⏸ Выключить лот" if rule["enabled"] else "▶️ Включить лот",
            f"{CALLBACK_PREFIX}re:{rule_id}",
        )],
        [("🗑 Удалить привязку", f"{CALLBACK_PREFIX}rd:{rule_id}")],
        [("⬅️ Все лоты", f"{CALLBACK_PREFIX}rules")],
    ]
    _bot().send_message(chat_id, text, reply_markup=_markup(*rows))


def _prompt(chat_id: int, key: str, text_value: str, context: int | None = None) -> None:
    global _pending_input
    _pending_input = (key, context)
    _bot().send_message(chat_id, text_value)


def _load_lots(chat_id: int) -> None:
    global _lot_cache
    profile = _cardinal.account.get_user(_cardinal.account.id)
    existing = {str(rule["lot_id"]) for rule in _sync(_rules())}
    available = [lot for lot in profile.get_lots() if str(lot.id) not in existing]
    if not available:
        _bot().send_message(chat_id, "❌ Свободных лотов не найдено.")
        return
    _lot_cache = {
        str(index): (
            str(lot.id),
            str(getattr(lot, "description", None) or f"Лот {lot.id}"),
        )
        for index, lot in enumerate(available[:40], start=1)
    }
    rows = [
        [(f"{title[:45]} · ID {lot_id}", f"{CALLBACK_PREFIX}lot:{key}")]
        for key, (lot_id, title) in _lot_cache.items()
    ]
    rows.append([("⬅️ Настройки", SETTINGS_CALLBACK)])
    _bot().send_message(
        chat_id,
        "🛒 <b>Выберите лот</b>\n\nПосле выбора укажите токены за одну купленную единицу.",
        reply_markup=_markup(*rows),
    )


async def _api_test(chat_id: int) -> None:
    try:
        settings = await _settings()
        account = await asyncio.to_thread(_account_data, settings)
        minimum = _server_minimum(account)
        text_value = (
            "✅ Emerald API отвечает.\n"
            f"Баланс: <b>{_format_tokens(int(account.get('balance_tokens') or 0))}</b> токенов\n"
            f"Минимальный промокод: <b>{_format_tokens(minimum)}</b> токенов"
        )
    except Exception as exc:
        logger.exception("Проверка Emerald API не выполнена")
        text_value = f"❌ Emerald API не отвечает: <code>{html.escape(str(exc)[:500])}</code>"
    await asyncio.to_thread(_bot().send_message, chat_id, text_value)


async def _retry_failed(chat_id: int) -> None:
    rows = await _db().fetch(
        """SELECT id FROM emerald_promo_issues
             WHERE telegram_id=$1 AND status='failed'
             ORDER BY updated_at LIMIT 20""",
        _telegram_id(),
    )
    for row in rows:
        await _update_issue(int(row["id"]), status="creating", error_text=None)
        _spawn(_fulfill_issue(int(row["id"])))
    await asyncio.to_thread(
        _bot().send_message, chat_id,
        f"🔁 Запущена повторная обработка: {len(rows)}.",
    )


def _on_callback(call: Any) -> None:
    data = str(call.data or "")
    chat_id = int(call.message.chat.id)
    try:
        _bot().answer_callback_query(call.id)
        if data == SETTINGS_CALLBACK or data == f"{CALLBACK_PREFIX}open":
            _show_settings(chat_id)
        elif data == f"{CALLBACK_PREFIX}set:base":
            _prompt(chat_id, "base", f"Отправьте HTTPS Base URL API. По умолчанию: <code>{DEFAULT_API_URL}</code>")
        elif data == f"{CALLBACK_PREFIX}set:token":
            _prompt(chat_id, "token", "Отправьте Seller API-токен. Сообщение будет удалено, токен сохранится зашифрованным.")
        elif data == f"{CALLBACK_PREFIX}free":
            settings = _sync(_settings())
            _sync(_set_setting("free_enabled", not bool(settings["free_enabled"])))
            _show_settings(chat_id)
        elif data == f"{CALLBACK_PREFIX}set:free":
            _prompt(chat_id, "free_amount", "Отправьте номинал #free от 10 000 до 1 000 000 000. По умолчанию 200 000.")
        elif data == f"{CALLBACK_PREFIX}set:age":
            _prompt(chat_id, "age", "Отправьте минимальный возраст FunPay-аккаунта в днях (0–3650). Рекомендуется 7.")
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
            _prompt(chat_id, "new_tokens", "Отправьте количество токенов за одну купленную единицу (минимум 10 000).")
        elif data.startswith(f"{CALLBACK_PREFIX}r:"):
            _show_rule(chat_id, int(data.rsplit(":", 1)[1]))
        elif data.startswith(f"{CALLBACK_PREFIX}rt:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_tokens", "Отправьте новый номинал за одну единицу.", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rbt:"):
            rule_id = int(data.rsplit(":", 1)[1])
            _prompt(chat_id, "edit_bonus", "Отправьте новый номинал бонуса за отзыв 5★.", rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}rb:"):
            rule_id = int(data.rsplit(":", 1)[1])
            rule = _sync(_rule(rule_id))
            if not rule:
                raise RuntimeError("привязка не найдена")
            _sync(_update_rule(rule_id, "review_bonus_enabled", not bool(rule["review_bonus_enabled"])))
            _show_rule(chat_id, rule_id)
        elif data.startswith(f"{CALLBACK_PREFIX}re:"):
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
                f"Удалить привязку <b>{html.escape(str(rule['lot_title']))}</b>? Уже выданные коды сохранятся.",
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
        elif data == f"{CALLBACK_PREFIX}retry":
            _spawn(_retry_failed(chat_id))
    except Exception as exc:
        logger.exception("Ошибка callback Emerald Promo")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


def _on_setting_message(message: Any) -> None:
    global _pending_input
    if _pending_input is None:
        value = str(message.text or "").strip()
        if not value.startswith("sk-em-seller-"):
            return
        # После перезапуска runtime временный мастер может потеряться. Seller-
        # ключ имеет однозначный префикс, поэтому безопасно восстанавливаем шаг.
        key, context = "token", None
    else:
        key, context = _pending_input
        value = str(message.text or "").strip()
    _pending_input = None
    chat_id = int(message.chat.id)
    try:
        if key == "token":
            try:
                _bot().delete_message(chat_id, message.message_id)
            except Exception:
                logger.warning("Не удалено сообщение с Emerald API-токеном", exc_info=True)
        if key == "base":
            _sync(_set_setting("api_base_url", _validate_api_url(value)))
        elif key == "token":
            if not 16 <= len(value) <= 1024:
                raise ValueError("длина API-токена должна быть от 16 до 1024 символов")
            if not value.startswith("sk-em-seller-"):
                raise ValueError("Seller API-токен должен начинаться с sk-em-seller-")
            _sync(_set_setting("api_token_enc", _secret_box().encrypt(value)))
        elif key == "free_amount":
            _sync(_set_setting("free_token_amount", _validate_token_amount(value)))
        elif key == "age":
            if not value.isdigit() or not 0 <= int(value) <= 3650:
                raise ValueError("возраст должен быть целым числом от 0 до 3650 дней")
            _sync(_set_setting("min_account_age_days", int(value)))
        elif key == "new_tokens":
            required = {"lot_id", "lot_title"}
            if not required.issubset(_draft_rule):
                raise RuntimeError("мастер добавления устарел; выберите лот заново")
            _sync(_upsert_rule(
                str(_draft_rule["lot_id"]),
                str(_draft_rule["lot_title"]),
                _validate_token_amount(value),
            ))
            _draft_rule.clear()
        elif key == "edit_tokens" and context is not None:
            _sync(_update_rule(context, "tokens_per_unit", _validate_token_amount(value)))
        elif key == "edit_bonus" and context is not None:
            _sync(_update_rule(context, "review_bonus_tokens", _validate_token_amount(value)))
        _bot().send_message(chat_id, "✅ Настройка сохранена.")
        _show_settings(chat_id)
    except Exception as exc:
        logger.exception("Настройка Emerald Promo не сохранена")
        _bot().send_message(chat_id, f"❌ {html.escape(str(exc)[:500])}")


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
        func=lambda message: _pending_input is not None
        or str(message.text or "").strip().startswith("sk-em-seller-"),
    )


def post_start(cardinal: Any) -> None:
    global _cardinal
    _cardinal = cardinal
    _spawn(_resume_issues())


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
        "description": str(order.description or ""),
        "lot_id": _order_lot_id(order),
        "amount": max(1, int(getattr(order, "amount", None) or 1)),
    }))


def new_message(cardinal: Any, event: Any) -> None:
    message = event.message
    type_name = getattr(getattr(message, "type", None), "name", "")
    if type_name in {"NEW_FEEDBACK", "FEEDBACK_CHANGED"}:
        _spawn(_process_review(message))
        return
    if (
        getattr(message, "author_id", None) in {0, cardinal.account.id}
        or getattr(message, "by_bot", False)
        or getattr(message, "by_vertex", False)
        or str(getattr(message, "text", "") or "").strip().casefold() != "#free"
    ):
        return
    buyer_id = (
        int(message.interlocutor_id)
        if getattr(message, "interlocutor_id", None) else None
    )
    _spawn(_process_free({
        "chat_id": str(message.chat_id),
        "chat_name": str(message.chat_name or ""),
        "buyer_id": buyer_id,
    }))


def pre_stop(cardinal: Any) -> None:
    global _pending_input
    _pending_input = None
    for future in list(_futures):
        future.cancel()


def on_delete(cardinal: Any, callback: Any) -> None:
    pre_stop(cardinal)
    _sync(_db().execute(
        "DELETE FROM emerald_promo_issues WHERE telegram_id=$1", _telegram_id()
    ))
    _sync(_db().execute(
        "DELETE FROM emerald_promo_lot_rules WHERE telegram_id=$1", _telegram_id()
    ))
    _sync(_db().execute(
        "DELETE FROM emerald_promo_settings WHERE telegram_id=$1", _telegram_id()
    ))


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
BIND_TO_ORDER_STATUS_CHANGED = []
BIND_TO_PRE_DELIVERY = []
BIND_TO_POST_DELIVERY = []
BIND_TO_PRE_LOTS_RAISE = []
BIND_TO_POST_LOTS_RAISE = []
BIND_TO_TELETHON_READY = []
BIND_TO_TELETHON_DISCONNECTED = []
BIND_TO_DELETE = on_delete
