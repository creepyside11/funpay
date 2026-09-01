import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import PasswordHashInvalidError

from ready_plugins import TelegramChannelBoost as plugin


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


def test_smm_request_uses_standard_post_contract_without_redirects(monkeypatch):
    captured = {}

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"order": 12345}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(plugin, "_api_token", lambda _settings: "api-secret")
    monkeypatch.setattr(plugin.requests, "post", fake_post)

    result = plugin._smm_request(
        {"api_base_url": "https://smmway.ru/api/v2"},
        action="add",
        service=77,
        link="https://t.me/fptestchannel",
        quantity=100,
    )

    assert result == {"order": 12345}
    assert captured == {
        "url": "https://smmway.ru/api/v2",
        "data": {
            "key": "api-secret",
            "action": "add",
            "service": 77,
            "link": "https://t.me/fptestchannel",
            "quantity": 100,
        },
        "timeout": 30,
        "allow_redirects": False,
    }


def test_wrong_2fa_is_checked_before_buyer_gets_admin_rights(monkeypatch):
    calls = []
    notifications = []
    job = {
        "id": 8,
        "status": "awaiting_owner_2fa",
        "buyer_username": "@buyer_name",
        "channel_id": 123,
        "channel_access_hash": 456,
        "order_id": "ORDER-1",
        "channel_url": "https://t.me/fptestchannel",
        "telethon_session_id": None,
    }

    class FakeClient:
        @staticmethod
        def is_connected():
            return True

        async def get_entity(self, username):
            calls.append(("get_entity", username))
            return SimpleNamespace(id=99)

        async def __call__(self, request):
            name = type(request).__name__
            calls.append(name)
            if name == "GetPasswordRequest":
                return object()
            if name == "GetPasswordSettingsRequest":
                raise PasswordHashInvalidError(request=request)
            return object()

    fake_client = FakeClient()
    monkeypatch.setattr(plugin, "_client", fake_client)
    monkeypatch.setattr(plugin, "_job", lambda _job_id: _async_value(job))
    monkeypatch.setattr(plugin, "compute_check", lambda _state, _password: object())
    async def notify(text, **_kwargs):
        notifications.append(text)

    monkeypatch.setattr(plugin, "_notify_owner", notify)

    asyncio.run(plugin._transfer_owner(8, "wrong-password"))

    assert "GetPasswordSettingsRequest" in calls
    assert "EditAdminRequest" not in calls
    assert notifications and "пароль 2FA" in notifications[-1]


def test_owner_transfer_validates_password_then_promotes_and_transfers(monkeypatch):
    calls = []
    updates = []
    job = {
        "id": 9,
        "status": "awaiting_owner_2fa",
        "buyer_username": "@buyer_name",
        "channel_id": 123,
        "channel_access_hash": 456,
        "order_id": "ORDER-2",
        "channel_url": "https://t.me/fptestchannel",
        "telethon_session_id": None,
    }

    class FakeClient:
        @staticmethod
        def is_connected():
            return True

        async def get_entity(self, username):
            return SimpleNamespace(id=99, username=username)

        async def __call__(self, request):
            name = type(request).__name__
            calls.append(name)
            return object()

    async def update_job(job_id, **values):
        updates.append((job_id, values))

    monkeypatch.setattr(plugin, "_client", FakeClient())
    monkeypatch.setattr(plugin, "_job", lambda _job_id: _async_value(job))
    monkeypatch.setattr(plugin, "compute_check", lambda _state, _password: object())
    monkeypatch.setattr(plugin, "_update_job", update_job)
    monkeypatch.setattr(plugin, "_funpay_send", lambda *_args: _async_value(None))
    monkeypatch.setattr(plugin, "_notify_owner", lambda *_args: _async_value(None))

    asyncio.run(plugin._transfer_owner(9, "correct-password"))

    assert calls == [
        "GetParticipantRequest",
        "GetPasswordRequest",
        "GetPasswordSettingsRequest",
        "EditAdminRequest",
        "GetPasswordRequest",
        "EditChatCreatorRequest",
    ]
    assert updates == [(9, {"status": "completed", "error_text": None})]


