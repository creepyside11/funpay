# ruff: noqa: N999 - имя модуля повторяет публичный импорт FunPayCardinal.
"""Callback prefixes, совместимые с FunPayCardinal.

Плагины обычно используют ``from tg_bot import CBT`` и регистрируют обработчик
для ``f"{CBT.PLUGIN_SETTINGS}:{UUID}:"``.
"""

PLUGIN_SETTINGS = "47"
