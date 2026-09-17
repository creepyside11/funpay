"""Совместимый импорт для однофайловых плагинов FunPayCardinal.

Плагины могут использовать ``from cardinal import Cardinal, get_cardinal``.
Полная реализация менеджера находится в :mod:`plugin_system`.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from plugin_system import CardinalAdapter, PluginManager

Cardinal = CardinalAdapter
_current: ContextVar[CardinalAdapter | None] = ContextVar("current_cardinal", default=None)
_ORIGINAL_DISPATCH_TELEGRAM_MESSAGE = PluginManager.dispatch_telegram_message


def _ensure_database_compat(cardinal: CardinalAdapter) -> None:
    """Add asyncpg-style helpers expected by older/saved plugin sources."""
    db = getattr(getattr(cardinal, "plugin_manager", None), "db", None)
    if db is None or hasattr(db, "fetchval"):
        return

    async def fetchval(query: str, *args: Any) -> Any:
        row = await db.fetchrow(query, *args)
        return None if row is None else row[0]

    db.fetchval = fetchval


def _pending_input_handler(module: Any) -> Any:
    """Return the conventional settings-input handler used by a plugin."""
    for name in ("_on_setting_message", "_on_setting_msg"):
        handler = getattr(module, name, None)
        if callable(handler):
            return handler
    return None


async def _dispatch_telegram_message_with_pending_input(
    self: PluginManager, telegram_id: int, message: Any
) -> bool:
    """Deliver the next Telegram message directly to a plugin input wizard.

    Several Cardinal plugins keep the expected settings input in a module-level
    ``_pending_input`` value. Passing the aiogram message through the generic
    telebot compatibility conversion can lose fields or prevent the handler
    from matching. When a plugin is explicitly waiting for input, invoke its
    settings handler with the original message object instead.
    """
    runtime = self.runtimes.get(telegram_id)
    if runtime:
        for plugin in runtime.plugins.values():
            if not plugin.enabled:
                continue
            module = plugin.module
            pending = getattr(module, "_pending_input", None)
            handler = _pending_input_handler(module)
            if pending is None or not callable(handler):
                continue

            def run_pending_handler(
                current_plugin: Any = plugin,
                current_handler: Any = handler,
            ) -> None:
                set_cardinal(runtime.adapter)
                runtime.adapter.telegram.bot.current_plugin_uuid = current_plugin.uuid
                try:
                    current_handler(message)
                finally:
                    runtime.adapter.telegram.bot.current_plugin_uuid = None

            await asyncio.to_thread(run_pending_handler)
            return True

    return await _ORIGINAL_DISPATCH_TELEGRAM_MESSAGE(self, telegram_id, message)


def set_cardinal(cardinal: CardinalAdapter) -> None:
    _ensure_database_compat(cardinal)
    _current.set(cardinal)


def get_cardinal() -> CardinalAdapter | None:
    return _current.get()


if not getattr(PluginManager.dispatch_telegram_message, "_pending_input_compat", False):
    _dispatch_telegram_message_with_pending_input._pending_input_compat = True
    PluginManager.dispatch_telegram_message = _dispatch_telegram_message_with_pending_input