def test_verified_buyer_is_transferred_automatically_with_saved_2fa(monkeypatch):
    events = []
    job = {
        "id": 11,
        "buyer_username": "@buyer_name",
        "channel_id": 123,
        "channel_access_hash": 456,
        "order_id": "ORDER-3",
        "channel_url": "https://t.me/fptestchannel",
        "telethon_session_id": None,
    }

    class FakeClient:
        @staticmethod
        def is_connected():
            return True

        async def get_entity(self, username):
            return SimpleNamespace(id=99, username=username)

        async def __call__(self, request):
            events.append(type(request).__name__)
            return object()

    async def update_job(job_id, **values):
        events.append(("update", job_id, values))

    async def funpay_send(_job, text):
        events.append(("buyer", text))

    async def transfer(job_id, password):
        events.append(("transfer", job_id, password))

    monkeypatch.setattr(plugin, "_client", FakeClient())
    monkeypatch.setattr(plugin, "_update_job", update_job)
    monkeypatch.setattr(plugin, "_funpay_send", funpay_send)
    monkeypatch.setattr(plugin, "_stored_2fa_password", lambda: _async_value("saved-2fa"))
    monkeypatch.setattr(plugin, "_transfer_owner", transfer)

    asyncio.run(plugin._verify_buyer(job))

    assert ("transfer", 11, "saved-2fa") in events
    assert any(
        event[0] == "update" and event[2]["status"] == "awaiting_owner_2fa"
        for event in events
        if isinstance(event, tuple)
    )


