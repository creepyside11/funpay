import asyncio
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
    messages = []
    job = {
        "id": 8,
        "status": "awaiting_owner_2fa",
        "buyer_username": "@buyer_name",
        "channel_id": 123,
        "channel_access_hash": 456,
        "order_id": "ORDER-1",
        "channel_url": "https://t.me/fptestchannel",
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
    monkeypatch.setattr(
        plugin,
        "_bot",
        lambda: SimpleNamespace(
            send_message=lambda chat_id, text: messages.append((chat_id, text))
        ),
    )

    asyncio.run(plugin._transfer_owner(8, "wrong-password", 42))

    assert "GetPasswordSettingsRequest" in calls
    assert "EditAdminRequest" not in calls
    assert messages and "Неверный пароль 2FA" in messages[-1][1]


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
    monkeypatch.setattr(
        plugin,
        "_bot",
        lambda: SimpleNamespace(send_message=lambda *_args, **_kwargs: None),
    )

    asyncio.run(plugin._transfer_owner(9, "correct-password", 42))

    assert calls == [
        "GetParticipantRequest",
        "GetPasswordRequest",
        "GetPasswordSettingsRequest",
        "EditAdminRequest",
        "GetPasswordRequest",
        "EditChatCreatorRequest",
    ]
    assert updates == [(9, {"status": "completed", "error_text": None})]


async def _async_value(value):
    return value
