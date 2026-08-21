"""Совместимый импорт для однофайловых плагинов FunPayCardinal.

Плагины могут использовать ``from cardinal import Cardinal, get_cardinal``.
Полная реализация менеджера находится в :mod:`plugin_system`.
"""

from __future__ import annotations

from contextvars import ContextVar

from plugin_system import CardinalAdapter

Cardinal = CardinalAdapter
_current: ContextVar[CardinalAdapter | None] = ContextVar("current_cardinal", default=None)


def set_cardinal(cardinal: CardinalAdapter) -> None:
    _current.set(cardinal)


def get_cardinal() -> CardinalAdapter | None:
    return _current.get()
