from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import urllib.parse
import urllib.request
from concurrent.futures import CancelledError, Future
from datetime import datetime, timezone
from typing import Any

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

NAME = "AI Market Radar"
VERSION = "1.0.0"
DESCRIPTION = "AI-мониторинг и парсинг выгодных лотов FunPay с авто-уведомлениями для перепродажи"
CREDITS = "FunPay aiogram bot"
SETTINGS_PAGE = True
TELETHON = False
UUID = "7736a016-3e1e-4d97-8750-821c652fed76"

CALLBACK_PREFIX = "aimr:"
SETTINGS_CALLBACK = f"47:{UUID}:0"
DEFAULT_API_URL = "https://www.emeraldai.beer/v1"
DEFAULT_MODEL = "gpt-4o-mini"
POLL_SECONDS = 30
MAX_TRACKERS = 20

logger = logging.getLogger("fpc_plugin.ai_market_radar")

_cardinal: Any | None = None
_pending_input: tuple[str, int | None] | None = None
_draft_task: dict[str, Any] = {}
_futures: set[Future[Any]] = set()
_poll_future: Future[Any] | None = None

CATEGORIES_CACHE: list[dict[str, Any]] = []
_categories_loaded_at: float = 0


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
    if service is None or getattr(service, "secrets", None) is None:
        return None
    return service.secrets


def _sync(awaitable: Any, timeout: float = 30) -> Any:
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
            logger.exception("Фоновая задача AI Market Radar завершилась с ошибкой")

    future.add_done_callback(done)
    return future


async def _ensure_schema() -> None:
    await _db().execute(
        """
        CREATE TABLE IF NOT EXISTS ai_radar_settings (
            telegram_id BIGINT PRIMARY KEY
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            api_base_url TEXT NOT NULL DEFAULT 'https://www.emeraldai.beer/v1',
            api_token_enc TEXT,
            model_id TEXT NOT NULL DEFAULT 'gpt-4o-mini',
            notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            poll_interval_seconds INT NOT NULL DEFAULT 30,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ai_radar_trackers (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL
                REFERENCES funpay_users(telegram_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            category_id INT NOT NULL,
            category_name TEXT NOT NULL,
            raw_prompt TEXT,
            title_keywords TEXT[] NOT NULL DEFAULT '{}',
            exclude_keywords TEXT[] NOT NULL DEFAULT '{}',
            min_price NUMERIC(12, 2),
            max_price NUMERIC(12, 2),
            only_auto_delivery BOOLEAN NOT NULL DEFAULT FALSE,
            only_online BOOLEAN NOT NULL DEFAULT FALSE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ai_radar_seen_offers (
            tracker_id BIGINT NOT NULL
                REFERENCES ai_radar_trackers(id) ON DELETE CASCADE,
            offer_id TEXT NOT NULL,
            price NUMERIC(12, 2) NOT NULL,
            seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tracker_id, offer_id)
        );

        CREATE INDEX IF NOT EXISTS ai_radar_trackers_tg_idx
            ON ai_radar_trackers (telegram_id, enabled);
        """
    )


async def _get_settings() -> dict[str, Any]:
    row = await _db().fetchrow(
        """
        INSERT INTO ai_radar_settings (telegram_id)
        VALUES ($1)
        ON CONFLICT (telegram_id) DO UPDATE SET updated_at = NOW()
        RETURNING *
        """,
        _telegram_id(),
    )
    return dict(row)


def _decrypt_token(enc_token: str | None) -> str | None:
    if not enc_token:
        return None
    box = _secret_box()
    if not box:
        return None
    try:
        return box.decrypt(enc_token)
    except Exception:
        return None


def _load_categories_sync() -> list[dict[str, Any]]:
    global CATEGORIES_CACHE, _categories_loaded_at
    now = datetime.now(timezone.utc).timestamp()
    if CATEGORIES_CACHE and (now - _categories_loaded_at < 3600):
        return CATEGORIES_CACHE

    req = urllib.request.Request(
        "https://funpay.com/",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
    )
    cats: list[dict[str, Any]] = []
    try:
        from bs4 import BeautifulSoup

        with urllib.request.urlopen(req, timeout=12) as resp:
            html_text = resp.read().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html_text, "html.parser")
        for it in soup.select(".promo-game-item"):
            game = it.select_one(".game-title")
            game_title = game.text.strip() if game else ""
            for a in it.select(".list-inline a"):
                href = a.get("href", "")
                m = re.search(r"/lots/(\d+)/", href)
                if m:
                    cid = int(m.group(1))
                    sub = a.text.strip()
                    cats.append({
                        "id": cid,
                        "game": game_title,
                        "sub": sub,
                        "name": f"{game_title} — {sub}",
                    })
        CATEGORIES_CACHE = cats
        _categories_loaded_at = now
    except Exception as e:
        logger.warning("Не удалось спарсить категории FunPay: %s", e)
    return CATEGORIES_CACHE


