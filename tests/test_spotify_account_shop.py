import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ready_plugins import SpotifyAccountShop as plugin


def _async_value(value):
    async def inner(*_args, **_kwargs):
        return value

    return inner()


def _settings(**overrides):
    base = {
        "followup_enabled": True,
        "followup_delay_minutes": 60,
        "review_url": "https://funpay.com/orders/",
        "delivery_template": None,
        "followup_template": None,
        "low_stock_threshold": 2,
    }
    base.update(overrides)
    return base


def test_plugin_metadata_is_valid():
    assert plugin.NAME == "Spotify Account Shop"
    assert plugin.SETTINGS_PAGE is True
    assert plugin.TELETHON is False
    assert plugin.UUID == "9d378672-7534-41b2-ab16-87057c697f37"
    assert plugin.BIND_TO_NEW_ORDER and plugin.BIND_TO_DELETE
    assert plugin.SETTINGS_CALLBACK == f"47:{plugin.UUID}:0"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("buyer@example.com:secret", ("buyer@example.com", "secret", None)),
        (
            "buyer@example.com:secret:SPOT-2026-1",
            ("buyer@example.com", "secret", "SPOT-2026-1"),
        ),
        (
            " buyer@example.com : sec ret : key-2 ",
            ("buyer@example.com", "sec ret", "key-2"),
        ),
        (
            "buyer@example.com:pass:word:SPOT-3",
            ("buyer@example.com", "pass:word", "SPOT-3"),
        ),
    ],
)
def test_parse_stock_line_supports_documented_formats(line, expected):
    assert plugin._parse_stock_line(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "buyer.example.com:secret",
        "buyer@example.com",
        "buyer@example.com:",
        ":secret",
    ],
)
def test_parse_stock_line_rejects_broken_values(line):
    with pytest.raises(ValueError):
        plugin._parse_stock_line(line)


def test_parse_stock_lines_deduplicates_and_reports_errors():
    text = "\n".join(
        [
            "one@example.com:first",
            "one@example.com:first",
            "broken-line",
            "two@example.com:second:KEY-1",
            "",
        ]
    )
    entries, errors, duplicates = plugin._parse_stock_lines(text)
    assert entries == [
        ("one@example.com", "first", None),
        ("two@example.com", "second", "KEY-1"),
    ]
    assert duplicates == 1
    assert len(errors) == 1 and "строка 3" in errors[0]


def test_delivery_render_includes_key_line_only_when_key_present():
    item = {
        "email": "buyer@example.com",
        "password": "Passw0rd!",
        "activation_key": "SPOT-KEY",
    }
    order = {"order_id": "ORDER-1", "buyer_username": "buyer"}

    text = plugin._render_delivery(
        plugin.DEFAULT_DELIVERY_TEMPLATE, item=item, days=30, order=order
    )
    assert "buyer@example.com" in text
    assert "Passw0rd!" in text
    assert "SPOT-KEY" in text
    assert "30 дн." in text

    without_key = dict(item, activation_key=None)
    text = plugin._render_delivery(
        plugin.DEFAULT_DELIVERY_TEMPLATE,
        item=without_key,
        days=30,
        order=order,
    )
    assert "SPOT-KEY" not in text
    assert "Ключ активации" not in text


def test_delivery_render_rejects_broken_ciphertext(monkeypatch):
    class BrokenSecrets:
        @staticmethod
        def decrypt(_value):
            raise ValueError("cannot decrypt")

    monkeypatch.setattr(
        plugin,
        "_secret_box",
        lambda: BrokenSecrets(),
    )
    item = {
        "email": "buyer@example.com",
        "password": plugin.SECRET_PREFIX + "cipher",
        "activation_key": None,
    }
    with pytest.raises(ValueError):
        plugin._render_delivery(
            plugin.DEFAULT_DELIVERY_TEMPLATE,
            item=item,
            days=30,
            order={"order_id": "1"},
        )


