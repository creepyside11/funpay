import asyncio
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

import bot as bot_module
from bot import (
    PLAYEROK_PLUGIN_DOCUMENTATION_PATH,
    PLUGIN_CATALOG_DESCRIPTION_MAX,
    PLUGIN_CATALOG_DESCRIPTION_MIN,
    PLUGIN_DOCUMENTATION_PATH,
    READY_PLUGINS,
    SecretBox,
    apply_bulk_lot_action,
    conversation_actions_keyboard,
    create_playerok_account,
    format_chat_history,
    format_money,
    format_order,
    format_sales_stats,
    load_detailed_balance,
    load_full_chat,
    load_sales_stats,
    main_keyboard,
    normalize_proxy,
    normalize_review_reply,
    playerok_proxy_value,
    plugin_settings_callback_data,
    proxy_dict,
    proxy_label,
    ready_plugin_source,
    render_template,
    telegram_publisher_name,
    validate_catalog_description,
    within_work_hours,
)
import FunPayAPI.account as funpay_account_module
from FunPayAPI import Account, Runner, exceptions as fp_exceptions, types
from playerok_plugin_system import (
    PLAYEROK_READY_PLUGINS,
    PlayerokPluginManager,
    PlayerokPluginValidationError,
    playerok_ready_plugin_source,
    playerok_setting_label,
)
from plugin_system import CardinalBotFacade, PluginManager, PluginValidationError
from tg_bot import CBT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user:pass@127.0.0.1:8080", "http://user:pass@127.0.0.1:8080"),
        ("socks5://user:pass@example.org:1080", "socks5://user:pass@example.org:1080"),
        ("https://example.org:443", "https://example.org:443"),
    ],
)
def test_normalize_proxy(raw, expected):
    assert normalize_proxy(raw) == expected
    assert proxy_dict(expected) == {"http": expected, "https": expected}


@pytest.mark.parametrize("value", ["example.org", "ftp://example.org:21", "http://example.org:nope"])
def test_normalize_proxy_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_proxy(value)


def test_proxy_label_hides_credentials():
    assert proxy_label("http://secret:password@example.org:8080") == "http://example.org:8080"