def _call_ai_parser(prompt: str, settings: dict[str, Any]) -> dict[str, Any]:
    api_token = _decrypt_token(settings.get("api_token_enc"))
    if not api_token:
        raise ValueError("API токен нейросети не задан в настройках плагина")

    base_url = (settings.get("api_base_url") or DEFAULT_API_URL).rstrip("/")
    model = settings.get("model_id") or DEFAULT_MODEL

    cats = _load_categories_sync()
    cats_sample = [
        {"id": c["id"], "name": c["name"]}
        for c in cats
        if any(w in c["name"].lower() for w in ["telegram", "discord", "steam", "spotify", "chatgpt", "youtube", "tiktok", "vpn", "аккаунты", "ключи"])
    ][:120]

    system_instruction = (
        "Ты — анализатор запросов для поиска и мониторинга товаров на FunPay.\n"
        "Пользователь пишет произвольный запрос на естественном языке, например:\n"
        "'Найди дешевые Telegram каналы с отлежкой до 150 рублей с автовыдачей'\n"
        "или 'Steam ключи рандом дешевле 5 руб' или 'Discord Nitro промо до 30р'.\n\n"
        "Тебе нужно понять параметры поиска и вернуть СТРОГО валидный JSON следующего формата:\n"
        "{\n"
        '  "category_id": 702,  // ID наиболее подходящей категории FunPay\n'
        '  "category_name": "Telegram — Каналы",\n'
        '  "title_keywords": ["отлежк", "канал"],  // обязательные ключевые слова (в нижнем регистре, подстроки)\n'
        '  "exclude_keywords": ["scam", "бан"],    // исключаемые слова (если есть)\n'
        '  "min_price": null,   // минимальная цена в рублях или null\n'
        '  "max_price": 150.0,  // максимальная цена в рублях или null\n'
        '  "only_auto_delivery": true, // true, если упомянута автовыдача\n'
        '  "only_online": false // true, если упомянуто только у продавцов онлайн\n'
        "}\n\n"
        "Популярные ID категорий:\n"
        "- Telegram Каналы: 702\n"
        "- Telegram Услуги/Накрутка: 703\n"
        "- Telegram Звёзды: 2418\n"
        "- Telegram Premium: 1391\n"
        "- Discord Nitro: 923\n"
        "- Discord Серверы: 922\n"
        "- Steam Ключи: 1008\n"
        "- Steam Аккаунты с играми: 89\n"
        "- ChatGPT Аккаунты: 1355\n"
        "- Spotify Premium: 1217\n"
        "- YouTube Услуги: 705\n"
        "- TikTok Услуги: 732\n\n"
        "Выбери наиболее подходящий category_id. Верни ТОЛЬКО JSON без markdown разметки."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Спарси запрос: {prompt}"},
        ],
        "temperature": 0.1,
    }

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
        "User-Agent": "FunPayMarketRadar/1.0",
    }

    class _SmartRedirectHandler(urllib.request.HTTPRedirectHandler):
        def http_error_308(self, req: Any, fp: Any, code: int, msg: str, hdrs: Any) -> Any:
            new_url = hdrs.get("Location")
            if not new_url:
                raise urllib.error.HTTPError(req.full_url, code, msg, hdrs, fp)
            new_req = urllib.request.Request(
                new_url, data=req.data, headers=dict(req.headers), method="POST"
            )
            return self.parent.open(new_req)

    opener = urllib.request.build_opener(_SmartRedirectHandler())
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with opener.open(req, timeout=25) as resp:
        res_json = json.loads(resp.read().decode("utf-8"))

    content = res_json["choices"][0]["message"]["content"].strip()
    match = re.search(r"(\{.*\})", content, re.DOTALL)
    if match:
        content = match.group(1)
    elif content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    return json.loads(content)


