import asyncio
from types import SimpleNamespace

import pytest

from ready_plugins import AIAssistant as plugin


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.anthropic.com/", "https://api.anthropic.com"),
        ("https://gateway.example/anthropic", "https://gateway.example/anthropic"),
    ],
)
def test_api_url_accepts_clean_https(value, expected):
    assert plugin._validate_api_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://api.anthropic.com",
        "https://user:pass@example.com",
        "https://example.com?token=secret",
        "not-a-url",
    ],
)
def test_api_url_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        plugin._validate_api_url(value)


def test_model_and_prompt_validation():
    assert plugin._validate_model_id("claude-sonnet-4-5") == "claude-sonnet-4-5"
    assert plugin._validate_system_prompt("Описание товара и правила ответа покупателю.")
    with pytest.raises(ValueError):
        plugin._validate_model_id("bad model id")
    with pytest.raises(ValueError):
        plugin._validate_system_prompt("коротко")


def test_longest_enabled_lot_title_wins():
    rules = [
        {"lot_title": "Telegram", "enabled": True},
        {"lot_title": "Telegram Premium", "enabled": True},
        {"lot_title": "Premium", "enabled": False},
    ]
    assert plugin._match_rule("Telegram Premium x2", rules) is rules[1]


def test_anthropic_sdk_receives_base_url_model_system_and_history(monkeypatch):
    captured = {}

    class Messages:
        @staticmethod
        def create(**kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="Готово")]
            )

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.messages = Messages()

        @staticmethod
        def close():
            captured["closed"] = True

    monkeypatch.setattr(plugin, "anthropic", SimpleNamespace(Anthropic=Client))
    monkeypatch.setattr(plugin, "_api_token", lambda _settings: "secret-token")
    settings = {
        "api_base_url": "https://gateway.example/anthropic",
        "model_id": "claude-sonnet-test",
    }
    messages = [{"role": "user", "content": "Вопрос"}]

    result = plugin._anthropic_request(settings, "Системный промпт", messages)

    assert result == "Готово"
    assert captured["client"] == {
        "api_key": "secret-token",
        "base_url": "https://gateway.example/anthropic",
        "timeout": 45,
        "max_retries": 1,
    }
    assert captured["request"] == {
        "model": "claude-sonnet-test",
        "max_tokens": 700,
        "system": "Системный промпт",
        "messages": messages,
    }
    assert captured["closed"] is True


def test_system_prompt_contains_stage_rules_and_seller_marker():
    prompt = plugin._build_system_prompt(
        {
            "stage": "after_purchase",
            "lot_title": "Тестовый товар",
            "system_prompt": "Товар выдаётся автоматически после оплаты.",
        }
    )
    assert "уже оформил заказ" in prompt
    assert "Тестовый товар" in prompt
    assert plugin.SELLER_MARKER in prompt
    assert "не запрашивай пароли" in prompt.casefold()


def test_regular_message_does_not_start_ai_without_help_command(monkeypatch):
    calls = []

    async def no_session(_message):
        calls.append("session_lookup")
        return None

    async def unexpected(_message):
        raise AssertionError("контекст лота нельзя искать без #помощь")

    monkeypatch.setattr(plugin, "_active_session", no_session)
    monkeypatch.setattr(plugin, "_handle_help_command", unexpected)

    asyncio.run(
        plugin._process_funpay_message(
            {
                "chat_id": "101",
                "chat_name": "Buyer",
                "buyer_id": 55,
                "text": "Расскажите о товаре",
            }
        )
    )
    assert calls == ["session_lookup"]


def test_help_command_activates_bound_lot_without_calling_ai(monkeypatch):
    sent = []
    activated = []
    rule = {"id": 8, "lot_title": "Лот", "system_prompt": "Промпт"}
    session = {
        "id": 3,
        "chat_id": "101",
        "chat_name": "Buyer",
        "lot_title": "Лот",
    }

    async def settings():
        return {
            "api_base_url": "https://api.anthropic.com",
            "api_token_enc": "encrypted",
            "model_id": "claude-test",
        }

    async def context(_message):
        return rule, "before_purchase", None

    async def activate(_message, selected, stage, order_id):
        activated.append((selected, stage, order_id))
        return session

    async def send(_target, text):
        sent.append(text)

    monkeypatch.setattr(plugin, "_settings", settings)
    monkeypatch.setattr(plugin, "_activation_context", context)
    monkeypatch.setattr(plugin, "_activate_session", activate)
    monkeypatch.setattr(plugin, "_funpay_send", send)

    asyncio.run(
        plugin._process_funpay_message(
            {
                "chat_id": "101",
                "chat_name": "Buyer",
                "buyer_id": 55,
                "text": "#ПОМОЩЬ",
            }
        )
    )

    assert activated == [(rule, "before_purchase", None)]
    assert "AI-помощник подключён" in sent[-1]
    assert "#продавец" in sent[-1]


def test_order_activation_uses_rule_id_not_context_row_id(monkeypatch):
    context = {
        "id": 900,
        "rule_id": 17,
        "lot_id": "123",
        "lot_title": "Купленный товар",
        "system_prompt": "Описание купленного товара и правила.",
        "enabled": True,
        "order_id": "ORDER-1",
    }

    async def viewing(*_args):
        return None, None, False

    async def order_context(_message):
        return context

    monkeypatch.setattr(plugin, "_cached_buyer_viewing", viewing)
    monkeypatch.setattr(plugin, "_order_context", order_context)

    result = asyncio.run(plugin._activation_context({"chat_id": "10"}))

    rule, stage, order_id = result
    assert rule["id"] == 17
    assert stage == "after_purchase"
    assert order_id == "ORDER-1"


