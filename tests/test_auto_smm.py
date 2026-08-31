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


def test_completion_message_explains_refill_only_when_enabled():
    without_refill = plugin._completion_message(
        {"target_url": "https://t.me/example", "refill_enabled": False}
    )
    with_refill = plugin._completion_message(
        {"target_url": "https://t.me/example", "refill_enabled": True}
    )

    assert "#рефилл" not in without_refill
    assert "#рефилл" in with_refill
    assert "восстановление" in with_refill


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


def test_refill_command_requests_original_smm_order_once(monkeypatch):
    calls = []
    messages = []
    updates = []
    job = {
        "id": 15,
        "status": "completed",
        "refill_enabled": True,
        "refill_id": None,
        "smm_order_id": "SMM-500",
        "order_id": "FP-100",
        "chat_id": "123",
        "chat_name": "Buyer",
    }

    async def get_job(_job_id):
        return job

    async def get_settings():
        return {"api_base_url": "https://smmway.ru/api/v2"}

    def smm_request(_settings, **payload):
        calls.append(payload)
        return {"refill": 777}

    async def update_job(job_id, **values):
        updates.append((job_id, values))
        job.update(values)

    async def funpay_send(_job, text):
        messages.append(text)

    monkeypatch.setattr(plugin, "_job", get_job)
    monkeypatch.setattr(plugin, "_settings", get_settings)
    monkeypatch.setattr(plugin, "_smm_request", smm_request)
    monkeypatch.setattr(plugin, "_update_job", update_job)
    monkeypatch.setattr(plugin, "_funpay_send", funpay_send)
    monkeypatch.setattr(plugin, "_notify_owner", lambda _text: _async_value(None))

    asyncio.run(plugin._request_refill(15))

    assert calls == [{"action": "refill", "order": "SMM-500"}]
    assert updates == [
        (15, {"refill_id": "777", "refill_status": "Requested"})
    ]
    assert "Рефилл запрошен" in messages[-1]

    asyncio.run(plugin._request_refill(15))
    assert len(calls) == 1
    assert "уже был запрошен" in messages[-1]


def test_refill_command_uses_completed_job_instead_of_link_job(monkeypatch):
    requested = []

    async def refill_job(*_args):
        return {"id": 44}

    async def request(job_id):
        requested.append(job_id)

    async def unexpected(*_args):
        raise AssertionError("обычный поиск ссылки не должен запускаться для #рефилл")

    monkeypatch.setattr(plugin, "_active_refill_job", refill_job)
    monkeypatch.setattr(plugin, "_request_refill", request)
    monkeypatch.setattr(plugin, "_active_buyer_job", unexpected)

    asyncio.run(
        plugin._process_funpay_message(
            {
                "chat_id": "987654",
                "buyer_id": 100,
                "chat_name": "BuyerName",
                "order_chat_id": "users-100-200",
                "text": "#РЕФИЛЛ",
            }
        )
    )

    assert requested == [44]


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


async def _async_value(value):
    return value