def test_followup_render_uses_confirm_hint_and_review_url():
    order = {
        "order_id": "ORDER-9",
        "buyer_username": "buyer",
        "subscription_days": 90,
    }
    settings = _settings(review_url="https://funpay.com/orders/")
    text = plugin._render_followup(
        plugin.DEFAULT_FOLLOWUP_TEMPLATE,
        order=order,
        email="buyer@example.com",
        closed=False,
        settings=settings,
    )
    assert "ORDER-9" in text
    assert "buyer@example.com" in text
    assert "https://funpay.com/orders/" in text
    assert plugin.CONFIRM_PAID_HINT in text

    closed = plugin._render_followup(
        plugin.DEFAULT_FOLLOWUP_TEMPLATE,
        order=order,
        email="buyer@example.com",
        closed=True,
        settings=settings,
    )
    assert plugin.CONFIRM_CLOSED_HINT in closed


def test_delivery_template_requires_email_and_password():
    with pytest.raises(ValueError):
        plugin._validate_delivery_template("Просто текст без плейсхолдеров")
    with pytest.raises(ValueError):
        plugin._validate_delivery_template(
            "Почта {email}, но без пароля и без достаточной длины"
        )
    template = "Почта: {email}\nПароль: {password}\nСпасибо за покупку!"
    assert plugin._validate_delivery_template(template) == template


def test_normalize_title_ignores_amount_suffix():
    assert plugin._normalize_title("Spotify Premium, 1 шт.") == "spotify premium"
    assert plugin._normalize_title("  Spotify   Premium  ") == "spotify premium"


def test_order_lot_id_prefers_attribute_then_html():
    class Order:
        lot_id = 123
        html = ""

    assert plugin._order_lot_id(Order()) == "123"

    class WidgetOrder:
        html = '<a href="/lots/offer?id=555">Spotify</a>'

    assert plugin._order_lot_id(WidgetOrder()) == "555"
    assert plugin._order_lot_id(SimpleNamespace(html="")) is None


def test_match_position_prefers_bound_lot_id(monkeypatch):
    positions = [
        {"id": 1, "title": "30 days", "enabled": True},
        {"id": 2, "title": "90 days", "enabled": True},
    ]
    bindings = [
        {"lot_id": "100", "position_id": 1, "lot_title": "Spotify Premium"},
        {"lot_id": "200", "position_id": 2, "lot_title": "Spotify Premium 90"},
    ]
    monkeypatch.setattr(plugin, "_positions", lambda: _async_value(positions))
    monkeypatch.setattr(plugin, "_all_bindings", lambda: _async_value(bindings))

    matched = asyncio.run(plugin._match_position("200", "Spotify Premium"))
    assert matched["id"] == 2

    by_title = asyncio.run(
        plugin._match_position(None, "Spotify Premium 90, 1 шт.")
    )
    assert by_title["id"] == 2

    assert asyncio.run(plugin._match_position("999", "Другой лот")) is None


def test_match_position_ignores_disabled_position(monkeypatch):
    monkeypatch.setattr(
        plugin,
        "_positions",
        lambda: _async_value(
            [{"id": 5, "title": "Off", "enabled": False}]
        ),
    )
    monkeypatch.setattr(
        plugin,
        "_all_bindings",
        lambda: _async_value(
            [{"lot_id": "77", "position_id": 5, "lot_title": "Spotify"}]
        ),
    )
    assert asyncio.run(plugin._match_position("77", "Spotify")) is None


class FakeDB:
    def __init__(self, queued_order, delivering_order):
        self.queued_order = queued_order
        self.delivering_order = delivering_order
        self.calls = []

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if normalized.startswith("SELECT * FROM spotify_accounts_orders"):
            return self.queued_order
        if normalized.startswith("UPDATE spotify_accounts_orders"):
            return self.delivering_order
        return None


