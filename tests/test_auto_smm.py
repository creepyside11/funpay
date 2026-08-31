import asyncio

import pytest

from ready_plugins import AutoSmm as plugin


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://smmway.ru/api/v2/", "https://smmway.ru/api/v2"),
        ("https://panel.example/api", "https://panel.example/api"),
    ],
)
def test_api_url_accepts_only_clean_https(value, expected):
    assert plugin._validate_api_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://smmway.ru/api/v2",
        "https://user:pass@smmway.ru/api/v2",
        "https://smmway.ru/api/v2?key=secret",
        "not-a-url",
    ],
)
def test_api_url_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        plugin._validate_api_url(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://t.me/example", "https://t.me/example"),
        ("http://example.com/post/1", "http://example.com/post/1"),
    ],
)
def test_target_url_validation(value, expected):
    assert plugin._validate_target_url(value) == expected


@pytest.mark.parametrize("value", ["t.me/example", "ftp://example.com", "https:///missing"])
def test_target_url_rejects_incomplete_links(value):
    with pytest.raises(ValueError):
        plugin._validate_target_url(value)


def test_quantity_is_multiplied_by_purchased_units():
    assert plugin._calculate_total_quantity(100, 3) == 300
    assert plugin._calculate_total_quantity(25, None) == 25


def test_longest_enabled_lot_title_wins():
    rules = [
        {"lot_title": "Telegram", "enabled": True},
        {"lot_title": "Telegram Premium", "enabled": True},
        {"lot_title": "Premium", "enabled": False},
    ]
    assert plugin._match_rule("Telegram Premium x3", rules) is rules[1]


def test_smm_request_uses_standard_post_contract(monkeypatch):
    captured = {}

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"order": 321}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(plugin, "_api_token", lambda _settings: "secret-token")
    monkeypatch.setattr(plugin.requests, "post", fake_post)

    result = plugin._smm_request(
        {"api_base_url": "https://smmway.ru/api/v2"},
        action="add",
        service=77,
        link="https://t.me/example",
        quantity=300,
    )

    assert result == {"order": 321}
    assert captured == {
        "url": "https://smmway.ru/api/v2",
        "data": {
            "key": "secret-token",
            "action": "add",
            "service": 77,
            "link": "https://t.me/example",
            "quantity": 300,
        },
        "timeout": 30,
        "allow_redirects": False,
    }


def test_buyer_link_is_confirmed_before_submission(monkeypatch):
    updates = []
    messages = []
    submissions = []
    job = {
        "id": 9,
        "status": "awaiting_link",
        "chat_id": "users-10-20",
        "chat_name": "Buyer",
    }

    async def active_job(*_args):
        return job

    async def update_job(job_id, **values):
        updates.append((job_id, values))
        job.update(values)

    async def send(_job, text):
        messages.append(text)

    async def submit(job_id):
        submissions.append(job_id)

    monkeypatch.setattr(plugin, "_active_buyer_job", active_job)
    monkeypatch.setattr(plugin, "_update_job", update_job)
    monkeypatch.setattr(plugin, "_funpay_send", send)
    monkeypatch.setattr(plugin, "_submit_job", submit)

    payload = {
        "chat_id": "123",
        "buyer_id": 10,
        "chat_name": "Buyer",
        "order_chat_id": "users-10-20",
        "text": "https://t.me/example",
    }
    asyncio.run(plugin._process_funpay_message(payload))

    assert updates == [
        (9, {"target_url": "https://t.me/example", "status": "link_confirmation"})
    ]
    assert submissions == []
    assert "#да" in messages[-1]

    payload["text"] = "#да"
    asyncio.run(plugin._process_funpay_message(payload))
    assert submissions == [9]


def test_link_message_matches_order_when_funpay_chat_ids_differ(monkeypatch):
    sent = []
    database_calls = []
    original_job = {
        "id": 12,
        "chat_id": "users-100-200",
        "status": "awaiting_link",
    }
    rebound_job = {
        "id": 12,
        "chat_id": "987654",
        "status": "awaiting_link",
    }

    class FakeDatabase:
        async def fetchrow(self, query, *args):
            database_calls.append((query, args))
            if "status IN" in query:
                return original_job
            return rebound_job

        async def execute(self, query, *args):
            database_calls.append((query, args))

    async def funpay_send(_job, text):
        sent.append(text)

    fake_database = FakeDatabase()
    monkeypatch.setattr(plugin, "_db", lambda: fake_database)
    monkeypatch.setattr(plugin, "_telegram_id", lambda: 7)
    monkeypatch.setattr(plugin, "_funpay_send", funpay_send)

    asyncio.run(
        plugin._process_funpay_message(
            {
                "chat_id": "987654",
                "buyer_id": 100,
                "chat_name": "BuyerName",
                "order_chat_id": "users-100-200",
                "text": "https://t.me/example",
            }
        )
    )

    lookup_args = database_calls[0][1]
    assert lookup_args == (7, "987654", 100, "BuyerName", "users-100-200")
    assert any(
        "SET chat_id=$3" in query and args[-1] == "987654"
        for query, args in database_calls
    )
    assert sent and "#да" in sent[-1] and "#изменить" in sent[-1]
