import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ready_plugins import EmeraldPromo as plugin


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10000", 10_000),
        ("200 000", 200_000),
        ("1000000000", 1_000_000_000),
    ],
)
def test_token_amount_accepts_current_emerald_range(value, expected):
    assert plugin._validate_token_amount(value) == expected


@pytest.mark.parametrize("value", ["9999", "1000000001", "10.5", ""])
def test_token_amount_rejects_values_outside_current_range(value):
    with pytest.raises(ValueError):
        plugin._validate_token_amount(value)


def test_paid_amount_is_multiplied_and_stays_one_nominal():
    assert plugin._calculate_total(50_000, 3) == 150_000
    assert plugin._calculate_total(200_000, None) == 200_000


def test_lot_matching_is_exact_and_prefers_event_lot_id():
    rules = [
        {"lot_id": "10", "lot_title": "Telegram Premium", "enabled": True},
        {"lot_id": "11", "lot_title": "Telegram", "enabled": True},
    ]

    assert plugin._match_rule("Telegram Premium, 3 шт.", rules) is rules[0]
    assert plugin._match_rule("Best Telegram Premium", rules) is None
    assert plugin._match_rule("Telegram Premium", rules, "11") is rules[1]
    assert plugin._match_rule("Telegram Premium", rules, "999") is None


def test_funpay_registration_age_is_parsed_from_profile_header():
    profile = SimpleNamespace(
        html="""
        <div class="profile-header">
          <div class="param-item">Дата регистрации <span class="text-nowrap">1 августа 2026</span></div>
        </div>
        """
    )

    age = plugin._profile_age_days(
        profile, datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    )

    assert age == 9


def test_api_request_uses_bearer_json_and_disables_redirects(monkeypatch):
    captured = {}

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"data": {"balance_tokens": 900_000}}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return Response()

    monkeypatch.setattr(plugin, "_api_token", lambda _settings: "sk-em-seller-test")
    monkeypatch.setattr(plugin.requests, "request", fake_request)

    result = plugin._api_request(
        {"api_base_url": "https://emeraldai.sbs/seller/v1"},
        "POST",
        "promo-codes",
        json_payload={"type": "tokens", "token_amount": 200_000, "quantity": 1},
    )

    assert result["data"]["balance_tokens"] == 900_000
    assert captured["method"] == "POST"
    assert captured["url"] == "https://emeraldai.sbs/seller/v1/promo-codes"
    assert captured["headers"]["Authorization"] == "Bearer sk-em-seller-test"
    assert captured["allow_redirects"] is False
    assert captured["timeout"] == 25


def test_account_minimum_never_falls_below_new_10000_limit():
    assert plugin._server_minimum({}) == 10_000
    assert plugin._server_minimum({"pricing": {"minimum_token_promo": 5_000}}) == 10_000
    assert plugin._server_minimum({"pricing": {"minimum_token_promo": 25_000}}) == 25_000


def test_repeated_free_command_resends_saved_code_without_new_api_call(monkeypatch):
    sent = []
    issue = {
        "id": 7,
        "kind": "free",
        "status": "sent",
        "promo_code": "FREE-CODE",
        "token_amount": 200_000,
        "chat_id": "123",
        "chat_name": "buyer",
    }

    async def by_source(kind, source_id):
        assert (kind, source_id) == ("free", "42")
        return issue

    async def send(_issue, text):
        sent.append(text)

    monkeypatch.setattr(plugin, "_issue_by_source", by_source)
    monkeypatch.setattr(plugin, "_funpay_send", send)

    asyncio.run(plugin._process_free({
        "buyer_id": 42, "chat_id": "123", "chat_name": "buyer"
    }))

    assert len(sent) == 1
    assert "FREE-CODE" in sent[0]
    assert "один раз" in sent[0]


def test_seller_token_is_saved_even_if_temporary_wizard_state_was_lost(monkeypatch):
    saved = []
    messages = []

    class Bot:
        @staticmethod
        def delete_message(*_args):
            return None

        @staticmethod
        def send_message(_chat_id, text, **_kwargs):
            messages.append(text)

    class Secrets:
        @staticmethod
        def encrypt(value):
            return f"encrypted:{value}"

    def sync(value):
        saved.append(value)

    monkeypatch.setattr(plugin, "_pending_input", None)
    monkeypatch.setattr(plugin, "_bot", lambda: Bot())
    monkeypatch.setattr(plugin, "_secret_box", lambda: Secrets())
    monkeypatch.setattr(plugin, "_set_setting", lambda column, value: (column, value))
    monkeypatch.setattr(plugin, "_sync", sync)
    monkeypatch.setattr(plugin, "_show_settings", lambda _chat_id: None)

    plugin._on_setting_message(SimpleNamespace(
        text="sk-em-seller-1234567890",
        chat=SimpleNamespace(id=10),
        message_id=20,
    ))

    assert saved == [("api_token_enc", "encrypted:sk-em-seller-1234567890")]
    assert messages == ["✅ Настройка сохранена."]


def test_review_bonus_is_reserved_only_for_actual_five_star_review(monkeypatch):
    reserved = []
    fulfilled = []
    sale = {
        "buyer_id": 42,
        "chat_id": "123",
        "chat_name": "buyer",
        "review_bonus_enabled": True,
        "review_bonus_tokens": 1_000_000,
    }

    class Account:
        id = 99

        @staticmethod
        def get_order(_order_id):
            return SimpleNamespace(
                seller_id=99, review=SimpleNamespace(stars=5)
            )

    async def by_source(kind, source_id):
        assert (kind, source_id) == ("sale", "ABCD1234")
        return sale

    async def reserve(**kwargs):
        reserved.append(kwargs)
        return {"id": 8, "status": "creating"}, True

    async def fulfill(issue_id):
        fulfilled.append(issue_id)

    monkeypatch.setattr(plugin, "_cardinal", SimpleNamespace(account=Account()))
    monkeypatch.setattr(plugin, "_issue_by_source", by_source)
    monkeypatch.setattr(plugin, "_reserve_issue", reserve)
    monkeypatch.setattr(plugin, "_fulfill_issue", fulfill)

    asyncio.run(plugin._process_review(SimpleNamespace(
        order_id="ABCD1234", text="Покупатель оставил отзыв"
    )))

    assert reserved[0]["kind"] == "review"
    assert reserved[0]["source_id"] == "ABCD1234"
    assert reserved[0]["token_amount"] == 1_000_000
    assert fulfilled == [8]