def test_fulfill_order_delivers_and_schedules_followup(monkeypatch):
    queued = {
        "id": 1,
        "order_id": "ORDER-1",
        "position_id": 7,
        "position_title": "Spotify 30",
        "subscription_days": 30,
        "buyer_username": "buyer",
        "chat_id": "users-1-2",
        "chat_name": "buyer",
        "status": "queued",
        "item_id": None,
        "followup_attempts": 0,
    }
    delivering = dict(queued, status="delivering", item_id=42)
    item = {
        "id": 42,
        "email": "buyer@example.com",
        "password": "Passw0rd!",
        "activation_key": "KEY-1",
    }
    sent = []
    updates = []
    sold = []
    notifications = []

    async def fake_send(order, text):
        sent.append((order["order_id"], text))

    async def fake_update(order_db_id, **values):
        updates.append((order_db_id, values))

    async def fake_sold(item_id, order_id):
        sold.append((item_id, order_id))

    async def fake_notify(text, **_kwargs):
        notifications.append(text)

    async def fake_alert(_position_id):
        return None

    monkeypatch.setattr(plugin, "_telegram_id", lambda: 1)
    db = FakeDB(queued, delivering)
    monkeypatch.setattr(plugin, "_db", lambda: db)
    monkeypatch.setattr(plugin, "_claim_items", lambda *_a: _async_value([item]))
    monkeypatch.setattr(plugin, "_settings", lambda: _async_value(_settings()))
    monkeypatch.setattr(plugin, "_funpay_send", fake_send)
    monkeypatch.setattr(plugin, "_update_order", fake_update)
    monkeypatch.setattr(plugin, "_mark_item_sold", fake_sold)
    monkeypatch.setattr(plugin, "_notify_owner", fake_notify)
    monkeypatch.setattr(plugin, "_maybe_alert_low_stock", fake_alert)

    result = asyncio.run(plugin._fulfill_order(1))

    assert result == "delivered"
    assert sent and sent[0][0] == "ORDER-1"
    assert "buyer@example.com" in sent[0][1]
    assert "Passw0rd!" in sent[0][1]
    assert "KEY-1" in sent[0][1]
    assert sold == [(42, "ORDER-1")]
    delivered = next(
        values for _, values in updates if values.get("status") == "delivered"
    )
    assert delivered["followup_status"] == "pending"
    claiming = next(
        args
        for query, args in db.calls
        if query.startswith("UPDATE spotify_accounts_orders")
    )
    assert claiming[2] == [42]
    delay = delivered["followup_at"] - datetime.now(timezone.utc)
    assert timedelta(minutes=55) < delay < timedelta(minutes=65)
    assert notifications and "выдан" in notifications[0]


def test_fulfill_order_returns_item_when_send_fails(monkeypatch):
    queued = {
        "id": 2,
        "order_id": "ORDER-2",
        "position_id": 7,
        "position_title": "Spotify 30",
        "subscription_days": 30,
        "buyer_username": "buyer",
        "chat_id": "users-1-2",
        "chat_name": "buyer",
        "status": "queued",
        "item_id": None,
        "followup_attempts": 0,
    }
    delivering = dict(queued, status="delivering", item_id=43)
    item = {
        "id": 43,
        "email": "buyer@example.com",
        "password": "Passw0rd!",
        "activation_key": None,
    }
    released = []
    updates = []
    notifications = []

    async def failing_send(_order, _text):
        raise RuntimeError("FunPay недоступен")

    async def fake_release(item_id):
        released.append(item_id)

    async def fake_update(order_db_id, **values):
        updates.append((order_db_id, values))

    async def fake_notify(text, **_kwargs):
        notifications.append(text)

    monkeypatch.setattr(plugin, "_telegram_id", lambda: 1)
    db = FakeDB(queued, delivering)
    monkeypatch.setattr(plugin, "_db", lambda: db)
    monkeypatch.setattr(plugin, "_claim_items", lambda *_a: _async_value([item]))
    monkeypatch.setattr(plugin, "_settings", lambda: _async_value(_settings()))
    monkeypatch.setattr(plugin, "_funpay_send", failing_send)
    monkeypatch.setattr(plugin, "_release_item", fake_release)
    monkeypatch.setattr(plugin, "_update_order", fake_update)
    monkeypatch.setattr(plugin, "_notify_owner", fake_notify)

    result = asyncio.run(plugin._fulfill_order(2))

    assert result == "error"
    assert released == [43]
    failed = next(values for _, values in updates if values.get("status") == "failed")
    assert "FunPay недоступен" in failed["error_text"]
    assert notifications and "Не удалось выдать" in notifications[0]