def _fetch_category_offers(category_id: int) -> list[dict[str, Any]]:
    url = f"https://funpay.com/lots/{category_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as resp:
        html_text = resp.read().decode("utf-8", errors="ignore")

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")
    items = soup.select(".tc-item")
    offers: list[dict[str, Any]] = []

    for it in items:
        href = it.get("href", "")
        offer_id = href.split("id=")[-1] if "id=" in href else None
        if not offer_id:
            continue

        desc_el = it.select_one(".tc-desc-text") or it.select_one(".tc-desc")
        desc = desc_el.text.strip() if desc_el else ""

        price_el = it.select_one(".tc-price")
        price_text = price_el.text.strip() if price_el else ""
        clean_p = re.sub(r"[^\d.]", "", price_text.replace(" ", "").replace(",", "."))
        try:
            price_val = float(clean_p) if clean_p else 0.0
        except ValueError:
            price_val = 0.0

        seller_el = it.select_one(".media-user-name")
        seller = seller_el.text.strip() if seller_el else ""

        auto = bool(it.select_one(".auto-dl"))
        online = it.get("data-online") == "1"

        offers.append({
            "offer_id": offer_id,
            "url": f"https://funpay.com/lots/offer?id={offer_id}",
            "title": desc,
            "price": price_val,
            "seller": seller,
            "auto": auto,
            "online": online,
        })
    return offers


async def _run_radar_scan() -> None:
    """Периодический скан активных трекеров."""
    try:
        trackers = await _db().fetch(
            """
            SELECT t.*, s.notifications_enabled, s.telegram_id as user_tg
            FROM ai_radar_trackers t
            JOIN ai_radar_settings s ON t.telegram_id = s.telegram_id
            WHERE t.enabled = TRUE AND s.notifications_enabled = TRUE
            """
        )
        if not trackers:
            return

        by_cat: dict[int, list[Any]] = {}
        for tr in trackers:
            by_cat.setdefault(tr["category_id"], []).append(tr)

        for cat_id, cat_trackers in by_cat.items():
            try:
                loop = asyncio.get_running_loop()
                offers = await loop.run_in_executor(None, _fetch_category_offers, cat_id)
            except Exception as e:
                logger.warning("Ошибка парсинга категории %s: %s", cat_id, e)
                continue

            for tr in cat_trackers:
                min_p = float(tr["min_price"]) if tr["min_price"] is not None else None
                max_p = float(tr["max_price"]) if tr["max_price"] is not None else None
                auto_only = bool(tr["only_auto_delivery"])
                online_only = bool(tr["only_online"])
                keywords = [k.lower() for k in tr["title_keywords"] or []]
                exclude = [k.lower() for k in tr["exclude_keywords"] or []]

                for off in offers:
                    p = off["price"]
                    if min_p is not None and p < min_p:
                        continue
                    if max_p is not None and p > max_p:
                        continue
                    if auto_only and not off["auto"]:
                        continue
                    if online_only and not off["online"]:
                        continue

                    t_lower = off["title"].lower()
                    if keywords and not all(k in t_lower for k in keywords):
                        continue
                    if exclude and any(k in t_lower for k in exclude):
                        continue

                    # Проверяем, видели ли уже этот лот для этого трекера
                    exists = await _db().fetchval(
                        """
                        SELECT 1 FROM ai_radar_seen_offers
                        WHERE tracker_id = $1 AND offer_id = $2
                        """,
                        tr["id"],
                        off["offer_id"],
                    )
                    if exists:
                        continue

                    # Сохраняем как просмотренный
                    await _db().execute(
                        """
                        INSERT INTO ai_radar_seen_offers (tracker_id, offer_id, price)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (tracker_id, offer_id) DO NOTHING
                        """,
                        tr["id"],
                        off["offer_id"],
                        off["price"],
                    )

                    # Отправляем уведомление в Telegram владельцу
                    msg_text = (
                        f"🚨 <b>AI Radar: найден выгодный лот!</b>\n\n"
                        f"🎯 <b>Трекер:</b> {html.escape(tr['title'])}\n"
                        f"📂 <b>Категория:</b> {html.escape(tr['category_name'])}\n"
                        f"🏷 <b>Лот:</b> {html.escape(off['title'])}\n"
                        f"💰 <b>Цена:</b> <code>{off['price']:.2f} ₽</code>\n"
                        f"👤 <b>Продавец:</b> {html.escape(off['seller'])}{' 🟢' if off['online'] else ''}\n"
                        f"⚡ <b>Автовыдача:</b> {'Да' if off['auto'] else 'Нет'}\n\n"
                        f"🔗 <a href=\"{off['url']}\">Открыть и купить на FunPay</a>"
                    )

                    kb = InlineKeyboardMarkup()
                    kb.add(
                        InlineKeyboardButton(
                            text="🛒 Купить на FunPay", url=off["url"]
                        )
                    )
                    try:
                        _bot().send_message(
                            tr["telegram_id"],
                            msg_text,
                            parse_mode="HTML",
                            reply_markup=kb,
                            disable_web_page_preview=False,
                        )
                    except Exception as e:
                        logger.warning("Не удалось отправить уведомление по лоту %s: %s", off["offer_id"], e)
    except Exception as e:
        logger.exception("Ошибка в фоновом цикле радара: %s", e)


