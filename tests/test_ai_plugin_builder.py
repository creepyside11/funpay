import asyncio
from types import SimpleNamespace

import pytest

import ai_plugin_builder as builder_module
from ai_plugin_builder import (
    AIPluginBuilderError,
    AnthropicPluginBuilder,
    generated_filename,
    inspect_generated_source,
    parse_generated_plugin,
    validate_api_base_url,
    validate_model_id,
    validate_plugin_request,
)


PLUGIN_UUID = "12345678-1234-4234-9234-123456789abc"


def plugin_source(uuid=PLUGIN_UUID, version="1.0.0"):
    return f'''NAME = "AI Test"
VERSION = "{version}"
DESCRIPTION = "Generated plugin"
CREDITS = "AI Plugin Builder"
SETTINGS_PAGE = False
UUID = "{uuid}"
BIND_TO_DELETE = None
BIND_TO_PRE_INIT = []
BIND_TO_POST_INIT = []
BIND_TO_PRE_START = []
BIND_TO_POST_START = []
BIND_TO_PRE_STOP = []
BIND_TO_POST_STOP = []
BIND_TO_INIT_MESSAGE = []
BIND_TO_MESSAGES_LIST_CHANGED = []
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = []
BIND_TO_NEW_MESSAGE = []
BIND_TO_INIT_ORDER = []
BIND_TO_NEW_ORDER = []
BIND_TO_ORDERS_LIST_CHANGED = []
BIND_TO_ORDER_STATUS_CHANGED = []
BIND_TO_PRE_DELIVERY = []
BIND_TO_POST_DELIVERY = []
BIND_TO_PRE_LOTS_RAISE = []
BIND_TO_POST_LOTS_RAISE = []
'''


def envelope(source, summary="Готово"):
    return f"<plugin_source>\n{source}</plugin_source>\n<summary>{summary}</summary>"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://api.anthropic.com/", "https://api.anthropic.com"),
        ("https://proxy.example/v1", "https://proxy.example/v1"),
    ],
)
def test_builder_base_url_validation(value, expected):
    assert validate_api_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["http://api.example", "https://user:pass@example.com", "https://example.com?q=1"],
)
def test_builder_rejects_unsafe_base_url(value):
    with pytest.raises(ValueError):
        validate_api_base_url(value)


def test_builder_validates_model_and_request():
    assert validate_model_id("claude-sonnet-custom") == "claude-sonnet-custom"
    assert validate_plugin_request("Создай полезный плагин с обработкой заказов")
    with pytest.raises(ValueError):
        validate_model_id("bad model id")
    with pytest.raises(ValueError):
        validate_plugin_request("коротко")


def test_generated_source_metadata_and_filename():
    metadata = inspect_generated_source(plugin_source())
    assert metadata["UUID"] == PLUGIN_UUID
    assert metadata["NAME"] == "AI Test"
    assert generated_filename(metadata["NAME"], metadata["UUID"]) == "AI_Test_12345678.py"


def test_edit_must_keep_uuid():
    with pytest.raises(AIPluginBuilderError, match="изменила UUID"):
        inspect_generated_source(
            plugin_source("aaaaaaaa-1234-4234-9234-123456789abc"),
            expected_uuid=PLUGIN_UUID,
        )


@pytest.mark.parametrize(
    "dangerous",
    ["import subprocess\n", "from ctypes import CDLL\n", "value = eval('1')\n"],
)
def test_auto_install_blocks_dangerous_constructs(dangerous):
    with pytest.raises(AIPluginBuilderError, match="запрещает"):
        inspect_generated_source(dangerous + plugin_source())


def test_model_envelope_accepts_plain_or_fenced_source():
    plain = parse_generated_plugin(envelope(plugin_source(), "Сделан тест"))
    fenced = parse_generated_plugin(
        envelope(f"```python\n{plugin_source()}\n```", "Проверен")
    )
    assert plain.summary == "Сделан тест"
    assert fenced.source.startswith('NAME = "AI Test"')


def test_official_anthropic_sdk_gets_docs_in_both_stages(monkeypatch):
    calls = []
    clients = []

    class FakeMessages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            version = "1.0.0" if len(calls) == 1 else "1.0.1"
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=envelope(plugin_source(version=version)))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.messages = FakeMessages()
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        builder_module,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=FakeClient),
    )
    builder = AnthropicPluginBuilder(
        "secret-token", "https://api.anthropic.com", "claude-test-model"
    )

    async def run():
        draft = await builder.create_draft(
            "Создай полезный плагин для автоматического ответа покупателю",
            "УНИКАЛЬНАЯ ДОКУМЕНТАЦИЯ",
        )
        return await builder.review_draft(
            "Создай полезный плагин для автоматического ответа покупателю",
            "УНИКАЛЬНАЯ ДОКУМЕНТАЦИЯ",
            draft,
        )

    reviewed = asyncio.run(run())

    assert len(calls) == 2
    assert all("УНИКАЛЬНАЯ ДОКУМЕНТАЦИЯ" in call["system"] for call in calls)
    assert calls[0]["model"] == "claude-test-model"
    assert calls[0]["messages"][0]["role"] == "user"
    assert all(client.kwargs["api_key"] == "secret-token" for client in clients)
    assert all(client.closed for client in clients)
    assert 'VERSION = "1.0.1"' in reviewed.source
