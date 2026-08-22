from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

logger = logging.getLogger("funpay_bot.playerok_plugins")

PLAYEROK_PLUGIN_FIELDS = (
    "NAME",
    "VERSION",
    "DESCRIPTION",
    "CREDITS",
    "UUID",
    "SETTINGS_PAGE",
    "SETTINGS",
    "ACTIONS",
    "BIND_TO_DELETE",
)

PLAYEROK_HOOK_NAMES = (
    "BIND_TO_START",
    "BIND_TO_STOP",
    "BIND_TO_TICK",
    "BIND_TO_NEW_MESSAGE",
    "BIND_TO_DEAL_CHANGED",
    "BIND_TO_NEW_REVIEW",
    "BIND_TO_SETTING_CHANGED",
)

SETTING_TYPES = {"bool", "int", "str", "choice"}


class PlayerokPluginValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlayerokReadyPluginSpec:
    uuid: str
    filename: str
    name: str
    version: str
    description: str
    details: str
    source: str


@dataclass(slots=True)
class PlayerokPluginData:
    name: str
    version: str
    description: str
    credits: str
    uuid: str
    filename: str
    module: ModuleType
    settings_page: bool
    settings_schema: dict[str, dict[str, Any]]
    actions: dict[str, dict[str, Any]]
    delete_handler: Callable[..., Any] | None
    enabled: bool = True
    hooks: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)


@dataclass(slots=True)
class PlayerokPluginRuntime:
    playerok_runtime: Any
    plugins: dict[str, PlayerokPluginData] = field(default_factory=dict)
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    states: dict[str, dict[str, Any]] = field(default_factory=dict)


class PlayerokPluginContext:
    """Контекст, передаваемый обработчикам Playerok-плагина."""

    def __init__(
        self,
        manager: PlayerokPluginManager,
        telegram_id: int,
        plugin_uuid: str,
        runtime: PlayerokPluginRuntime,
    ):
        self.manager = manager
        self.telegram_id = telegram_id
        self.plugin_uuid = plugin_uuid
        self.runtime = runtime.playerok_runtime
        self.account = runtime.playerok_runtime.account
        self.state = runtime.states.setdefault(plugin_uuid, {})
        self._settings = runtime.settings.setdefault(plugin_uuid, {})

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def notify(self, text: str, **kwargs: Any) -> None:
        """Планирует Telegram-уведомление владельцу без блокировки хука."""
        loop = self.manager.loop
        if loop is None or loop.is_closed():
            return
        body = f"🔵 <b>Playerok · плагин</b>\n{text!s}"
        asyncio.run_coroutine_threadsafe(
            self.manager.bot.send_message(self.telegram_id, body, **kwargs),
            loop,
        )


def _validate_uuid(value: str) -> str:
    try:
        parsed = UUID(str(value), version=4)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlayerokPluginValidationError(
            "UUID плагина должен быть корректным UUID4"
        ) from exc
    if str(parsed) != str(value):
        raise PlayerokPluginValidationError(
            "UUID плагина должен быть в каноническом нижнем регистре"
        )
    return str(parsed)