async def _radar_loop() -> None:
    logger.info("Запущен фоновый мониторинг AI Market Radar")
    while True:
        try:
            await _run_radar_scan()
        except Exception:
            logger.exception("Ошибка тика AI Market Radar")
        await asyncio.sleep(POLL_SECONDS)


def _render_menu(settings: dict[str, Any], trackers: list[Any]) -> tuple[str, InlineKeyboardMarkup]:
    token = _decrypt_token(settings.get("api_token_enc"))
    token_lbl = "••••" + token[-4:] if token and len(token) >= 4 else ("задан" if token else "❌ не задан")
    notif_lbl = "🔔 Включены" if settings.get("notifications_enabled") else "🔕 Выключены"

    text = (
        "🤖 <b>AI Market Radar — Мониторинг и перепродажа</b>\n\n"
        "Плагин в реальном времени парсит категории FunPay по вашим запросам. "
        "Вы можете написать запрос на обычном русском языке (например: "
        "<i>«Дешевые тг каналы до 100р»</i> или <i>«Steam ключи с автовыдачей до 5р»</i>), "
        "ИИ сам подберёт категорию и ключевые слова, а радар пришлёт ссылку на покупку сразу, "
        "как только лот появится!\n\n"
        f"🔑 <b>AI API Gateway:</b> <code>{html.escape(settings.get('api_base_url') or DEFAULT_API_URL)}</code>\n"
        f"🔑 <b>AI Токен:</b> <code>{html.escape(token_lbl)}</code>\n"
        f"🧠 <b>Модель:</b> <code>{html.escape(settings.get('model_id') or DEFAULT_MODEL)}</code>\n"
        f"📢 <b>Уведомления:</b> {notif_lbl}\n\n"
        f"🎯 <b>Активных трекеров:</b> {len(trackers)}/{MAX_TRACKERS}\n"
    )

    rows: list[list[tuple[str, str]]] = []
    rows.append([("➕ Добавить AI-трекер", f"{CALLBACK_PREFIX}add_ai")])

    for tr in trackers:
        status = "🟢" if tr["enabled"] else "⏸"
        rows.append([(f"{status} {tr['title']} ({tr['category_name']})", f"{CALLBACK_PREFIX}view:{tr['id']}")])

    rows.append([
        ("🔔 Вкл/Выкл уведомления", f"{CALLBACK_PREFIX}toggle_notif"),
        ("⚙️ Настройки AI", f"{CALLBACK_PREFIX}settings"),
    ])
    rows.append([("🔄 Обновить", f"{CALLBACK_PREFIX}refresh")])

    return text, _markup(*rows)


def _render_tracker_view(tr: Any) -> tuple[str, InlineKeyboardMarkup]:
    min_p = f"{tr['min_price']} ₽" if tr["min_price"] is not None else "любая"
    max_p = f"{tr['max_price']} ₽" if tr["max_price"] is not None else "без лимита"
    kw = ", ".join(tr["title_keywords"] or []) or "нет (любые)"
    ex = ", ".join(tr["exclude_keywords"] or []) or "нет"
    auto = "Да" if tr["only_auto_delivery"] else "Не важно"
    online = "Да" if tr["only_online"] else "Не важно"
    status = "🟢 Включен (парсится)" if tr["enabled"] else "⏸ Приостановлен"

    text = (
        f"🎯 <b>Трекер: {html.escape(tr['title'])}</b>\n\n"
        f"📂 <b>Категория:</b> {html.escape(tr['category_name'])} (ID: <code>{tr['category_id']}</code>)\n"
        f"📝 <b>Исходный запрос:</b> <i>{html.escape(tr['raw_prompt'] or 'Ручной')}</i>\n"
        f"🔍 <b>Ключевые слова:</b> <code>{html.escape(kw)}</code>\n"
        f"🚫 <b>Исключать слова:</b> <code>{html.escape(ex)}</code>\n"
        f"💰 <b>Диапазон цены:</b> от <code>{min_p}</code> до <code>{max_p}</code>\n"
        f"⚡ <b>Только автовыдача:</b> {auto}\n"
        f"🟢 <b>Только продавцы Online:</b> {online}\n"
        f"📊 <b>Статус:</b> {status}\n"
    )

    t_btn = "⏸ Приостановить" if tr["enabled"] else "▶️ Включить"
    rows: list[list[tuple[str, str]]] = [
        [(t_btn, f"{CALLBACK_PREFIX}toggle_tr:{tr['id']}")],
        [("🔍 Проверить лоты прямо сейчас", f"{CALLBACK_PREFIX}check_now:{tr['id']}")],
        [("🗑 Удалить трекер", f"{CALLBACK_PREFIX}del_tr:{tr['id']}")],
        [("⬅️ Назад к списку", f"{CALLBACK_PREFIX}refresh")],
    ]
    return text, _markup(*rows)


