import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot import (
    READY_PLUGINS,
    SecretBox,
    apply_bulk_lot_action,
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
    proxy_dict,
    proxy_label,
    ready_plugin_source,
    render_template,
    within_work_hours,
)
from FunPayAPI import Runner, types
from plugin_system import CardinalBotFacade, PluginManager, PluginValidationError


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


def test_cardinal_plugin_contract_is_loaded(tmp_path):
    source = '''
from cardinal import get_cardinal
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
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