def test_fulfill_order_reports_no_stock(monkeypatch):
    queued = {
        "id": 3,
        "order_id": "ORDER-3",
        "position_id": 7,
        "position_title": "Spotify 30",
        "subscription_days": 30,
        "buyer_username": "buyer",
        "chat_id": "users-1-2",
        "chat_name": "buyer",
        "status": "queued",
        "item_id": None,
        "followup_attempts": 0,
    }
    monkeypatch.setattr(plugin, "_telegram_id", lambda: 1)
    monkeypatch.setattr(plugin, "_db", lambda: FakeDB(queued, queued))
    monkeypatch.setattr(plugin, "_claim_items", lambda *_a: _async_value([]))

    assert asyncio.run(plugin._fulfill_order(3)) == "no_stock"


def test_builtin_delivery_conflict_detects_enabled_rule(monkeypatch):
    class FakeDatabase:
        @staticmethod
        async def get_user(_telegram_id):
            return {"auto_delivery_enabled": True}

        @staticmethod
        async def find_delivery_rule(_telegram_id, _description):
            return {"enabled": True, "response": "$product"}

    monkeypatch.setattr(plugin, "_cardinal", SimpleNamespace())
    monkeypatch.setattr(plugin, "_db", lambda: FakeDatabase())
    monkeypatch.setattr(plugin, "_telegram_id", lambda: 1)
    monkeypatch.setattr(
        plugin, "_sync", lambda awaitable, timeout=60: asyncio.run(awaitable)
    )
    assert plugin._builtin_delivery_conflict("Spotify Premium") is True


def test_builtin_delivery_conflict_is_false_without_auto_delivery(monkeypatch):
    class FakeDatabase:
        @staticmethod
        async def get_user(_telegram_id):
            return {"auto_delivery_enabled": False}

        @staticmethod
        async def find_delivery_rule(_telegram_id, _description):
            return {"enabled": True, "response": "$product"}

    monkeypatch.setattr(plugin, "_db", lambda: FakeDatabase())
    monkeypatch.setattr(
        plugin, "_sync", lambda awaitable, timeout=60: asyncio.run(awaitable)
    )
    assert plugin._builtin_delivery_conflict("Spotify Premium") is False


def test_fulfill_order_delivers_all_purchased_units(monkeypatch):
    queued = {
        "id": 4,
        "order_id": "ORDER-4",
        "position_id": 7,
        "position_title": "Spotify 30",
        "subscription_days": 30,
        "amount": 2,
        "buyer_username": "buyer",
        "chat_id": "users-1-2",
        "chat_name": "buyer",
        "status": "queued",
        "item_ids": [],
        "followup_attempts": 0,
    }
    delivering = dict(queued, status="delivering", item_ids=[51, 52])
    items = [
        {
            "id": 51,
            "email": "first@example.com",
            "password": "FirstPass!",
            "activation_key": None,
        },
        {
            "id": 52,
            "email": "second@example.com",
            "password": "SecondPass!",
            "activation_key": "KEY-2",
        },
    ]
    sent = []
    sold = []
    updates = []

    async def fake_send(order, text):
        sent.append(text)

    async def fake_sold(item_id, order_id):
        sold.append((item_id, order_id))

    async def fake_update(_order_db_id, **values):
        updates.append(values)

    async def fake_notify(_text, **_kwargs):
        return None

    async def fake_alert(_position_id):
        return None

    monkeypatch.setattr(plugin, "_telegram_id", lambda: 1)
    db = FakeDB(queued, delivering)
    monkeypatch.setattr(plugin, "_db", lambda: db)
    monkeypatch.setattr(plugin, "_claim_items", lambda *_a: _async_value(items))
    monkeypatch.setattr(plugin, "_settings", lambda: _async_value(_settings()))
    monkeypatch.setattr(plugin, "_funpay_send", fake_send)
    monkeypatch.setattr(plugin, "_update_order", fake_update)
    monkeypatch.setattr(plugin, "_mark_item_sold", fake_sold)
    monkeypatch.setattr(plugin, "_notify_owner", fake_notify)
    monkeypatch.setattr(plugin, "_maybe_alert_low_stock", fake_alert)

    assert asyncio.run(plugin._fulfill_order(4)) == "delivered"
    assert "first@example.com" in sent[0]
    assert "second@example.com" in sent[0]
    assert "KEY-2" in sent[0]
    assert sold == [(51, "ORDER-4"), (52, "ORDER-4")]
    delivered = next(values for values in updates if values.get("status") == "delivered")
    assert delivered["followup_status"] == "pending"
    claiming = next(
        args
        for query, args in db.calls
        if query.startswith("UPDATE spotify_accounts_orders")
    )
    assert claiming[2] == [51, 52]