def test_before_purchase_activation_uses_exact_viewing_lot_id(monkeypatch):
    selected_rule = {
        "id": 20,
        "lot_id": "555",
        "lot_title": "Просматриваемый товар",
    }

    async def viewing(*_args):
        return "555", "Просматриваемый товар", True

    async def rule_by_lot(lot_id):
        assert lot_id == "555"
        return selected_rule

    async def order_context(_message):
        return None

    monkeypatch.setattr(plugin, "_cached_buyer_viewing", viewing)
    monkeypatch.setattr(plugin, "_rule_by_lot_id", rule_by_lot)
    monkeypatch.setattr(plugin, "_order_context", order_context)

    result = asyncio.run(plugin._activation_context({"chat_id": "10"}))

    assert result == (selected_rule, "before_purchase", None)


def test_viewing_same_lot_after_purchase_uses_order_stage(monkeypatch):
    selected_rule = {"id": 20, "lot_id": "555", "lot_title": "Товар"}

    async def viewing(*_args):
        return "555", "Товар", True

    async def rule_by_lot(_lot_id):
        return selected_rule

    async def order_context(_message):
        return {"rule_id": 20, "order_id": "ORDER-55"}

    monkeypatch.setattr(plugin, "_cached_buyer_viewing", viewing)
    monkeypatch.setattr(plugin, "_rule_by_lot_id", rule_by_lot)
    monkeypatch.setattr(plugin, "_order_context", order_context)

    result = asyncio.run(plugin._activation_context({"chat_id": "10"}))

    assert result == (selected_rule, "after_purchase", "ORDER-55")


def test_manual_seller_command_stops_ai_dialog(monkeypatch):
    escalations = []
    session = {"id": 5, "chat_id": "seller-chat"}

    async def active(_message):
        return session

    async def escalate(_session, question, reason):
        escalations.append((question, reason))

    monkeypatch.setattr(plugin, "_active_session", active)
    monkeypatch.setattr(plugin, "_escalate", escalate)

    asyncio.run(
        plugin._process_funpay_message(
            {
                "chat_id": "seller-chat",
                "chat_name": "Buyer",
                "buyer_id": 55,
                "text": "#ПРОДАВЕЦ",
            }
        )
    )

    assert escalations == [
        (
            "Покупатель вызвал продавца командой #продавец.",
            "ручной вызов продавца",
        )
    ]


def test_escalation_stops_session_and_notifies_buyer_and_owner(monkeypatch):
    states = []
    buyer_messages = []
    owner_messages = []
    session = {
        "id": 5,
        "chat_id": "seller-chat",
        "chat_name": "Buyer",
        "lot_title": "Тестовый лот",
        "order_id": "ORDER-7",
    }

    async def state(session_id, **values):
        states.append((session_id, values))

    async def buyer(_session, text):
        buyer_messages.append(text)

    async def owner(text):
        owner_messages.append(text)

    monkeypatch.setattr(plugin, "_set_session_state", state)
    monkeypatch.setattr(plugin, "_funpay_send", buyer)
    monkeypatch.setattr(plugin, "_notify_owner", owner)

    asyncio.run(
        plugin._escalate(
            session,
            "Нужна замена товара",
            "решение принимает продавец",
        )
    )

    assert states == [(5, {"active": False, "escalated": True})]
    assert "позвал продавца" in buyer_messages[-1]
    assert "ORDER-7" in owner_messages[-1]
    assert "Нужна замена товара" in owner_messages[-1]


def test_ai_marker_stops_session_and_calls_seller(monkeypatch):
    saved = []
    escalations = []
    session = {
        "id": 4,
        "chat_id": "marker-chat",
        "chat_name": "Buyer",
        "rule_id": 8,
        "lot_title": "Лот",
        "stage": "before_purchase",
        "system_prompt": "Описание товара и правила ответа.",
    }

    async def active(_message):
        return session

    async def history(_session):
        return []

    async def settings():
        return {"model_id": "test"}

    async def save(_session, role, content):
        saved.append((role, content))

    async def escalate(_session, question, reason):
        escalations.append((question, reason))

    monkeypatch.setattr(plugin, "_active_session", active)
    monkeypatch.setattr(plugin, "_history", history)
    monkeypatch.setattr(plugin, "_settings", settings)
    monkeypatch.setattr(
        plugin,
        "_anthropic_request",
        lambda *_args, **_kwargs: f"{plugin.SELLER_MARKER} Нужна проверка продавца",
    )
    monkeypatch.setattr(plugin, "_save_message", save)
    monkeypatch.setattr(plugin, "_escalate", escalate)

    asyncio.run(
        plugin._process_funpay_message(
            {
                "chat_id": "marker-chat",
                "chat_name": "Buyer",
                "buyer_id": 55,
                "text": "Можно оформить возврат?",
            }
        )
    )

    assert saved == [("user", "Можно оформить возврат?")]
    assert escalations == [
        ("Можно оформить возврат?", "Нужна проверка продавца")
    ]


async def _async_value(value):
    return value