def _normalize_setting_value(spec: dict[str, Any], value: Any) -> Any:
    kind = spec["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "да"}:
            return True
        if normalized in {"0", "false", "no", "off", "нет"}:
            return False
        raise ValueError("ожидается логическое значение")
    if kind == "int":
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("ожидается целое число") from exc
        minimum = int(spec.get("min", -2_147_483_648))
        maximum = int(spec.get("max", 2_147_483_647))
        if not minimum <= number <= maximum:
            raise ValueError(f"значение должно быть от {minimum} до {maximum}")
        return number
    text = str(value).strip()
    if kind == "choice":
        choices = spec["choices"]
        if text not in choices:
            raise ValueError("значение отсутствует в списке вариантов")
        return text
    minimum = int(spec.get("min_length", 0))
    maximum = int(spec.get("max_length", 2000))
    if not minimum <= len(text) <= maximum:
        raise ValueError(f"длина должна быть от {minimum} до {maximum} символов")
    return text


def _validate_settings(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise PlayerokPluginValidationError("SETTINGS должен быть словарём")
    result: dict[str, dict[str, Any]] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.isidentifier() or len(key) > 40:
            raise PlayerokPluginValidationError(
                "ключ настройки должен быть Python-идентификатором до 40 символов"
            )
        if not isinstance(raw, dict):
            raise PlayerokPluginValidationError(f"SETTINGS[{key!r}] должен быть словарём")
        spec = dict(raw)
        kind = str(spec.get("type", "str"))
        if kind not in SETTING_TYPES:
            raise PlayerokPluginValidationError(
                f"неподдерживаемый тип настройки {key}: {kind}"
            )
        label = str(spec.get("label", key)).strip()
        if not 1 <= len(label) <= 48:
            raise PlayerokPluginValidationError(
                f"label настройки {key} должен содержать 1–48 символов"
            )
        spec["type"] = kind
        spec["label"] = label
        if kind == "choice":
            choices = spec.get("choices")
            if not isinstance(choices, dict) or not 2 <= len(choices) <= 20:
                raise PlayerokPluginValidationError(
                    f"choices настройки {key} должен содержать 2–20 вариантов"
                )
            spec["choices"] = {
                str(choice): str(title)[:48] for choice, title in choices.items()
            }
        try:
            spec["default"] = _normalize_setting_value(
                spec, spec.get("default", False if kind == "bool" else "")
            )
        except ValueError as exc:
            raise PlayerokPluginValidationError(
                f"некорректный default настройки {key}: {exc}"
            ) from exc
        result[key] = spec
    return result


def _validate_actions(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise PlayerokPluginValidationError("ACTIONS должен быть словарём")
    result: dict[str, dict[str, Any]] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.isidentifier() or len(key) > 32:
            raise PlayerokPluginValidationError(
                "ID действия должен быть Python-идентификатором до 32 символов"
            )
        if not isinstance(raw, dict) or not callable(raw.get("handler")):
            raise PlayerokPluginValidationError(
                f"ACTIONS[{key!r}] должен содержать вызываемый handler"
            )
        label = str(raw.get("label", key)).strip()
        if not 1 <= len(label) <= 48:
            raise PlayerokPluginValidationError(
                f"label действия {key} должен содержать 1–48 символов"
            )
        result[key] = {"label": label, "handler": raw["handler"]}
    return result


def _auto_restore_source() -> str:
    return '''from __future__ import annotations

import html
import time

NAME = "Playerok Auto Restore"
VERSION = "1.0.0"
DESCRIPTION = "Автоматически публикует черновики Playerok с бесплатным приоритетом."
CREDITS = "FunPay aiogram bot"
UUID = "4f74c693-1bd0-4b36-a20e-28d72a0d4411"
SETTINGS_PAGE = True
SETTINGS = {
    "enabled": {"label": "Автовосстановление", "type": "bool", "default": False},
    "interval_minutes": {"label": "Интервал, минут", "type": "int", "default": 30, "min": 5, "max": 1440},
    "max_per_run": {"label": "Максимум за запуск", "type": "int", "default": 10, "min": 1, "max": 24},
    "notifications": {"label": "Уведомления", "type": "bool", "default": True},
}


def _publish(ctx):
    from playerokapi.enums import ItemStatuses

    page = ctx.account.get_my_items(statuses=[ItemStatuses.DRAFT], count=24)
    drafts = list(getattr(page, "items", []) or [])[: int(ctx.get_setting("max_per_run", 10))]
    published = 0
    errors = []
    for item in drafts:
        try:
            priorities = ctx.account.get_item_priority_statuses(item.id, item.price)
            free = next((status for status in priorities if int(status.price or 0) == 0), None)
            if free is None:
                raise RuntimeError("нет бесплатного статуса публикации")
            ctx.account.publish_item(item.id, free.id)
            published += 1
        except Exception as exc:
            errors.append(f"{getattr(item, 'name', item.id)}: {exc}")
    ctx.state["last_run"] = time.monotonic()
    result = f"📢 <b>Playerok Auto Restore</b>\\n\\nОпубликовано: <b>{published}/{len(drafts)}</b>"
    if errors:
        result += f"\\nОшибок: <b>{len(errors)}</b>\\n<code>{html.escape(str(errors[0])[:500])}</code>"
    return result


def run_now(ctx):
    return _publish(ctx)


def on_tick(ctx):
    if not ctx.get_setting("enabled", False):
        return
    interval = int(ctx.get_setting("interval_minutes", 30)) * 60
    if time.monotonic() - float(ctx.state.get("last_run", 0)) < interval:
        return
    result = _publish(ctx)
    if ctx.get_setting("notifications", True):
        ctx.notify(result)


ACTIONS = {"run_now": {"label": "▶️ Запустить сейчас", "handler": run_now}}
BIND_TO_TICK = [on_tick]
BIND_TO_START = []
BIND_TO_STOP = []
BIND_TO_NEW_MESSAGE = []
BIND_TO_DEAL_CHANGED = []
BIND_TO_NEW_REVIEW = []
BIND_TO_SETTING_CHANGED = []
BIND_TO_DELETE = None
'''


def _advanced_stats_source() -> str:
    return '''from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

NAME = "Playerok Advanced Stats"
VERSION = "1.0.0"
DESCRIPTION = "Показывает расширенную статистику продаж Playerok за выбранный период."
CREDITS = "FunPay aiogram bot"
UUID = "6a9bf914-cc4f-45c1-954c-fb94cc302522"
SETTINGS_PAGE = True
SETTINGS = {
    "period_days": {"label": "Период", "type": "choice", "default": "30", "choices": {"7": "7 дней", "30": "30 дней", "90": "90 дней", "365": "365 дней"}},
    "pages": {"label": "Страниц истории", "type": "int", "default": 5, "min": 1, "max": 10},
}


def _date(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def show_stats(ctx):
    from playerokapi.enums import ItemDealDirections

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(ctx.get_setting("period_days", "30")))
    deals = []
    cursor = None
    for _ in range(int(ctx.get_setting("pages", 5))):
        page = ctx.account.get_deals(direction=ItemDealDirections.OUT, count=24, after_cursor=cursor)
        batch = list(getattr(page, "deals", []) or [])
        deals.extend(batch)
        info = getattr(page, "page_info", None)
        cursor = getattr(info, "end_cursor", None)
        if not batch or not getattr(info, "has_next_page", False) or not cursor:
            break
    selected = [deal for deal in deals if (_date(getattr(deal, "created_at", None)) or cutoff) >= cutoff]
    statuses = Counter(getattr(getattr(deal, "status", None), "name", "UNKNOWN") for deal in selected)
    completed = [deal for deal in selected if getattr(getattr(deal, "status", None), "name", "") in {"SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"}]
    revenue = sum(float(getattr(getattr(deal, "item", None), "price", 0) or 0) for deal in completed)
    buyers = {str(getattr(getattr(deal, "user", None), "id", "")) for deal in completed if getattr(getattr(deal, "user", None), "id", None)}
    popular = Counter(getattr(getattr(deal, "item", None), "name", "Без названия") for deal in completed)
    top = popular.most_common(3)
    lines = [
        "📊 <b>Расширенная статистика Playerok</b>",
        "",
        f"Период: <b>{ctx.get_setting('period_days', '30')} дней</b>",
        f"Сделок найдено: <b>{len(selected)}</b>",
        f"Успешных: <b>{len(completed)}</b>",
        f"Покупателей: <b>{len(buyers)}</b>",
        f"Выручка: <b>{revenue:,.2f} ₽</b>".replace(",", " "),
        f"Активных/ожидающих: <b>{statuses.get('PAID', 0) + statuses.get('PENDING', 0)}</b>",
    ]
    if top:
        lines.extend(["", "🏆 <b>Популярные объявления</b>"])
        lines.extend(f"• {name[:80]} — {count}" for name, count in top)
    if len(deals) >= int(ctx.get_setting("pages", 5)) * 24:
        lines.extend(["", "ℹ️ История ограничена настройкой количества страниц."])
    return "\\n".join(lines)


ACTIONS = {"show_stats": {"label": "📊 Показать статистику", "handler": show_stats}}
BIND_TO_START = []
BIND_TO_STOP = []
BIND_TO_TICK = []
BIND_TO_NEW_MESSAGE = []
BIND_TO_DEAL_CHANGED = []
BIND_TO_NEW_REVIEW = []
BIND_TO_SETTING_CHANGED = []
BIND_TO_DELETE = None
'''


PLAYEROK_READY_PLUGINS = (
    PlayerokReadyPluginSpec(
        "4f74c693-1bd0-4b36-a20e-28d72a0d4411",
        "PlayerokAutoRestore.py",
        "Playerok Auto Restore",
        "1.0.0",
        "Автоматическое восстановление объявлений",
        "Ищет черновики Playerok и публикует их с бесплатным приоритетом. "
        "Настраиваются включение, интервал, лимит за один запуск и уведомления; "
        "есть ручная кнопка запуска.",
        _auto_restore_source(),
    ),
    PlayerokReadyPluginSpec(
        "6a9bf914-cc4f-45c1-954c-fb94cc302522",
        "PlayerokAdvancedStats.py",
        "Playerok Advanced Stats",
        "1.0.0",
        "Расширенная статистика сделок",
        "Считает успешные и ожидающие сделки, покупателей, выручку и популярные "
        "объявления. Настраиваются период и глубина загрузки истории.",
        _advanced_stats_source(),
    ),
)

PLAYEROK_READY_PLUGIN_BY_UUID = {
    plugin.uuid: plugin for plugin in PLAYEROK_READY_PLUGINS
}


class PlayerokPluginManager:
    """Загрузчик однофайловых плагинов Playerok SDK."""

    def __init__(self, db: Any, bot: Any):
        self.db = db
        self.bot = bot
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runtimes: dict[int, PlayerokPluginRuntime] = {}
        self.root = Path("playerok_plugins_runtime")

    @staticmethod
    def validate_source(filename: str, source: str) -> None:
        if not filename.lower().endswith(".py"):
            raise PlayerokPluginValidationError("поддерживаются только одиночные файлы .py")
        if len(source.encode("utf-8")) > 512 * 1024:
            raise PlayerokPluginValidationError("размер плагина не должен превышать 512 КБ")
        if source.splitlines() and "noplug" in source.splitlines()[0].split():
            raise PlayerokPluginValidationError("файл помечен автором как noplug")

    def _write_source(self, telegram_id: int, uuid: str, source: str) -> Path:
        directory = self.root / str(telegram_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid}.py"
        path.write_text(source, encoding="utf-8")
        return path

    def _load_module(
        self,
        telegram_id: int,
        filename: str,
        source: str,
        enabled: bool,
        expected_uuid: str | None = None,
    ) -> PlayerokPluginData:
        self.validate_source(filename, source)
        preliminary_uuid = expected_uuid or "pending"
        path = self._write_source(telegram_id, preliminary_uuid, source)
        module_name = f"playerok_plugin_{telegram_id}_{preliminary_uuid.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            raise PlayerokPluginValidationError("не удалось создать модуль плагина")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            missing = [field for field in PLAYEROK_PLUGIN_FIELDS if not hasattr(module, field)]
            if missing:
                raise PlayerokPluginValidationError(
                    "отсутствуют обязательные поля: " + ", ".join(missing)
                )
            uuid = _validate_uuid(str(module.UUID))
            if expected_uuid and uuid != expected_uuid:
                raise PlayerokPluginValidationError("UUID в БД и файле плагина не совпадает")
            settings = _validate_settings(module.SETTINGS)
            actions = _validate_actions(module.ACTIONS)
            hooks: dict[str, list[Callable[..., Any]]] = {}
            for name in PLAYEROK_HOOK_NAMES:
                value = getattr(module, name, [])
                if not isinstance(value, (list, tuple)) or not all(callable(item) for item in value):
                    raise PlayerokPluginValidationError(f"{name} должен быть списком функций")
                hooks[name] = list(value)
            delete_handler = module.BIND_TO_DELETE
            if delete_handler is not None and not callable(delete_handler):
                raise PlayerokPluginValidationError(
                    "BIND_TO_DELETE должен быть функцией или None"
                )
            if preliminary_uuid != uuid:
                old_path = path
                path = self._write_source(telegram_id, uuid, source)
                module.__file__ = str(path)
                old_path.unlink(missing_ok=True)
            return PlayerokPluginData(
                name=str(module.NAME),
                version=str(module.VERSION),
                description=str(module.DESCRIPTION),
                credits=str(module.CREDITS),
                uuid=uuid,
                filename=filename,
                module=module,
                settings_page=bool(module.SETTINGS_PAGE),
                settings_schema=settings,
                actions=actions,
                delete_handler=delete_handler,
                enabled=enabled,
                hooks=hooks,
            )
        except Exception:
            sys.modules.pop(module_name, None)
            path.unlink(missing_ok=True)
            raise

    async def _load_settings(
        self, telegram_id: int, plugin: PlayerokPluginData
    ) -> dict[str, Any]:
        stored = await self.db.list_playerok_plugin_settings(telegram_id, plugin.uuid)
        result: dict[str, Any] = {}
        for key, spec in plugin.settings_schema.items():
            raw = stored.get(key, spec["default"])
            try:
                result[key] = _normalize_setting_value(spec, raw)
            except ValueError:
                result[key] = spec["default"]
        return result

    async def load_runtime(self, telegram_id: int, runtime: Any) -> PlayerokPluginRuntime:
        self.loop = asyncio.get_running_loop()
        plugin_runtime = PlayerokPluginRuntime(runtime)
        self.runtimes[telegram_id] = plugin_runtime
        for row in await self.db.list_playerok_plugins(telegram_id):
            try:
                plugin = await asyncio.to_thread(
                    self._load_module,
                    telegram_id,
                    row["filename"],
                    row["source"],
                    bool(row["enabled"]),
                    row["uuid"],
                )
                plugin_runtime.plugins[plugin.uuid] = plugin
                plugin_runtime.settings[plugin.uuid] = await self._load_settings(
                    telegram_id, plugin
                )
            except Exception:
                logger.exception("Не удалось восстановить Playerok-плагин %s", row["uuid"])
        await self.dispatch(telegram_id, "BIND_TO_START")
        return plugin_runtime

    async def install(
        self, telegram_id: int, filename: str, source: str, runtime: Any
    ) -> PlayerokPluginData:
        plugin_runtime = self.runtimes.get(telegram_id)
        if not plugin_runtime:
            plugin_runtime = await self.load_runtime(telegram_id, runtime)
        plugin = await asyncio.to_thread(
            self._load_module, telegram_id, filename, source, True
        )
        await self.db.upsert_playerok_plugin(telegram_id, plugin, source)
        old = plugin_runtime.plugins.get(plugin.uuid)
        if old:
            sys.modules.pop(old.module.__name__, None)
        plugin_runtime.plugins[plugin.uuid] = plugin
        plugin_runtime.settings[plugin.uuid] = await self._load_settings(
            telegram_id, plugin
        )
        await self.dispatch(telegram_id, "BIND_TO_START", only=plugin.uuid)
        return plugin

    def is_enabled(self, telegram_id: int, uuid: str) -> bool:
        runtime = self.runtimes.get(telegram_id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        return bool(plugin and plugin.enabled)

    def context(self, telegram_id: int, uuid: str) -> PlayerokPluginContext:
        runtime = self.runtimes[telegram_id]
        return PlayerokPluginContext(self, telegram_id, uuid, runtime)

    async def _call(self, handler: Callable[..., Any], *args: Any) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(*args)
        return await asyncio.to_thread(handler, *args)

    async def dispatch(
        self,
        telegram_id: int,
        hook: str,
        *args: Any,
        only: str | None = None,
    ) -> None:
        runtime = self.runtimes.get(telegram_id)
        if not runtime:
            return
        for uuid, plugin in list(runtime.plugins.items()):
            if only and uuid != only:
                continue
            if not plugin.enabled:
                continue
            ctx = self.context(telegram_id, uuid)
            for handler in plugin.hooks.get(hook, []):
                try:
                    await self._call(handler, ctx, *args)
                except Exception:
                    logger.exception("Ошибка %s в Playerok-плагине %s", hook, plugin.name)

    async def run_action(self, telegram_id: int, uuid: str, action_id: str) -> Any:
        runtime = self.runtimes.get(telegram_id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        if not plugin or not plugin.enabled:
            raise KeyError(uuid)
        action = plugin.actions.get(action_id)
        if not action:
            raise KeyError(action_id)
        return await self._call(action["handler"], self.context(telegram_id, uuid))

    async def set_setting(
        self, telegram_id: int, uuid: str, key: str, value: Any
    ) -> Any:
        runtime = self.runtimes.get(telegram_id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        if not plugin or key not in plugin.settings_schema:
            raise KeyError(key)
        normalized = _normalize_setting_value(plugin.settings_schema[key], value)
        runtime.settings.setdefault(uuid, {})[key] = normalized
        await self.db.set_playerok_plugin_setting(
            telegram_id, uuid, key, str(normalized)
        )
        await self.dispatch(
            telegram_id,
            "BIND_TO_SETTING_CHANGED",
            key,
            normalized,
            only=uuid,
        )
        return normalized

    async def toggle(self, telegram_id: int, uuid: str) -> bool:
        plugin = self.runtimes[telegram_id].plugins[uuid]
        plugin.enabled = not plugin.enabled
        await self.db.set_playerok_plugin_enabled(telegram_id, uuid, plugin.enabled)
        return plugin.enabled

    async def delete(self, telegram_id: int, uuid: str) -> None:
        runtime = self.runtimes.get(telegram_id)
        plugin = runtime.plugins.get(uuid) if runtime else None
        if plugin and plugin.delete_handler:
            try:
                await self._call(plugin.delete_handler, self.context(telegram_id, uuid))
            except Exception:
                logger.exception("Ошибка BIND_TO_DELETE Playerok-плагина %s", plugin.name)
        if runtime and plugin:
            runtime.plugins.pop(uuid, None)
            runtime.settings.pop(uuid, None)
            runtime.states.pop(uuid, None)
            sys.modules.pop(plugin.module.__name__, None)
        await self.db.delete_playerok_plugin(telegram_id, uuid)
        (self.root / str(telegram_id) / f"{uuid}.py").unlink(missing_ok=True)

    async def stop_runtime(self, telegram_id: int) -> None:
        if telegram_id not in self.runtimes:
            return
        await self.dispatch(telegram_id, "BIND_TO_STOP")
        runtime = self.runtimes.pop(telegram_id)
        for plugin in runtime.plugins.values():
            sys.modules.pop(plugin.module.__name__, None)


def playerok_ready_plugin_source(plugin: PlayerokReadyPluginSpec) -> str:
    return plugin.source


def playerok_setting_label(spec: dict[str, Any], value: Any) -> str:
    if spec["type"] == "bool":
        return "✅" if bool(value) else "❌"
    if spec["type"] == "choice":
        return spec["choices"].get(str(value), str(value))
    return str(value)