def test_username_message_matches_order_when_funpay_chat_ids_differ(monkeypatch):
    sent = []
    database_calls = []
    original_job = {
        "id": 12,
        "chat_id": "users-100-200",
        "status": "awaiting_username",
    }
    rebound_job = {
        "id": 12,
        "chat_id": "987654",
        "status": "awaiting_username",
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
                "text": "@buyer_name",
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


def test_inventory_refill_is_requested_when_ready_channel_drops(monkeypatch):
    calls = []
    updates = []
    notifications = []
    item = {
        "id": 31,
        "smm_order_id": "SMM-500",
        "refill_pending": False,
        "last_refill_at": None,
        "channel_url": "https://t.me/readychannel",
        "member_count": 85,
        "target_members": 100,
    }

    def smm_request(_settings, **payload):
        calls.append(payload)
        return {"refill": 700}

    async def update_inventory(item_id, **values):
        updates.append((item_id, values))

    async def notify(text, **_kwargs):
        notifications.append(text)

    monkeypatch.setattr(plugin, "_smm_request", smm_request)
    monkeypatch.setattr(plugin, "_update_inventory", update_inventory)
    monkeypatch.setattr(plugin, "_notify_owner", notify)

    requested = asyncio.run(plugin._request_inventory_refill(item, {}))

    assert requested is True
    assert calls == [{"action": "refill", "order": "SMM-500"}]
    assert any(values.get("refill_pending") is True for _id, values in updates)
    assert notifications and "refill" in notifications[-1]


def test_inventory_refill_is_checked_every_five_minutes_without_duplicates():
    now = datetime.now(timezone.utc)
    base = {
        "refill_pending": False,
        "last_refill_at": now - timedelta(seconds=plugin.INVENTORY_CHECK_SECONDS),
    }
    assert plugin.INVENTORY_CHECK_SECONDS == 300
    assert plugin._refill_due(base, now=now) is True
    assert plugin._refill_due(
        {**base, "last_refill_at": now - timedelta(seconds=299)}, now=now
    ) is False
    assert plugin._refill_due({**base, "refill_pending": True}, now=now) is False


def test_ready_inventory_is_attached_without_creating_or_boosting_channel(monkeypatch):
    updates = []
    messages = []
    notifications = []
    database_calls = []
    job = {
        "id": 41,
        "order_id": "ORDER-READY",
        "target_members": 100,
        "chat_id": "chat-1",
        "chat_name": "Buyer",
        "rule_id": 6,
    }
    item = {
        "id": 51,
        "channel_id": 123,
        "channel_access_hash": 456,
        "channel_username": "readychannel",
        "channel_url": "https://t.me/readychannel",
        "smm_order_id": "SMM-READY",
        "smm_status": "Completed",
        "member_count": 105,
        "target_members": 100,
        "telethon_session_id": 3,
    }
    saved_job = {**job, **item, "inventory_id": 51, "status": "awaiting_username"}

    class FakeDatabase:
        async def fetchrow(self, query, *args):
            database_calls.append((query, args))
            if "SET status='assigning'" in query:
                return job
            if "SET inventory_id=$3" in query:
                return saved_job
            raise AssertionError(query)

    async def update_job(job_id, **values):
        updates.append((job_id, values))

    async def funpay_send(_job, text):
        messages.append(text)

    async def notify(text, **_kwargs):
        notifications.append(text)

    monkeypatch.setattr(plugin, "_db", lambda: FakeDatabase())
    monkeypatch.setattr(plugin, "_telegram_id", lambda: 7)
    monkeypatch.setattr(plugin, "_settings", lambda: _async_value({}))
    monkeypatch.setattr(plugin, "_claim_ready_inventory", lambda _order, _rule: _async_value(item))
    monkeypatch.setattr(plugin, "_member_count", lambda _item: _async_value(105))
    monkeypatch.setattr(plugin, "_update_job", update_job)
    monkeypatch.setattr(plugin, "_job", lambda _job_id: _async_value(saved_job))
    monkeypatch.setattr(plugin, "_funpay_send", funpay_send)
    monkeypatch.setattr(plugin, "_notify_owner", notify)
    monkeypatch.setattr(
        plugin,
        "_create_public_channel",
        lambda *_args: (_ for _ in ()).throw(AssertionError("channel must already exist")),
    )

    assigned = asyncio.run(plugin._assign_inventory_to_job(41))

    assert assigned is True
    attach = next(args for query, args in database_calls if "SET inventory_id=$3" in query)
    assert attach[2] == 51
    assert "status='awaiting_username'" in next(
        query for query, _args in database_calls if "SET inventory_id=$3" in query
    )
    assert messages and "заранее подготовленный" in messages[-1]
    assert notifications and "выдан со склада" in notifications[-1]


def test_inventory_maintenance_creates_configured_minimum(monkeypatch):
    launched = []
    inserted = []
    settings = {
        "api_base_url": "https://smmway.ru/api/v2",
        "api_token_enc": "encrypted",
    }
    rule = {
        "id": 8, "enabled": True, "service_id": 10, "quantity": 100,
        "target_members": 100, "min_ready_channels": 3,
        "lot_id": "55", "lot_title": "Telegram channel",
    }

    class FakeDatabase:
        async def fetch(self, _query, *_args):
            return []

        async def fetchrow(self, query, *_args):
            if "COUNT(*) AS count" in query:
                return {"count": 0}
            raise AssertionError(query)

    class Client:
        @staticmethod
        def is_connected():
            return True

    class PluginManager:
        @staticmethod
        def is_enabled(_telegram_id, _uuid):
            return True

    async def insert(_settings):
        item = {"id": 100 + len(inserted)}
        inserted.append(item)
        return item

    monkeypatch.setattr(plugin, "_inventory_maintenance_lock", None)
    monkeypatch.setattr(plugin, "_telethon_accounts", lambda: [(1, Client())])
    monkeypatch.setattr(
        plugin,
        "_cardinal",
        SimpleNamespace(plugin_manager=PluginManager()),
    )
    monkeypatch.setattr(plugin, "_db", lambda: FakeDatabase())
    monkeypatch.setattr(plugin, "_telegram_id", lambda: 7)
    monkeypatch.setattr(plugin, "_settings", lambda: _async_value(settings))
    monkeypatch.setattr(plugin, "_rules", lambda **_kwargs: _async_value([rule]))
    monkeypatch.setattr(plugin, "_check_ready_inventory", lambda _settings: _async_value(None))
    monkeypatch.setattr(plugin, "_assign_waiting_jobs", lambda: _async_value(None))
    monkeypatch.setattr(plugin, "_insert_inventory_item", insert)
    monkeypatch.setattr(plugin, "_launch_inventory_item", launched.append)

    asyncio.run(plugin._maintain_inventory())

    assert len(inserted) == 3
    assert launched == [100, 101, 102]


def test_purchase_uses_prepared_inventory_instead_of_starting_boost(monkeypatch):
    events = []
    settings = {
        "api_base_url": "https://smmway.ru/api/v2",
        "api_token_enc": "encrypted",
    }
    rule = {
        "id": 4, "enabled": True, "service_id": 10, "quantity": 100,
        "target_members": 100, "min_ready_channels": 1,
        "lot_id": "55", "lot_title": "Готовый Telegram канал",
    }
    order = {
        "id": "ORDER-STOCK",
        "chat_id": "chat-5",
        "chat_name": "Buyer",
        "buyer_id": 9,
        "description": "Готовый Telegram канал, 1 шт.",
    }

    async def insert(_order, _rule):
        events.append("job")
        return {"id": 88}

    async def assign(job_id):
        events.append(("assign", job_id))
        return True

    async def maintain():
        events.append("replenish")

    async def forbidden_run(_job_id):
        raise AssertionError("boost must not start after purchase")

    monkeypatch.setattr(plugin, "_settings", lambda: _async_value(settings))
    monkeypatch.setattr(plugin, "_rules", lambda **_kwargs: _async_value([rule]))
    monkeypatch.setattr(plugin, "_insert_job", insert)
    monkeypatch.setattr(plugin, "_assign_inventory_to_job", assign)
    monkeypatch.setattr(plugin, "_maintain_inventory", maintain)
    monkeypatch.setattr(plugin, "_run_job", forbidden_run)

    asyncio.run(plugin._process_new_order(order))

    assert events == ["job", ("assign", 88), "replenish"]


def test_unbound_telegram_lot_does_not_trigger(monkeypatch):
    events = []
    settings = {"api_base_url": "https://smmway.ru/api/v2", "api_token_enc": "encrypted"}
    rule = {
        "id": 4, "enabled": True, "service_id": 10, "quantity": 100,
        "target_members": 100, "min_ready_channels": 1,
        "lot_id": "55", "lot_title": "Telegram канал 100 подписчиков",
    }
    order = {
        "id": "ORDER-OTHER", "chat_id": "chat", "chat_name": "Buyer",
        "buyer_id": 9, "description": "Telegram канал 500 подписчиков, 1 шт.",
        "lot_id": None,
    }

    monkeypatch.setattr(plugin, "_settings", lambda: _async_value(settings))
    monkeypatch.setattr(plugin, "_rules", lambda **_kwargs: _async_value([rule]))
    monkeypatch.setattr(plugin, "_insert_job", lambda *_args: events.append("job"))
    monkeypatch.setattr(plugin, "_notify_owner", lambda *_args: events.append("notify"))

    asyncio.run(plugin._process_new_order(order))

    assert events == []


def test_exact_lot_id_wins_over_similar_title():
    rules = [
        {"id": 1, "enabled": True, "lot_id": "55", "lot_title": "Telegram канал",
         "service_id": 1, "quantity": 1, "target_members": 1, "min_ready_channels": 1},
        {"id": 2, "enabled": True, "lot_id": "77", "lot_title": "Telegram канал VIP",
         "service_id": 2, "quantity": 2, "target_members": 2, "min_ready_channels": 1},
    ]
    assert plugin._match_rule("любое описание", rules, "77")["id"] == 2
    assert plugin._match_rule("Telegram канал VIP, 3 шт.", rules)["id"] == 2
    assert plugin._match_rule("Telegram канал VIP extra", rules) is None


def test_new_channels_use_least_loaded_telegram_account(monkeypatch):
    first, second = object(), object()

    class FakeDatabase:
        async def fetch(self, _query, *_args):
            return [
                {"telethon_session_id": 10, "count": 4},
                {"telethon_session_id": 11, "count": 1},
            ]

    monkeypatch.setattr(plugin, "_telethon_accounts", lambda: [(10, first), (11, second)])
    monkeypatch.setattr(plugin, "_db", lambda: FakeDatabase())
    monkeypatch.setattr(plugin, "_telegram_id", lambda: 7)

    assert asyncio.run(plugin._select_telethon_account()) == (11, second)


async def _async_value(value):
    return value