def test_fulfill_order_releases_partial_claim_when_stock_short(monkeypatch):
    queued = {
        "id": 5,
        "order_id": "ORDER-5",
        "position_id": 7,
        "position_title": "Spotify 30",
        "subscription_days": 30,
        "amount": 2,
        "buyer_username": "buyer",
        "chat_id": "users-1-2",
        "chat_name": "buyer",
        "status": "queued",
        "item_ids": [],
        "followup_attempts": 0,
    }
    released = []

    async def fake_release(item_id):
        released.append(item_id)

    monkeypatch.setattr(plugin, "_telegram_id", lambda: 1)
    monkeypatch.setattr(plugin, "_db", lambda: FakeDB(queued, queued))
    monkeypatch.setattr(
        plugin,
        "_claim_items",
        lambda *_a: _async_value([{"id": 61, "email": "only@example.com"}]),
    )
    monkeypatch.setattr(plugin, "_release_item", fake_release)

    assert asyncio.run(plugin._fulfill_order(5)) == "no_stock"
    assert released == [61]


class FakeBot:
    def __init__(self):
        self.messages = []
        self.handlers = []
        self.callback_handlers = []

    def send_message(self, chat_id, text, **_kwargs):
        self.messages.append((chat_id, text))

    def register_message_handler(self, callback, **filters):
        self.handlers.append((callback, filters))

    def register_callback_query_handler(self, callback, func=None):
        self.callback_handlers.append((callback, func))


def _cardinal_with_bot(bot):
    return SimpleNamespace(
        telegram=SimpleNamespace(bot=bot, loop=None), plugin_manager=None
    )


def test_pre_init_registers_conventional_settings_handler(monkeypatch):
    bot = FakeBot()
    def fake_sync(awaitable, timeout=60):
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        return None

    monkeypatch.setattr(plugin, "_sync", fake_sync)
    plugin.pre_init(_cardinal_with_bot(bot))
    assert [callback for callback, _ in bot.handlers] == [plugin._on_setting_message]
    assert plugin._on_text_message is not None


def test_title_input_creates_position_and_asks_for_days(monkeypatch):
    """Без этого мастер зависал: текст названия не создавал позицию."""
    created = []
    prompts = []

    monkeypatch.setattr(plugin, "_telegram_id", lambda: 1)
    monkeypatch.setattr(
        plugin,
        "_create_position",
        lambda title, days: _async_value({"id": 9, "title": title, "days": days}),
    )
    monkeypatch.setattr(
        plugin,
        "_update_position",
        lambda *_args: _async_value(None),
    )
    monkeypatch.setattr(plugin, "_sync", lambda awaitable, timeout=60: asyncio.run(awaitable) if asyncio.iscoroutine(awaitable) else None)
    monkeypatch.setattr(
        plugin,
        "_prompt",
        lambda chat_id, key, text, context=None: prompts.append((key, context)),
    )

    plugin._pending_input = ("new_title", None)
    plugin._handle_pending_input(
        SimpleNamespace(text="Spotify Premium 30", chat=SimpleNamespace(id=5))
    )

    assert plugin._pending_input is None
    assert prompts == [("new_days", 9)]


def test_stock_mode_routes_text_through_pending_input(monkeypatch):
    handled = []

    monkeypatch.setattr(
        plugin, "_handle_stock_input", lambda message: handled.append(message.text)
    )
    plugin._stock_mode = {"position_id": 3, "added": 0}
    plugin._pending_input = (plugin.STOCK_PENDING_KEY, 3)

    plugin._handle_pending_input(
        SimpleNamespace(text="a@example.com:pw", chat=SimpleNamespace(id=5))
    )

    assert handled == ["a@example.com:pw"]
    assert plugin._pending_input == (plugin.STOCK_PENDING_KEY, 3)

    plugin._stock_mode = None
    plugin._pending_input = (plugin.STOCK_PENDING_KEY, 3)
    plugin._handle_pending_input(
        SimpleNamespace(text="b@example.com:pw", chat=SimpleNamespace(id=5))
    )
    assert plugin._pending_input is None
