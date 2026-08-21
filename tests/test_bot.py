import threading
from types import SimpleNamespace

import pytest

from bot import (
    SecretBox,
    format_chat_history,
    format_money,
    format_order,
    load_detailed_balance,
    load_full_chat,
    normalize_proxy,
    proxy_dict,
    proxy_label,
    render_template,
)
from FunPayAPI import Runner, types


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


class FakeAccount:
    is_initiated = True
    runner = None


def test_runner_threads_can_stop_without_network_requests():
    runner = Runner(FakeAccount())
    stop_event = threading.Event()
    stop_event.set()
    runner.loop(stop_event)
    assert list(runner.listen(stop_event=stop_event)) == []