def test_funpay_429_retries_are_bounded(monkeypatch):
    calls = []
    response = requests.Response()
    response.status_code = 429
    response._content = b"rate limited"
    response.request = requests.Request("GET", "https://funpay.com/").prepare()

    account = Account("golden-key-value")

    def request(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(account.session, "request", request)
    monkeypatch.setattr(funpay_account_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(fp_exceptions.RequestFailedError):
        account.method(
            "get",
            "https://funpay.com/",
            {},
            {},
            raise_not_200=True,
        )

    assert len(calls) == 10


def test_funpay_407_has_actionable_proxy_message():
    response = requests.Response()
    response.status_code = 407
    response._content = b"proxy authentication required"
    response.request = requests.Request("GET", "https://funpay.com/").prepare()

    message = bot_module.funpay_connection_error_message(
        fp_exceptions.RequestFailedError(response)
    )

    assert "407" in message
    assert "логин" in message
    assert "пароль" in message


def test_secret_box_round_trip_and_no_plaintext():
    box = SecretBox("test-secret")
    encrypted = box.encrypt("golden-key-value")
    assert "golden-key-value" not in encrypted
    assert box.decrypt(encrypted) == "golden-key-value"


def test_message_variables_are_rendered():
    message = SimpleNamespace(
        author="Buyer",
        chat_name="Buyer",
        chat_id=123,
        __str__=lambda self: "hello",
    )
    account = SimpleNamespace(username="Seller", id=456)
    result = render_template(
        "Привет, $username! Чат $chat_id, продавец $account_name, $date $full_time",
        message=message,
        account=account,
    )
    assert "Buyer" in result
    assert "123" in result
    assert "Seller" in result
    assert "$" not in result


def test_review_variables_are_rendered():
    review = SimpleNamespace(stars=4, text="Хорошо", reply="")
    order = SimpleNamespace(
        id="ABCD1234",
        buyer_username="Buyer",
        chat_id=10,
        title="Lot",
        review=review,
    )
    result = render_template(
        "$username: $stars/5 — $review_text — $order_title",
        order=order,
        review=review,
    )
    assert result == "Buyer: 4/5 — Хорошо — Lot"


def test_chat_history_is_escaped_and_split_into_valid_sized_chunks():
    item = SimpleNamespace(
        author_id=10,
        author="<Buyer>",
        by_bot=False,
        by_vertex=False,
        text="<script>" + "x" * 5000,
        image_link=None,
    )
    chat = SimpleNamespace(id=5, name="Buyer", looking_link=None, looking_text=None, messages=[item, item])
    chunks = format_chat_history(chat, account_id=99)
    assert all(len(chunk) <= 3800 for chunk in chunks)
    assert all("<script>" not in chunk for chunk in chunks)
    assert "&lt;Buyer&gt;" in "".join(chunks)
    assert "<pre>" in "".join(chunks)
    assert "<code>5</code>" in chunks[0]


def test_detailed_balance_uses_profile_lot():
    expected = object()
    account = SimpleNamespace(
        id=1,
        get_user=lambda _id: SimpleNamespace(get_common_lots=lambda: [SimpleNamespace(id=77)]),
        get_balance=lambda lot_id: expected if lot_id == 77 else None,
    )
    assert load_detailed_balance(account) is expected
    assert format_money(1200.50) == "1 200.5"


def test_full_chat_loads_older_pages():
    chat = SimpleNamespace(
        name="Buyer",
        messages=[SimpleNamespace(id=3), SimpleNamespace(id=4)],
    )

    class Account:
        def get_chat(self, _chat_id, _with_history):
            return chat

        def get_chat_history(self, _chat_id, cursor, _name):
            return [SimpleNamespace(id=1), SimpleNamespace(id=2)] if cursor == 3 else []

    result, truncated = load_full_chat(Account(), 10)
    assert [message.id for message in result.messages] == [1, 2, 3, 4]
    assert truncated is False


def test_order_card_omits_delivery_secrets():
    order = SimpleNamespace(
        id="ABC123",
        title="Test <lot>",
        subcategory=None,
        status=types.OrderStatuses.PAID,
        amount=2,
        sum=1200.5,
        currency=types.Currency.RUB,
        buyer_username="<Buyer>",
        buyer_id=10,
        seller_username="Seller",
        seller_id=20,
        server=None,
        side=None,
        player=None,
        chat_id=30,
        order_secrets=["DO-NOT-SHOW"],
    )
    card = format_order(order)
    assert "ABC123" in card
    assert "1 200.5" in card
    assert "&lt;Buyer&gt;" in card
    assert "DO-NOT-SHOW" not in card


def test_sales_statistics_uses_selected_period_and_closed_revenue():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    orders = [
        SimpleNamespace(
            date=now - timedelta(days=1),
            buyer_id=1,
            currency=types.Currency.RUB,
            status=types.OrderStatuses.CLOSED,
            price=100,
            description="Lot A",
        ),
        SimpleNamespace(
            date=now - timedelta(days=2),
            buyer_id=2,
            currency=types.Currency.RUB,
            status=types.OrderStatuses.REFUNDED,
            price=50,
            description="Lot B",
        ),
        SimpleNamespace(
            date=now - timedelta(days=40),
            buyer_id=3,
            currency=types.Currency.RUB,
            status=types.OrderStatuses.CLOSED,
            price=999,
            description="Old lot",
        ),
    ]

    class Account:
        def get_sales(self, **_kwargs):
            return None, orders, "ru", {}

    stats = load_sales_stats(Account(), 30)
    assert stats.total == 2
    assert stats.closed == 1
    assert stats.refunded == 1
    assert stats.revenue["₽"] == 100
    assert "Lot A" in format_sales_stats(stats)


def test_autoreply_schedule_and_review_limits():
    assert within_work_hours(9, 22, 12)
    assert not within_work_hours(9, 22, 23)
    assert within_work_hours(22, 7, 2)
    assert len(normalize_review_reply("x" * 1200)) == 999
    assert normalize_review_reply("\n".join(str(i) for i in range(20))).count("\n") == 9


def test_main_menu_has_no_direct_message_or_image_buttons():
    callbacks = {
        button.callback_data
        for row in main_keyboard().inline_keyboard
        for button in row
    }
    assert "send_message" not in callbacks
    assert "images" not in callbacks
    assert "plugins" in callbacks
    assert "marketplace_switch" in callbacks
    assert "account_switch:funpay" in callbacks
    assert {"delivery", "command_replies"} <= callbacks


def test_additional_notification_copies_do_not_include_owner_buttons():
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    class FakeDatabase:
        async def list_notification_targets(self, telegram_id):
            assert telegram_id == 10
            return [{"chat_id": -10020, "enabled": True}]

    manager = bot_module.RuntimeManager(
        FakeBot(), FakeDatabase(), SimpleNamespace()
    )
    markup = main_keyboard()
    asyncio.run(manager.safe_notify(10, "Тест", reply_markup=markup))

    assert sent[0][0] == 10
    assert sent[0][2]["reply_markup"] is markup
    assert sent[1][0] == -10020
    assert "reply_markup" not in sent[1][2]


def test_funpay_plugin_timeout_does_not_block_runtime(monkeypatch):
    class HangingPlugins:
        def __init__(self):
            self.runtimes = {}
            self.started = False

        async def load_runtime(self, _telegram_id, _runtime):
            self.started = True
            await asyncio.Event().wait()

    manager = bot_module.RuntimeManager(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    plugins = HangingPlugins()
    manager.plugins = plugins
    monkeypatch.setattr(bot_module, "PLUGIN_LOAD_TIMEOUT", 0.01)

    runtime = SimpleNamespace()
    asyncio.run(manager._reload_funpay_plugins(10, runtime))

    assert plugins.started is True
    assert plugins.runtimes == {}


def test_saved_accounts_start_in_background():
    started = asyncio.Event()
    row = {
        "id": 7,
        "telegram_id": 10,
        "label": "Seller",
        "external_id": "50",
    }

    class FakeDatabase:
        async def active_users(self):
            return [row]

        async def active_playerok_users(self):
            return []

        async def get_user(self, _telegram_id):
            return {
                "active_funpay_account_id": 7,
                "notify_system": False,
            }

    manager = bot_module.RuntimeManager(
        SimpleNamespace(), FakeDatabase(), SimpleNamespace()
    )

    async def hanging_start(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    manager.start = hanging_start

    async def scenario():
        await asyncio.wait_for(manager.start_saved(), timeout=0.2)
        await asyncio.wait_for(started.wait(), timeout=0.2)
        assert manager.connection_tasks
        await manager.close()

    asyncio.run(scenario())


def test_start_command_answers_before_reconnecting_accounts():
    events = []
    account = {"id": 7, "label": "Seller"}

    class FakeDatabase:
        async def ensure_user(self, _telegram_id):
            pass

        async def get_user(self, _telegram_id):
            return {"active_marketplace": "funpay"}

        async def list_marketplace_accounts(self, _telegram_id, marketplace):
            return [account] if marketplace == "funpay" else []

        async def get_active_marketplace_account(self, _telegram_id, marketplace):
            return account if marketplace == "funpay" else None

    class FakeManager:
        funpay_account_runtimes = {}
        playerok_account_runtimes = {}

        def get(self, _telegram_id):
            return None

        def get_playerok(self, _telegram_id):
            return None

        def start_account_in_background(self, *_args):
            events.append("connect")

    class FakeState:
        async def clear(self):
            pass

    class FakeMessage:
        from_user = SimpleNamespace(id=10)

        async def answer(self, _text, **_kwargs):
            events.append("answer")

    router = bot_module.build_router(
        FakeDatabase(), FakeManager(), SimpleNamespace()
    )
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "start"
    )

    asyncio.run(handler(FakeMessage(), FakeState()))

    assert events == ["answer", "connect"]


def test_funpay_save_error_finishes_connection_dialog(monkeypatch):
    edits = []

    class FakeAccount:
        id = 50
        username = "Seller"

        def __init__(self, *_args, **_kwargs):
            pass

        def get(self):
            return self

    class FakeDatabase:
        async def save_account(self, *_args):
            raise RuntimeError("database unavailable")

    class FakeState:
        cleared = False

        async def get_data(self):
            return {"proxy": "http://127.0.0.1:8080"}

        async def clear(self):
            self.cleared = True

    class WaitMessage:
        async def edit_text(self, text):
            edits.append(text)

    class FakeMessage:
        text = "golden-key-value-123456"
        from_user = SimpleNamespace(id=10)

        async def delete(self):
            pass

        async def answer(self, _text):
            return WaitMessage()

    monkeypatch.setattr(bot_module, "Account", FakeAccount)
    router = bot_module.build_router(
        FakeDatabase(),
        SimpleNamespace(),
        SimpleNamespace(encrypt=lambda value: f"encrypted:{value}"),
    )
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "accept_golden_key"
    )
    state = FakeState()

    asyncio.run(handler(FakeMessage(), state))

    assert state.cleared is True
    assert edits and "сохранить аккаунт не удалось" in edits[-1]


def test_cardinal_command_reply_matches_exact_message():
    outgoing = []

    class FakeDatabase:
        async def find_command_reply(self, telegram_id, command):
            assert (telegram_id, command) == (10, "#help")
            return {"response": "Привет, $username", "notify": False}

    class FakeAccount:
        id = 50
        username = "Seller"

        def send_message(self, chat_id, text, chat_name):
            outgoing.append((chat_id, text, chat_name))
            return True

    manager = bot_module.RuntimeManager(
        SimpleNamespace(), FakeDatabase(), SimpleNamespace()
    )
    runtime = bot_module.AccountRuntime(10, FakeAccount(), SimpleNamespace())
    message = SimpleNamespace(
        text="  #HELP  ",
        chat_id=77,
        chat_name="Buyer",
        author="Buyer",
    )

    assert asyncio.run(manager._process_command_reply(runtime, message)) is True
    assert outgoing == [(77, "Привет, Buyer", "Buyer")]


def test_auto_delivery_issues_stock_and_disables_empty_lot():
    sent = []
    finished = []
    lot_fields = SimpleNamespace(active=True)

    rule = {
        "id": 7,
        "lot_id": 88,
        "lot_title": "Нужный лот",
        "response": "Ваш товар: $product",
        "products": ["KEY-1"],
        "enabled": True,
        "disable_auto_restore": False,
        "disable_auto_disable": False,
    }

    class FakeDatabase:
        async def find_delivery_rule(self, telegram_id, title):
            assert (telegram_id, title) == (10, "Нужный лот")
            return rule

        async def claim_delivery(self, telegram_id, order_id, title, amount):
            assert (telegram_id, order_id, title, amount) == (
                10,
                "ORDER-1",
                "Нужный лот",
                1,
            )
            return rule, ["KEY-1"], 0, None

        async def finish_delivery(self, telegram_id, order_id, status, details=""):
            finished.append((telegram_id, order_id, status, details))

        async def restore_delivery_products(self, *_args):
            raise AssertionError("Успешно выданный товар не возвращается в запас")

    class FakeAccount:
        id = 50
        username = "Seller"

        def send_message(self, chat_id, text, chat_name):
            sent.append((chat_id, text, chat_name))
            return True

        def get_lot_fields(self, lot_id):
            assert lot_id == 88
            return lot_fields

        def save_lot(self, fields):
            assert fields is lot_fields

    manager = bot_module.RuntimeManager(
        SimpleNamespace(), FakeDatabase(), SimpleNamespace()
    )

    async def record_notification(_telegram_id, text, **_kwargs):
        sent.append(("notification", text, None))

    manager.safe_notify = record_notification
    runtime = bot_module.AccountRuntime(10, FakeAccount(), SimpleNamespace())
    event = SimpleNamespace(
        order=SimpleNamespace(
            id="ORDER-1",
            amount=1,
            description="Нужный лот",
            chat_id=77,
            buyer_username="Buyer",
            title="Нужный лот",
        )
    )
    settings = {
        "auto_delivery_enabled": True,
        "multi_delivery_enabled": True,
        "delivery_auto_disable": True,
        "delivery_auto_restore": True,
        "notify_delivery": True,
    }

    asyncio.run(manager._process_delivery(runtime, event, settings))

    assert sent[0] == (77, "Ваш товар: KEY-1", "Buyer")
    assert finished[0][:3] == (10, "ORDER-1", "sent")
    assert event.delivered is True
    assert event.goods_delivered == 1
    assert event.goods_left == 0
    assert lot_fields.active is False


def test_playerok_menu_and_independent_account_adapter(monkeypatch):
    assert bot_module.PlayerokAccount is not None
    callbacks = {
        button.callback_data
        for row in main_keyboard("playerok").inline_keyboard
        for button in row
    }
    assert {"marketplace_switch", "po_profile", "po_balance", "po_items"} <= callbacks
    assert "account_switch:playerok" in callbacks
    assert {
        "po_chats",
        "po_deals",
        "po_item_create",
        "po_autoreply",
        "po_delivery",
        "po_plugins",
    } <= callbacks
    assert playerok_proxy_value("http://user:pass@127.0.0.1:8080") == (
        "user:pass@127.0.0.1:8080"
    )
    with pytest.raises(ValueError):
        playerok_proxy_value("socks5://127.0.0.1:1080")

    class FakePlayerokAccount:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(bot_module, "PlayerokAccount", FakePlayerokAccount)
    first = create_playerok_account("token-one-123456", "http://127.0.0.1:8080")
    second = create_playerok_account("token-two-123456", "http://127.0.0.1:8080")
    assert first is not second
    assert first.kwargs["token"] == "token-one-123456"
    assert second.kwargs["token"] == "token-two-123456"

    class MissingCertificateAccount:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            raise FileNotFoundError("cacert.pem")

        def _refresh_clients(self):
            self.refreshed = True

    monkeypatch.setattr(bot_module, "PlayerokAccount", MissingCertificateAccount)
    recovered = create_playerok_account("token-three-123", "http://127.0.0.1:8080")
    assert recovered.refreshed is True
    assert recovered._tmp_cert_path == bot_module.certifi.where()


def test_playerok_email_code_auth_returns_token_cookie(monkeypatch):
    calls = []

    class Cookies(dict):
        def get_dict(self):
            return dict(self)

    class Response:
        status_code = 200

        def __init__(self, payload, cookies=None):
            self._payload = payload
            self.cookies = Cookies(cookies or {})

        def json(self):
            return self._payload

    class Session:
        def __init__(self, **kwargs):
            calls.append(("session", kwargs))
            self.cookies = Cookies({"device": "abc"})

        def post(self, url, *, json, headers):
            calls.append((json["operationName"], url, headers))
            if json["operationName"] == "getEmailAuthCode":
                return Response({"data": {"getEmailAuthCode": True}})
            return Response(
                {"data": {"checkEmailAuthCode": {"id": "seller-1"}}},
                {"token": "token-from-email"},
            )

    monkeypatch.setitem(
        sys.modules,
        "curl_cffi",
        SimpleNamespace(requests=SimpleNamespace(Session=Session)),
    )
    proxy = "http://user:pass@127.0.0.1:8080"
    pending_cookie = bot_module.request_playerok_email_code("seller@example.com", proxy)
    cookie, viewer = bot_module.verify_playerok_email_code(
        "seller@example.com", "123456", proxy, pending_cookie
    )

    assert "device=abc" in pending_cookie
    assert "token=token-from-email" in cookie
    assert viewer["id"] == "seller-1"
    assert [call[0] for call in calls if call[0] != "session"] == [
        "getEmailAuthCode",
        "checkEmailAuthCode",
    ]


def test_playerok_email_code_accepts_auid_from_set_cookie_header(monkeypatch):
    class Headers(dict):
        def get_list(self, key):
            if key.casefold() == "set-cookie":
                return [
                    "auid=authenticated-user-id; Path=/; HttpOnly; Secure; SameSite=Lax"
                ]
            return []

    class Response:
        status_code = 200

        def __init__(self):
            self.cookies = {}
            self.headers = Headers()
            self.history = []

        def json(self):
            return {"data": {"checkEmailAuthCode": {"id": "seller-1"}}}

    class Session:
        def __init__(self, **_kwargs):
            self.cookies = {}

        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setitem(
        sys.modules,
        "curl_cffi",
        SimpleNamespace(requests=SimpleNamespace(Session=Session)),
    )

    cookie, viewer = bot_module.verify_playerok_email_code(
        "seller@example.com",
        "123456",
        "http://127.0.0.1:8080",
        "lb_session_id=temporary-session",
    )

    assert "auid=authenticated-user-id" in cookie
    assert "lb_session_id=temporary-session" in cookie
    assert "token=" not in cookie
    assert viewer["id"] == "seller-1"


def test_notification_identifies_marketplace_account():
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    class FakeDatabase:
        async def list_notification_targets(self, _telegram_id):
            return []

    manager = bot_module.RuntimeManager(FakeBot(), FakeDatabase(), SimpleNamespace())
    runtime = bot_module.PlayerokRuntime(
        10,
        SimpleNamespace(id="seller-42", username="Seller"),
        account_key=7,
        account_label="Main Playerok",
    )
    asyncio.run(
        manager.safe_notify(
            10,
            "Test",
            marketplace="playerok",
            account_runtime=runtime,
        )
    )
    assert "Playerok" in sent[0][1]
    assert "Main Playerok" in sent[0][1]
    assert "seller-42" in sent[0][1]


def test_playerok_template_uses_chat_deal_and_message_variables():
    account = SimpleNamespace(id="seller-id", username="Seller")
    chat = SimpleNamespace(id="chat-1")
    message = SimpleNamespace(text="Здравствуйте", user=SimpleNamespace(username="Buyer"))
    deal = SimpleNamespace(
        id="deal-1",
        user=SimpleNamespace(username="Buyer"),
        item=SimpleNamespace(name="Item"),
        chat=chat,
    )
    rendered = bot_module.render_playerok_template(
        "$username · $chat_id · $message_text · $order_id · $order_title",
        account,
        chat=chat,
        message=message,
        deal=deal,
    )
    assert rendered == "Buyer · chat-1 · Здравствуйте · deal-1 · Item"


def test_playerok_auto_delivery_can_confirm_after_success(monkeypatch):
    sent = []
    updated = []
    rule = {
        "id": 7,
        "item_title": "Item",
        "response": "Ключ: $product",
    }

    class FakeDatabase:
        async def claim_playerok_delivery(self, telegram_id, deal_id, item_id):
            assert (telegram_id, deal_id, item_id) == (10, "deal-1", "item-1")
            return rule, ["KEY-1"], 0, None

        async def restore_playerok_delivery_products(self, *_args):
            raise AssertionError("Товар не должен возвращаться после успешной выдачи")

        async def finish_playerok_delivery(self, *args):
            sent.append(("log", args))

    class FakeAccount:
        id = "seller"
        username = "Seller"

        def send_message(self, chat_id, text):
            sent.append((chat_id, text))

        def update_deal(self, deal_id, status):
            updated.append((deal_id, status))

    manager = bot_module.RuntimeManager(SimpleNamespace(), FakeDatabase(), SimpleNamespace())
    monkeypatch.setattr(bot_module, "PlayerokItemDealStatuses", SimpleNamespace(SENT="SENT"))

    async def record_notification(*_args, **_kwargs):
        return None

    manager.safe_notify = record_notification
    runtime = bot_module.PlayerokRuntime(10, FakeAccount())
    deal = SimpleNamespace(
        id="deal-1",
        status=SimpleNamespace(name="PAID"),
        item=SimpleNamespace(id="item-1", name="Item"),
        chat=SimpleNamespace(id="chat-1"),
        user=SimpleNamespace(username="Buyer"),
    )
    row = {
        "playerok_auto_delivery_enabled": True,
        "playerok_auto_confirm_enabled": True,
        "playerok_notify_delivery": True,
    }

    asyncio.run(manager._process_playerok_delivery(runtime, row, deal))

    assert sent[0] == ("chat-1", "Ключ: KEY-1")
    assert updated == [("deal-1", "SENT")]


def test_playerok_draft_publication_uses_free_priority(monkeypatch):
    monkeypatch.setattr(
        bot_module, "PlayerokItemStatuses", SimpleNamespace(DRAFT="DRAFT")
    )
    draft = SimpleNamespace(id="item-1", name="Draft", price=150)
    published = []

    class Account:
        def get_my_items(self, **kwargs):
            assert kwargs["statuses"] == ["DRAFT"]
            return SimpleNamespace(items=[draft])

        def get_item_priority_statuses(self, item_id, price):
            assert (item_id, price) == ("item-1", 150)
            return [SimpleNamespace(id="free", price=0)]

        def publish_item(self, item_id, priority_id):
            published.append((item_id, priority_id))

    manager = bot_module.RuntimeManager(
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
    )
    manager.playerok_runtimes[1] = bot_module.PlayerokRuntime(1, Account())
    changed, total, errors = asyncio.run(manager.publish_playerok_drafts(1))
    assert (changed, total, errors) == (1, 1, [])
    assert published == [("item-1", "free")]


def test_playerok_paid_priorities_exclude_free_and_sort_by_price():
    free = SimpleNamespace(id="free", name="Базовый", price=0, period=0)
    week = SimpleNamespace(id="week", name="Premium", price=350, period=7)
    day = SimpleNamespace(id="day", name="Boost", price=100, period=1)
    invalid = SimpleNamespace(id=None, name="Broken", price=50, period=1)

    result = bot_module.playerok_paid_priorities([free, week, invalid, day])

    assert [item.id for item in result] == ["day", "week"]
    assert bot_module.playerok_priority_label(day) == "Boost · 1 дн. · 100 ₽"


def test_conversation_actions_do_not_force_main_menu():
    callbacks = {
        button.callback_data
        for row in conversation_actions_keyboard(123).inline_keyboard
        for button in row
    }
    assert callbacks == {"reply:123", "chat_full:123:0", "image_chat:123"}
    assert "menu" not in callbacks


def test_downloadable_plugin_documentation_is_ai_readable():
    text = PLUGIN_DOCUMENTATION_PATH.read_text(encoding="utf-8")
    assert "Короткая инструкция для нейросети" in text
    assert "BIND_TO_NEW_MESSAGE" in text
    assert "автоответ на отзыв" in text.casefold()
    assert "Финальный чек-лист" in text


def test_downloadable_playerok_plugin_documentation_is_ai_readable():
    text = PLAYEROK_PLUGIN_DOCUMENTATION_PATH.read_text(encoding="utf-8")
    assert "Инструкция для нейросети" in text
    assert "BIND_TO_DEAL_CHANGED" in text
    assert "ctx.get_setting" in text
    assert "Финальный чек-лист" in text


def test_playerok_plugin_contract_and_ready_plugins(tmp_path):
    source = '''
NAME = "Test Playerok Plugin"
VERSION = "1.0.0"
DESCRIPTION = "Plugin"
CREDITS = "Tester"
UUID = "12345678-1234-4234-9234-123456789abc"
SETTINGS_PAGE = True
SETTINGS = {
    "enabled": {"label": "Включено", "type": "bool", "default": False},
    "period": {"label": "Период", "type": "choice", "default": "7", "choices": {"7": "7 дней", "30": "30 дней"}},
}
def run(ctx):
    return "ok"
ACTIONS = {"run": {"label": "Запустить", "handler": run}}
BIND_TO_START = []
BIND_TO_STOP = []
BIND_TO_TICK = []
BIND_TO_NEW_MESSAGE = []
BIND_TO_DEAL_CHANGED = []
BIND_TO_NEW_REVIEW = []
BIND_TO_SETTING_CHANGED = []
BIND_TO_DELETE = None
'''
    manager = PlayerokPluginManager(SimpleNamespace(), SimpleNamespace())
    manager.root = tmp_path
    plugin = manager._load_module(1, "test.py", source, True)
    assert plugin.settings_schema["enabled"]["default"] is False
    assert plugin.actions["run"]["label"] == "Запустить"
    assert playerok_setting_label(plugin.settings_schema["period"], "30") == "30 дней"
    with pytest.raises(PlayerokPluginValidationError):
        manager._load_module(1, "bad.txt", source, True)

    ready = [
        manager._load_module(
            2, spec.filename, playerok_ready_plugin_source(spec), True
        )
        for spec in PLAYEROK_READY_PLUGINS
    ]
    assert [item.uuid for item in ready] == [spec.uuid for spec in PLAYEROK_READY_PLUGINS]
    assert all(item.settings_page and item.actions for item in ready)


def test_plugin_settings_button_uses_cardinal_callback_contract():
    external = SimpleNamespace(
        uuid="12345678-1234-4234-9234-123456789abc",
        enabled=True,
        settings_page=True,
    )
    assert CBT.PLUGIN_SETTINGS == "47"
    assert plugin_settings_callback_data(external) == (
        "47:12345678-1234-4234-9234-123456789abc:0"
    )
    external.enabled = False
    assert plugin_settings_callback_data(external) == (
        "47:12345678-1234-4234-9234-123456789abc:0"
    )
    external.enabled = True
    external.settings_page = False
    assert plugin_settings_callback_data(external) is None

    builtin = SimpleNamespace(
        uuid=READY_PLUGINS[0].uuid,
        enabled=True,
        settings_page=True,
    )
    assert plugin_settings_callback_data(builtin) == (
        f"builtin_open:{READY_PLUGINS[0].uuid}"
    )


def test_cardinal_plugin_contract_is_loaded(tmp_path):
    source = '''
from cardinal import get_cardinal
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from tg_bot import CBT
NAME = "Test"
VERSION = "1.0"
DESCRIPTION = "Plugin"
CREDITS = "Tester"
SETTINGS_PAGE = False
UUID = "12345678-1234-4234-9234-123456789abc"
BIND_TO_DELETE = None
def on_message(cardinal, event):
    return None
BIND_TO_NEW_MESSAGE = [on_message]
'''
    manager = PluginManager(SimpleNamespace(), SimpleNamespace())
    manager.root = tmp_path
    plugin = manager._load_module(1, "test.py", source, True)
    assert plugin.name == "Test"
    assert len(plugin.hooks["BIND_TO_NEW_MESSAGE"]) == 1
    with pytest.raises(PluginValidationError):
        manager._load_module(1, "bad.txt", source, True)


def test_ready_plugins_have_valid_cardinal_sources(tmp_path):
    manager = PluginManager(SimpleNamespace(), SimpleNamespace())
    manager.root = tmp_path
    loaded = [
        manager._load_module(1, spec.filename, ready_plugin_source(spec), True)
        for spec in READY_PLUGINS
    ]
    assert [plugin.uuid for plugin in loaded] == [spec.uuid for spec in READY_PLUGINS]
    assert all(plugin.settings_page for plugin in loaded)


def test_catalog_description_validation_and_publisher_name():
    valid = "Плагин добавляет команду, настройки и безопасный сценарий работы."
    assert validate_catalog_description(f"  {valid}  ") == valid
    with pytest.raises(ValueError):
        validate_catalog_description("x" * (PLUGIN_CATALOG_DESCRIPTION_MIN - 1))
    with pytest.raises(ValueError):
        validate_catalog_description("x" * (PLUGIN_CATALOG_DESCRIPTION_MAX + 1))
    assert telegram_publisher_name(SimpleNamespace(username="author", full_name="Author")) == "@author"
    assert telegram_publisher_name(SimpleNamespace(username=None, full_name="Иван")) == "Иван"


def test_bulk_lot_action_updates_common_and_currency_lots():
    common_subcategory = SimpleNamespace(type=types.SubCategoryTypes.COMMON, id=10)
    currency_subcategory = SimpleNamespace(type=types.SubCategoryTypes.CURRENCY, id=20)
    lots = [
        SimpleNamespace(id=1, subcategory=common_subcategory),
        SimpleNamespace(id="1-2-20-3", subcategory=currency_subcategory),
    ]
    lot_fields = SimpleNamespace(active=False)
    lot_fields.renew_fields = lambda: lot_fields
    chip_offer = SimpleNamespace(active=False)
    chip_fields = SimpleNamespace(chip_offers={"offer": chip_offer})
    chip_fields.renew_fields = lambda: chip_fields

    class Account:
        id = 42

        def get_user(self, _user_id):
            return SimpleNamespace(get_lots=lambda: lots)

        def get_lot_fields(self, _lot_id):
            return lot_fields

        def save_lot(self, fields):
            assert fields is lot_fields

        def get_chip_fields(self, subcategory_id):
            assert subcategory_id == 20
            return chip_fields

        def save_chip(self, fields):
            assert fields is chip_fields

    result = apply_bulk_lot_action(Account(), "activate")
    assert result.changed == 2
    assert not result.errors
    assert lot_fields.active is True
    assert chip_offer.active is True


def test_cardinal_telegram_handler_bridge_dispatches_commands():
    facade = CardinalBotFacade(SimpleNamespace(), SimpleNamespace())
    received = []
    facade.register_message_handler(lambda message: received.append(message.text), commands=["hello"])
    source = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=1),
        from_user=SimpleNamespace(id=1),
        text="/hello world",
        caption=None,
        document=None,
        photo=None,
    )
    assert facade.dispatch_message(source)
    assert received == ["/hello world"]


def test_cardinal_telegram_handlers_follow_plugin_state():
    uuid = "12345678-1234-4234-9234-123456789abc"
    enabled = {uuid: False}
    facade = CardinalBotFacade(
        SimpleNamespace(),
        SimpleNamespace(),
        enabled_checker=lambda value: enabled.get(value, False),
    )
    facade.current_plugin_uuid = uuid
    received = []
    facade.register_message_handler(lambda message: received.append(message.text))
    source = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=1),
        from_user=SimpleNamespace(id=1),
        text="test",
        caption=None,
        document=None,
        photo=None,
    )

    assert not facade.dispatch_message(source)
    enabled[uuid] = True
    assert facade.dispatch_message(source)
    assert received == ["test"]
    facade.unregister_plugin(uuid)
    assert not facade.dispatch_message(source)


class FakeAccount:
    is_initiated = True
    runner = None


def test_runner_threads_can_stop_without_network_requests():
    runner = Runner(FakeAccount())
    stop_event = threading.Event()
    stop_event.set()
    runner.loop(stop_event)
    assert list(runner.listen(stop_event=stop_event)) == []
