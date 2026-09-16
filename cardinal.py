"""Совместимый импорт для однофайловых плагинов FunPayCardinal.

Плагины могут использовать ``from cardinal import Cardinal, get_cardinal``.
Полная реализация менеджера находится в :mod:`plugin_system`.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from plugin_system import CardinalAdapter

Cardinal = CardinalAdapter
_current: ContextVar[CardinalAdapter | None] = ContextVar("current_cardinal", default=None)


def _ensure_database_compat(cardinal: CardinalAdapter) -> None:
    """Add asyncpg-style helpers expected by older/saved plugin sources."""
    db = getattr(getattr(cardinal, "plugin_manager", None), "db", None)
    if db is None or hasattr(db, "fetchval"):
        return

    async def fetchval(query: str, *args: Any) -> Any:
        row = await db.fetchrow(query, *args)
        return None if row is None else row[0]

    db.fetchval = fetchval


def set_cardinal(cardinal: CardinalAdapter) -> None:
    _ensure_database_compat(cardinal)
    _current.set(cardinal)


def get_cardinal() -> CardinalAdapter | None:
    return _current.get()