def _render_settings_view(settings: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    token = _decrypt_token(settings.get("api_token_enc"))
    token_lbl = "••••" + token[-4:] if token and len(token) >= 4 else ("задан" if token else "❌ не задан")

    text = (
        "⚙️ <b>Настройки AI для парсера</b>\n\n"
        "Для разбора естественных запросов используется любой OpenAI-совместимый API "
        "(например, шлюз <code>https://emeraldai.beer/v1</code> или официальный OpenAI / OpenRouter).\n\n"
        f"🌐 <b>Base URL:</b> <code>{html.escape(settings.get('api_base_url') or DEFAULT_API_URL)}</code>\n"
        f"🔑 <b>API Токен:</b> <code>{html.escape(token_lbl)}</code>\n"
        f"🧠 <b>Модель:</b> <code>{html.escape(settings.get('model_id') or DEFAULT_MODEL)}</code>\n"
    )

    rows: list[list[tuple[str, str]]] = [
        [("🔑 Задать API-токен", f"{CALLBACK_PREFIX}set:token")],
        [("🌐 Изменить Base URL", f"{CALLBACK_PREFIX}set:url")],
        [("🧠 Изменить Модель", f"{CALLBACK_PREFIX}set:model")],
        [("⬅️ Назад", f"{CALLBACK_PREFIX}refresh")],
    ]
    return text, _markup(*rows)


def _show_settings(chat_id: int) -> None:
    settings = _sync(_get_settings())
    trackers = _sync(_db().fetch(
        "SELECT * FROM ai_radar_trackers WHERE telegram_id = $1 ORDER BY id DESC",
        _telegram_id()
    ))
    text, markup = _render_menu(settings, trackers)
    _bot().send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def pre_init(cardinal: Any) -> None:
    global _cardinal
    _cardinal = cardinal
    _sync(_ensure_schema())
    bot = cardinal.telegram.bot
    bot.register_callback_query_handler(
        _on_callback,
        func=lambda call: str(getattr(call, "data", "") or "") == SETTINGS_CALLBACK
        or str(getattr(call, "data", "") or "").startswith(CALLBACK_PREFIX),
    )
    bot.register_message_handler(
        _on_setting_message,
        content_types=["text"],
        func=lambda _message: _pending_input is not None,
    )


def post_start(cardinal: Any) -> None:
    global _cardinal, _poll_future
    _cardinal = cardinal
    _sync(_ensure_schema())
    if _poll_future is None or _poll_future.done():
        _poll_future = _spawn(_radar_loop())
    logger.info("AI Market Radar успешно запущен")


def pre_stop(cardinal: Any) -> None:
    global _cardinal, _poll_future, _pending_input
    _pending_input = None
    if _poll_future and not _poll_future.done():
        _poll_future.cancel()
    for f in list(_futures):
        if not f.done():
            f.cancel()
    _futures.clear()
    logger.info("AI Market Radar остановлен")


def on_delete(cardinal: Any, callback: Any = None) -> None:
    pre_stop(cardinal)
    try:
        _sync(
            _db().execute(
                """
                DELETE FROM ai_radar_seen_offers WHERE tracker_id IN (
                    SELECT id FROM ai_radar_trackers WHERE telegram_id = $1
                );
                DELETE FROM ai_radar_trackers WHERE telegram_id = $1;
                DELETE FROM ai_radar_settings WHERE telegram_id = $1;
                """,
                _telegram_id(),
            )
        )
    except Exception:
        pass


def open_settings(chat_id: int) -> None:
    _show_settings(chat_id)


def _on_callback(call: Any) -> None:
    global _pending_input
    data = str(getattr(call, "data", ""))
    chat_id = int(call.message.chat.id)
    message_id = int(call.message.message_id)

    try:
        _bot().answer_callback_query(call.id)
    except Exception:
        pass

    if data == SETTINGS_CALLBACK or data == f"{CALLBACK_PREFIX}refresh":
        settings = _sync(_get_settings())
        trackers = _sync(_db().fetch(
            "SELECT * FROM ai_radar_trackers WHERE telegram_id = $1 ORDER BY id DESC",
            _telegram_id()
        ))
        text, markup = _render_menu(settings, trackers)
        try:
            _bot().edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            _bot().send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        return

    if not data.startswith(CALLBACK_PREFIX):
        return

    action = data[len(CALLBACK_PREFIX):]

    if action == "settings":
        settings = _sync(_get_settings())
        text, markup = _render_settings_view(settings)
        _bot().edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)

    elif action == "toggle_notif":
        settings = _sync(_get_settings())
        new_val = not settings.get("notifications_enabled")
        _sync(_db().execute(
            "UPDATE ai_radar_settings SET notifications_enabled = $1 WHERE telegram_id = $2",
            new_val, _telegram_id()
        ))
        _on_callback(type("Call", (), {"data": f"{CALLBACK_PREFIX}refresh", "message": call.message, "id": call.id})())

    elif action.startswith("view:"):
        tr_id = int(action.split(":")[1])
        tr = _sync(_db().fetchrow(
            "SELECT * FROM ai_radar_trackers WHERE id = $1 AND telegram_id = $2",
            tr_id, _telegram_id()
        ))
        if not tr:
            _bot().answer_callback_query(call.id, "Трекер не найден")
            return
        text, markup = _render_tracker_view(tr)
        _bot().edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=markup)

    elif action.startswith("toggle_tr:"):
        tr_id = int(action.split(":")[1])
        tr = _sync(_db().fetchrow(
            "SELECT enabled FROM ai_radar_trackers WHERE id = $1 AND telegram_id = $2",
            tr_id, _telegram_id()
        ))
        if tr:
            new_st = not tr["enabled"]
            _sync(_db().execute(
                "UPDATE ai_radar_trackers SET enabled = $1, updated_at = NOW() WHERE id = $2",
                new_st, tr_id
            ))
        _on_callback(type("Call", (), {"data": f"{CALLBACK_PREFIX}view:{tr_id}", "message": call.message, "id": call.id})())

    elif action.startswith("del_tr:"):
        tr_id = int(action.split(":")[1])
        _sync(_db().execute(
            "DELETE FROM ai_radar_trackers WHERE id = $1 AND telegram_id = $2",
            tr_id, _telegram_id()
        ))
        _bot().answer_callback_query(call.id, "Трекер удалён")
        _on_callback(type("Call", (), {"data": f"{CALLBACK_PREFIX}refresh", "message": call.message, "id": call.id})())

    elif action.startswith("check_now:"):
        tr_id = int(action.split(":")[1])
        tr = _sync(_db().fetchrow(
            "SELECT * FROM ai_radar_trackers WHERE id = $1 AND telegram_id = $2",
            tr_id, _telegram_id()
        ))
        if not tr:
            _bot().answer_callback_query(call.id, "Трекер не найден")
            return
        
        # Ручная разовая проверка с выдачей топ-5 подходящих лотов
        try:
            offers = _fetch_category_offers(tr["category_id"])
            min_p = float(tr["min_price"]) if tr["min_price"] is not None else None
            max_p = float(tr["max_price"]) if tr["max_price"] is not None else None
            auto_only = bool(tr["only_auto_delivery"])
            online_only = bool(tr["only_online"])
            keywords = [k.lower() for k in tr["title_keywords"] or []]
            exclude = [k.lower() for k in tr["exclude_keywords"] or []]

            matched: list[dict[str, Any]] = []
            for off in offers:
                p = off["price"]
                if min_p is not None and p < min_p:
                    continue
                if max_p is not None and p > max_p:
                    continue
                if auto_only and not off["auto"]:
                    continue
                if online_only and not off["online"]:
                    continue
                t_lower = off["title"].lower()
                if keywords and not all(k in t_lower for k in keywords):
                    continue
                if exclude and any(k in t_lower for k in exclude):
                    continue
                matched.append(off)

            if not matched:
                _bot().send_message(chat_id, "🔍 По вашим фильтрам прямо сейчас подходящих лотов в категории нет.")
            else:
                lines = [f"🔍 <b>Найдено лотов прямо сейчас: {len(matched)}</b>\nПоказываю топ самых выгодных:"]
                for m_lot in matched[:5]:
                    lines.append(
                        f"• <b>{m_lot['price']:.2f} ₽</b> | {html.escape(m_lot['title'])}\n"
                        f"  (Продавец: {html.escape(m_lot['seller'])}, Автовыдача: {'Да' if m_lot['auto'] else 'Нет'})\n"
                        f"  👉 <a href=\"{m_lot['url']}\">Купить</a>"
                    )
                _bot().send_message(chat_id, "\n\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            _bot().send_message(chat_id, f"❌ Ошибка проверки: {e}")

    elif action == "add_ai":
        count = _sync(_db().fetchval(
            "SELECT count(*) FROM ai_radar_trackers WHERE telegram_id = $1",
            _telegram_id()
        ))
        if count >= MAX_TRACKERS:
            _bot().answer_callback_query(call.id, f"Достигнут лимит {MAX_TRACKERS} трекеров")
            return

        settings = _sync(_get_settings())
        token = _decrypt_token(settings.get("api_token_enc"))
        if not token:
            _bot().answer_callback_query(call.id, "Сначала укажите AI токен в Настройках!", show_alert=True)
            return

        _pending_input = ("ai_prompt", None)
        msg = (
            "🤖 <b>Создание нового трекера через ИИ</b>\n\n"
            "Напишите произвольный запрос, что и за сколько вы хотите отслеживать.\n\n"
            "<i>Примеры:</i>\n"
            "• <code>Telegram каналы с отлежкой дешевле 150 рублей</code>\n"
            "• <code>Случайные Steam ключи с автовыдачей до 10 рублей</code>\n"
            "• <code>Discord Nitro Full гифт ссылка до 60 руб</code>\n"
            "• <code>Аккаунты ChatGPT личные до 120р</code>\n\n"
            "Отправьте ваш запрос ответным сообщением:"
        )
        markup = _markup([("❌ Отмена", f"{CALLBACK_PREFIX}refresh")])
        _bot().edit_message_text(msg, chat_id, message_id, parse_mode="HTML", reply_markup=markup)

    elif action == "set:token":
        _pending_input = ("token", None)
        msg = "🔑 <b>Отправьте ваш API токен нейросети</b>\n(Сообщение с токеном будет сразу же удалено и сохранено в зашифрованном виде):"
        markup = _markup([("❌ Отмена", f"{CALLBACK_PREFIX}settings")])
        _bot().edit_message_text(msg, chat_id, message_id, parse_mode="HTML", reply_markup=markup)

    elif action == "set:url":
        _pending_input = ("url", None)
        msg = (
            f"🌐 <b>Отправьте Base URL OpenAI-совместимого API</b>\n\n"
            f"По умолчанию: <code>{DEFAULT_API_URL}</code>\n"
            f"Для официального OpenAI: <code>https://api.openai.com/v1</code>"
        )
        markup = _markup([("❌ Отмена", f"{CALLBACK_PREFIX}settings")])
        _bot().edit_message_text(msg, chat_id, message_id, parse_mode="HTML", reply_markup=markup)

    elif action == "set:model":
        _pending_input = ("model", None)
        msg = f"🧠 <b>Отправьте название модели</b>\n(например, <code>gpt-4o-mini</code>, <code>gpt-4o</code>, <code>claude-3-5-haiku-20241022</code>):"
        markup = _markup([("❌ Отмена", f"{CALLBACK_PREFIX}settings")])
        _bot().edit_message_text(msg, chat_id, message_id, parse_mode="HTML", reply_markup=markup)


def _on_setting_message(message: Any) -> bool:
    global _pending_input
    if not _pending_input:
        return False

    chat_id = int(message.chat.id)
    text = (getattr(message, "text", "") or "").strip()
    kind, _ = _pending_input
    _pending_input = None

    if kind == "token":
        try:
            _bot().delete_message(chat_id, message.message_id)
        except Exception:
            pass

        box = _secret_box()
        if not box:
            _bot().send_message(chat_id, "❌ Хранилище шифрования ключей недоступно.")
            return True

        enc = box.encrypt(text)
        _sync(_db().execute(
            "UPDATE ai_radar_settings SET api_token_enc = $1, updated_at = NOW() WHERE telegram_id = $2",
            enc, _telegram_id()
        ))
        _bot().send_message(chat_id, "✅ API токен нейросети успешно сохранён и зашифрован!")
        _show_settings(chat_id)
        return True

    elif kind == "url":
        url = text.rstrip("/")
        if not (url.startswith("http://") or url.startswith("https://")):
            _bot().send_message(chat_id, "❌ URL должен начинаться с https:// или http://")
            _show_settings(chat_id)
            return True

        _sync(_db().execute(
            "UPDATE ai_radar_settings SET api_base_url = $1, updated_at = NOW() WHERE telegram_id = $2",
            url, _telegram_id()
        ))
        _bot().send_message(chat_id, f"✅ Base URL обновлён: <code>{html.escape(url)}</code>", parse_mode="HTML")
        _show_settings(chat_id)
        return True

    elif kind == "model":
        model = text.strip()
        if not model:
            _bot().send_message(chat_id, "❌ Модель не может быть пустой")
            _show_settings(chat_id)
            return True

        _sync(_db().execute(
            "UPDATE ai_radar_settings SET model_id = $1, updated_at = NOW() WHERE telegram_id = $2",
            model, _telegram_id()
        ))
        _bot().send_message(chat_id, f"✅ Модель обновлена: <code>{html.escape(model)}</code>", parse_mode="HTML")
        _show_settings(chat_id)
        return True

    elif kind == "ai_prompt":
        status_msg = _bot().send_message(chat_id, "⏳ <i>ИИ анализирует ваш запрос и структуру категорий FunPay...</i>", parse_mode="HTML")
        try:
            settings = _sync(_get_settings())
            parsed = _call_ai_parser(text, settings)

            cat_id = int(parsed.get("category_id") or 702)
            cat_name = str(parsed.get("category_name") or f"Категория {cat_id}")
            kw = parsed.get("title_keywords") or []
            ex = parsed.get("exclude_keywords") or []
            min_p = parsed.get("min_price")
            max_p = parsed.get("max_price")
            auto_only = bool(parsed.get("only_auto_delivery", False))
            online_only = bool(parsed.get("only_online", False))

            title = f"{cat_name} (до {max_p}₽)" if max_p else cat_name

            # Создаём трекер в базе
            tr_id = _sync(_db().fetchval(
                """
                INSERT INTO ai_radar_trackers (
                    telegram_id, title, category_id, category_name, raw_prompt,
                    title_keywords, exclude_keywords, min_price, max_price,
                    only_auto_delivery, only_online, enabled
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, TRUE)
                RETURNING id
                """,
                _telegram_id(), title, cat_id, cat_name, text,
                kw, ex, min_p, max_p, auto_only, online_only
            ))

            try:
                _bot().delete_message(chat_id, status_msg.message_id)
            except Exception:
                pass

            res_text = (
                f"🎉 <b>AI Трекер успешно создан и запущен!</b>\n\n"
                f"🎯 <b>Название:</b> {html.escape(title)}\n"
                f"📂 <b>Категория:</b> {html.escape(cat_name)} (ID: <code>{cat_id}</code>)\n"
                f"🔍 <b>Ключевые слова:</b> <code>{html.escape(', '.join(kw) or 'все')}</code>\n"
                f"💰 <b>Цена:</b> до <code>{max_p or 'без ограничений'} ₽</code>\n"
                f"⚡ <b>Только с автовыдачей:</b> {'Да' if auto_only else 'Нет'}\n\n"
                f"📡 <i>Радар мониторит FunPay каждые 30 секунд. Как только появится подходящий лот, вы мгновенно получите уведомление со ссылкой для выкупа!</i>"
            )
            kb = _markup([
                [("🔍 Проверить подходящие лоты сейчас", f"{CALLBACK_PREFIX}check_now:{tr_id}")],
                [("⬅️ В меню радара", f"{CALLBACK_PREFIX}refresh")],
            ])
            _bot().send_message(chat_id, res_text, parse_mode="HTML", reply_markup=kb)
            return True

        except Exception as e:
            logger.exception("Ошибка AI анализа запроса")
            try:
                _bot().delete_message(chat_id, status_msg.message_id)
            except Exception:
                pass
            _bot().send_message(
                chat_id,
                f"❌ Не удалось обработать запрос через ИИ: {e}\n\nПроверьте API-токен и доступность модели в Настройках.",
                reply_markup=_markup([("⚙️ Настройки AI", f"{CALLBACK_PREFIX}settings")])
            )
            return True

    return False


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
BIND_TO_NEW_ORDER = []
BIND_TO_ORDERS_LIST_CHANGED = []
BIND_TO_ORDER_STATUS_CHANGED = []
BIND_TO_PRE_DELIVERY = []
BIND_TO_POST_DELIVERY = []
BIND_TO_PRE_LOTS_RAISE = []
BIND_TO_POST_LOTS_RAISE = []
BIND_TO_TELETHON_READY = []
BIND_TO_TELETHON_DISCONNECTED = []
BIND_TO_DELETE = on_delete

