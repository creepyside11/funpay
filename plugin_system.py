from __future__ import annotations

import asyncio
import configparser
import importlib.util
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

from aiogram.types import (
    InlineKeyboardButton as AiogramButton,
)
from aiogram.types import (
    InlineKeyboardMarkup as AiogramMarkup,
)

from FunPayAPI import events, types
from telebot import types as telebot_types

logger = logging.getLogger("funpay_bot.plugins")

PLUGIN_FIELDS = (
    "NAME",
    "VERSION",
    "DESCRIPTION",
    "CREDITS",
    "SETTINGS_PAGE",
    "UUID",
    "BIND_TO_DELETE",
)

HOOK_NAMES = (
    "BIND_TO_PRE_INIT",
    "BIND_TO_POST_INIT",
    "BIND_TO_PRE_START",
    "BIND_TO_POST_START",
    "BIND_TO_PRE_STOP",
    "BIND_TO_POST_STOP",
    "BIND_TO_INIT_MESSAGE",
    "BIND_TO_MESSAGES_LIST_CHANGED",
    "BIND_TO_LAST_CHAT_MESSAGE_CHANGED",
    "BIND_TO_NEW_MESSAGE",
    "BIND_TO_INIT_ORDER",
    "BIND_TO_NEW_ORDER",
    "BIND_TO_ORDERS_LIST_CHANGED",
    "BIND_TO_ORDER_STATUS_CHANGED",
    "BIND_TO_PRE_DELIVERY",
    "BIND_TO_POST_DELIVERY",
    "BIND_TO_PRE_LOTS_RAISE",
    "BIND_TO_POST_LOTS_RAISE",
)

EVENT_HOOKS = {
    events.InitialChatEvent: "BIND_TO_INIT_MESSAGE",
    events.ChatsListChangedEvent: "BIND_TO_MESSAGES_LIST_CHANGED",
    events.LastChatMessageChangedEvent: "BIND_TO_LAST_CHAT_MESSAGE_CHANGED",
    events.NewMessageEvent: "BIND_TO_NEW_MESSAGE",
    events.InitialOrderEvent: "BIND_TO_INIT_ORDER",
    events.NewOrderEvent: "BIND_TO_NEW_ORDER",
    events.OrdersListChangedEvent: "BIND_TO_ORDERS_LIST_CHANGED",
    events.OrderStatusChangedEvent: "BIND_TO_ORDER_STATUS_CHANGED",
}


class PluginValidationError(ValueError):
    pass


@dataclass
class PluginData:
    name: str
    version: str
    description: str
    credits: str
    uuid: str
    filename: str
    module: ModuleType
    settings_page: bool
    delete_handler: Callable[..., Any] | None
    enabled: bool = True
    commands: dict[str, str] = field(default_factory=dict)
    hooks: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)


def convert_markup(value: Any) -> Any:
    if not isinstance(value, telebot_types.InlineKeyboardMarkup):
        return value
    return AiogramMarkup(
        inline_keyboard=[
            [
                AiogramButton(
                    text=button.text,
                    url=button.url,
                    callback_data=button.callback_data,
                )
                for button in row
            ]
            for row in value.keyboard
        ]
    )


class CardinalBotFacade:
    def __init__(
        self,
        bot: Any,
        loop: asyncio.AbstractEventLoop,
        enabled_checker: Callable[[str], bool] | None = None,
    ):
        self._bot = bot
        self.loop = loop
        self.enabled_checker = enabled_checker
        self.current_plugin_uuid: str | None = None
        self.message_handlers: list[dict[str, Any]] = []
        self.callback_handlers: list[dict[str, Any]] = []

    def _call(self, coroutine: Any) -> Any:
        result = asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=30)
        return telebot_types.Message(result) if hasattr(result, "message_id") else result

    def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        if "reply_markup" in kwargs:
            kwargs["reply_markup"] = convert_markup(kwargs["reply_markup"])
        return self._call(self._bot.send_message(chat_id, text, **kwargs))

    def edit_message_text(
        self, text: str, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        if "reply_markup" in kwargs:
            kwargs["reply_markup"] = convert_markup(kwargs["reply_markup"])
        return self._call(
            self._bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, **kwargs
            )
        )

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        if "reply_markup" in kwargs:
            kwargs["reply_markup"] = convert_markup(kwargs["reply_markup"])
        return self._call(
            self._bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, **kwargs
            )
        )

    def answer_callback_query(self, callback_query_id: str, *args: Any, **kwargs: Any) -> Any:
        text = args[0] if args else kwargs.pop("text", None)
        return self._call(
            self._bot.answer_callback_query(callback_query_id, text=text, **kwargs)
        )

    def delete_message(self, chat_id: int, message_id: int) -> Any:
        return self._call(self._bot.delete_message(chat_id, message_id))

    def register_message_handler(
        self, callback: Callable[..., Any], **filters: Any
    ) -> None:
        self.message_handlers.append(
            {
                "callback": callback,
                "plugin_uuid": self.current_plugin_uuid,
                **filters,
            }
        )

    def register_callback_query_handler(
        self, callback: Callable[..., Any], func: Callable[..., bool] | None = None, **filters: Any
    ) -> None:
        self.callback_handlers.append(
            {
                "callback": callback,
                "func": func,
                "plugin_uuid": self.current_plugin_uuid,
                **filters,
            }
        )

    def message_handler(self, **filters: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(callback: Callable[..., Any]) -> Callable[..., Any]:
            self.register_message_handler(callback, **filters)
            return callback

        return decorator

    def callback_query_handler(
        self, func: Callable[..., bool] | None = None, **filters: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(callback: Callable[..., Any]) -> Callable[..., Any]:
            self.register_callback_query_handler(callback, func=func, **filters)
            return callback

        return decorator

    @staticmethod
    def _message_matches(handler: dict[str, Any], message: telebot_types.Message) -> bool:
        commands = handler.get("commands")
        if commands:
            command = (message.text or "").split(maxsplit=1)[0].removeprefix("/").split("@")[0]
            if command not in commands:
                return False
        content_types = handler.get("content_types")
        if content_types:
            content_type = "text" if message.text is not None else "document" if message.document else "photo"
            if content_type not in content_types:
                return False
        func = handler.get("func")
        return not func or bool(func(message))

    def dispatch_message(self, source: Any) -> bool:
        message = telebot_types.Message(source)
        for handler in self.message_handlers:
            uuid = handler.get("plugin_uuid")
            if uuid and self.enabled_checker and not self.enabled_checker(uuid):
                continue
            if self._message_matches(handler, message):
                handler["callback"](message)
                return True
        return False

    def dispatch_callback(self, source: Any) -> bool:
        callback = telebot_types.CallbackQuery(source)
        for handler in self.callback_handlers:
            uuid = handler.get("plugin_uuid")
            if uuid and self.enabled_checker and not self.enabled_checker(uuid):
                continue
            func = handler.get("func")
            if not func or func(callback):
                handler["callback"](callback)
                return True
        return False

    def unregister_plugin(self, uuid: str) -> None:
        self.message_handlers = [
            handler
            for handler in self.message_handlers
            if handler.get("plugin_uuid") != uuid
        ]
        self.callback_handlers = [
            handler
            for handler in self.callback_handlers
            if handler.get("plugin_uuid") != uuid
        ]


class TelegramBridge:
    """Минимальный мост уведомлений для headless-плагинов Cardinal."""

    def __init__(
        self,
        bot: Any,
        telegram_id: int,
        loop: asyncio.AbstractEventLoop,
        enabled_checker: Callable[[str], bool] | None = None,
    ):
        self.bot = CardinalBotFacade(bot, loop, enabled_checker)
        self.telegram_id = telegram_id
        self.loop = loop

    def send_notification(self, text: str, *args: Any, **kwargs: Any) -> Any:
        if args and "reply_markup" not in kwargs:
            kwargs["reply_markup"] = args[0]
        return self.bot.send_message(self.telegram_id, str(text), **kwargs)

    def add_command_to_menu(self, *_args: Any, **_kwargs: Any) -> None:
        # Команды сохраняются в PluginData, но не смешиваются с глобальным меню
        # многопользовательского aiogram-бота.
        return None

    def msg_handler(self, callback: Callable[..., Any], **filters: Any) -> None:
        self.bot.register_message_handler(callback, **filters)

    def cbq_handler(
        self, callback: Callable[..., Any], func: Callable[..., bool] | None = None
    ) -> None:
        self.bot.register_callback_query_handler(callback, func=func)

    def set_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class CardinalAdapter:
    """Совместимый объект `Cardinal`, передаваемый в стандартные хуки FPC."""

    VERSION = "aiogram-compat-1"

    def __init__(
        self,
        runtime: Any,
        telegram: TelegramBridge,
        plugin_manager: PluginManager,
    ):
        self.runtime = runtime
        self.account = runtime.account
        self.runner = runtime.runner
        self.telegram = telegram
        self.plugin_manager = plugin_manager
        self.plugins: dict[str, PluginData] = {}
        self.profile = None
        self.curr_profile = None
        self.tg_profile = None
        self.lots_ids: list[int | str] = []
        self.delivery_tests: dict[str, str] = {}
        self.raise_time: dict[int, float] = {}
        self.raised_time: dict[int, float] = {}
        self.MAIN_CFG = self._build_compat_config()

    @staticmethod
    def _build_compat_config() -> configparser.ConfigParser:
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "FunPay": {
                    "autoRaise": "0",
                    "autoResponse": "0",
                    "autoDelivery": "0",
                    "multiDelivery": "0",
                    "autoRestore": "0",
                    "autoDisable": "0",
                    "oldMsgGetMode": "0",
                    "keepSentMessagesUnread": "0",
                },
                "Other": {"watermark": "", "requestsDelay": "4"},
                "Telegram": {"enabled": "1"},
                "ReviewReply": {},
            }
        )
        return config

    def send_message(
        self,
        chat_id: int | str,
        message_text: str,
        chat_name: str | None = None,
        *_args: Any,
        **_kwargs: Any,
    ) -> list[Any]:
        return [self.account.send_message(chat_id, message_text, chat_name)]

    def get_order_from_object(self, obj: Any, order_id: str | None = None) -> Any:
        if order_id is None:
            order_id = getattr(obj, "id", None) if isinstance(obj, types.OrderShortcut) else None
        if order_id is None:
            match = re.search(r"#([A-Za-z0-9_-]{4,40})", str(obj))
            order_id = match.group(1) if match else None
        return self.account.get_order(order_id) if order_id else None

    def add_telegram_commands(
        self, uuid: str, commands: list[tuple[str, str, bool]]
    ) -> None:
        plugin = self.plugins.get(uuid)
        if plugin:
            plugin.commands.update({command: description for command, description, _ in commands})

    def run_handlers(self, handlers: list[Callable[..., Any]], args: tuple[Any, ...]) -> None:
        for handler in handlers:
            handler(*args)

    def update_lots_and_categories(self) -> bool:
        profile = self.account.get_user(self.account.id)
        self.profile = self.curr_profile = self.tg_profile = profile
        self.lots_ids = [lot.id for lot in profile.get_lots()]
        return True

    def update_session(self, *_args: Any, **_kwargs: Any) -> bool:
        self.account.get(update_phpsessid=True)
        return True

    @property
    def autoraise_enabled(self) -> bool:
        return bool(getattr(self.runtime, "auto_raise_enabled", False))

    @property
    def old_mode_enabled(self) -> bool:
        return False


@dataclass
class PluginRuntime:
    adapter: CardinalAdapter
    plugins: dict[str, PluginData] = field(default_factory=dict)


class PluginManager:
    """Загрузчик одиночных Python-плагинов формата FunPayCardinal."""

    def __init__(self, db: Any, bot: Any):
        self.db = db
        self.bot = bot
        self.runtimes: dict[int, PluginRuntime] = {}
        self.root = Path("plugins_runtime")

    @staticmethod
    def validate_uuid(value: str) -> str:
        try:
            parsed = UUID(str(value), version=4)
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginValidationError("UUID плагина должен быть корректным UUID4") from exc
        if str(parsed) != str(value):
            raise PluginValidationError("UUID плагина должен быть в каноническом нижнем регистре")
        return str(parsed)

    @staticmethod
    def validate_source(filename: str, source: str) -> None:
        if not filename.lower().endswith(".py"):
            raise PluginValidationError("поддерживаются только одиночные файлы .py")
        if len(source.encode("utf-8")) > 512 * 1024:
            raise PluginValidationError("размер плагина не должен превышать 512 КБ")
        if source.splitlines() and "noplug" in source.splitlines()[0].split():
            raise PluginValidationError("файл помечен автором как noplug")

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
        adapter: CardinalAdapter | None = None,
    ) -> PluginData:
        self.validate_source(filename, source)
        preliminary_uuid = expected_uuid or "pending"
        path = self._write_source(telegram_id, preliminary_uuid, source)
        module_name = f"fpc_plugin_{telegram_id}_{preliminary_uuid.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if not spec or not spec.loader:
            raise PluginValidationError("не удалось создать модуль плагина")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        plugin_dir = str(path.parent.resolve())
        if plugin_dir not in sys.path:
            sys.path.append(plugin_dir)
        facade = adapter.telegram.bot if adapter else None
        message_handlers_start = len(facade.message_handlers) if facade else 0
        callback_handlers_start = len(facade.callback_handlers) if facade else 0
        registration_tag = f"pending:{module_name}"

        def rollback_import() -> None:
            if facade:
                del facade.message_handlers[message_handlers_start:]
                del facade.callback_handlers[callback_handlers_start:]
            sys.modules.pop(module_name, None)

        try:
            if adapter:
                from cardinal import set_cardinal

                set_cardinal(adapter)
                facade.current_plugin_uuid = registration_tag
            spec.loader.exec_module(module)
        except Exception as exc:
            rollback_import()
            raise PluginValidationError(
                f"ошибка импорта {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if facade:
                facade.current_plugin_uuid = None

        try:
            missing = [field for field in PLUGIN_FIELDS if not hasattr(module, field)]
            if missing:
                raise PluginValidationError(
                    "отсутствуют обязательные поля: " + ", ".join(missing)
                )
            uuid = self.validate_uuid(str(module.UUID))
            if expected_uuid and uuid != expected_uuid:
                raise PluginValidationError("UUID в БД и файле плагина не совпадает")
            if facade:
                for handler in facade.message_handlers[message_handlers_start:]:
                    if handler.get("plugin_uuid") == registration_tag:
                        handler["plugin_uuid"] = uuid
                for handler in facade.callback_handlers[callback_handlers_start:]:
                    if handler.get("plugin_uuid") == registration_tag:
                        handler["plugin_uuid"] = uuid
            if preliminary_uuid != uuid:
                old_path = path
                path = self._write_source(telegram_id, uuid, source)
                module.__file__ = str(path)
                if module.__spec__:
                    module.__spec__.origin = str(path)
                old_path.unlink(missing_ok=True)

            hooks: dict[str, list[Callable[..., Any]]] = {}
            for name in HOOK_NAMES:
                value = getattr(module, name, [])
                if not isinstance(value, (list, tuple)) or not all(
                    callable(item) for item in value
                ):
                    raise PluginValidationError(f"{name} должен быть списком функций")
                hooks[name] = list(value)
            delete_handler = module.BIND_TO_DELETE
            if delete_handler is not None and not callable(delete_handler):
                raise PluginValidationError(
                    "BIND_TO_DELETE должен быть функцией или None"
                )
            return PluginData(
                str(module.NAME),
                str(module.VERSION),
                str(module.DESCRIPTION),
                str(module.CREDITS),
                uuid,
                filename,
                module,
                bool(module.SETTINGS_PAGE),
                delete_handler,
                enabled,
                hooks=hooks,
            )
        except PluginValidationError:
            rollback_import()
            raise

    async def load_runtime(self, telegram_id: int, runtime: Any) -> PluginRuntime:
        loop = asyncio.get_running_loop()
        adapter = CardinalAdapter(
            runtime,
            TelegramBridge(
                self.bot,
                telegram_id,
                loop,
                lambda uuid: bool(
                    self.runtimes.get(telegram_id)
                    and self.runtimes[telegram_id].plugins.get(uuid)
                    and self.runtimes[telegram_id].plugins[uuid].enabled
                ),
            ),
            self,
        )
        plugin_runtime = PluginRuntime(adapter)
        self.runtimes[telegram_id] = plugin_runtime
        rows = await self.db.list_plugins(telegram_id)
        if rows:
            try:
                await asyncio.to_thread(adapter.update_lots_and_categories)
            except Exception:
                logger.exception(
                    "Не удалось подготовить профиль для плагинов пользователя %s",
                    telegram_id,
                )
        for row in rows:
            try:
                plugin = await asyncio.to_thread(
                    self._load_module,
                    telegram_id,
                    row["filename"],
                    row["source"],
                    bool(row["enabled"]),
                    row["uuid"],
                    adapter,
                )
            except Exception:
                logger.exception("Не удалось восстановить плагин %s", row["uuid"])
                continue
            plugin_runtime.plugins[plugin.uuid] = plugin
        adapter.plugins = plugin_runtime.plugins
        await self.dispatch(telegram_id, "BIND_TO_PRE_INIT", adapter)
        await self.dispatch(telegram_id, "BIND_TO_POST_INIT", adapter)
        await self.dispatch(telegram_id, "BIND_TO_PRE_START", adapter)
        await self.dispatch(telegram_id, "BIND_TO_POST_START", adapter)
        return plugin_runtime

    async def install(
        self, telegram_id: int, filename: str, source: str, runtime: Any
    ) -> PluginData:
        plugin_runtime = self.runtimes.get(telegram_id)
        if not plugin_runtime:
            plugin_runtime = await self.load_runtime(telegram_id, runtime)
        plugin = await asyncio.to_thread(
            self._load_module,
            telegram_id,
            filename,
            source,
            True,
            None,
            plugin_runtime.adapter,
        )
        await self.db.upsert_plugin(telegram_id, plugin, source)
        old = plugin_runtime.plugins.get(plugin.uuid)
        if old:
            plugin_runtime.adapter.telegram.bot.unregister_plugin(plugin.uuid)
            sys.modules.pop(old.module.__name__, None)
        plugin_runtime.plugins[plugin.uuid] = plugin
        plugin_runtime.adapter.plugins = plugin_runtime.plugins
        if plugin_runtime.adapter.profile is None:
            try:
                await asyncio.to_thread(
                    plugin_runtime.adapter.update_lots_and_categories
                )
            except Exception:
                logger.exception("Не удалось подготовить профиль для нового плагина")
        await self.dispatch(telegram_id, "BIND_TO_PRE_INIT", plugin_runtime.adapter, only=plugin.uuid)
        await self.dispatch(telegram_id, "BIND_TO_POST_INIT", plugin_runtime.adapter, only=plugin.uuid)
        await self.dispatch(telegram_id, "BIND_TO_PRE_START", plugin_runtime.adapter, only=plugin.uuid)
        await self.dispatch(telegram_id, "BIND_TO_POST_START", plugin_runtime.adapter, only=plugin.uuid)
        return plugin

    async def dispatch(
        self,
        telegram_id: int,
        hook: str,
        *args: Any,
        only: str | None = None,
    ) -> None:
        plugin_runtime = self.runtimes.get(telegram_id)
        if not plugin_runtime:
            return
        for uuid, plugin in list(plugin_runtime.plugins.items()):
            if only and uuid != only:
                continue
            if not plugin.enabled:
                continue
            for handler in plugin.hooks.get(hook, []):
                try:
                    def run_handler(
                        current_handler: Callable[..., Any] = handler,
                        current_uuid: str = uuid,
                    ) -> None:
                        from cardinal import set_cardinal

                        set_cardinal(plugin_runtime.adapter)
                        plugin_runtime.adapter.telegram.bot.current_plugin_uuid = current_uuid
                        current_handler(*args)

                    try:
                        await asyncio.to_thread(run_handler)
                    finally:
                        plugin_runtime.adapter.telegram.bot.current_plugin_uuid = None
                except Exception:
                    logger.exception("Ошибка %s в плагине %s", hook, plugin.name)

    async def dispatch_event(self, telegram_id: int, event: Any) -> None:
        hook = next(
            (name for event_type, name in EVENT_HOOKS.items() if isinstance(event, event_type)),
            None,
        )
        plugin_runtime = self.runtimes.get(telegram_id)
        if hook and plugin_runtime:
            await self.dispatch(
                telegram_id, hook, plugin_runtime.adapter, event
            )

    async def dispatch_telegram_message(self, telegram_id: int, message: Any) -> bool:
        plugin_runtime = self.runtimes.get(telegram_id)
        if not plugin_runtime:
            return False
        return await asyncio.to_thread(
            plugin_runtime.adapter.telegram.bot.dispatch_message, message
        )

    async def dispatch_telegram_callback(self, telegram_id: int, callback: Any) -> bool:
        plugin_runtime = self.runtimes.get(telegram_id)
        if not plugin_runtime:
            return False
        return await asyncio.to_thread(
            plugin_runtime.adapter.telegram.bot.dispatch_callback, callback
        )

    async def toggle(self, telegram_id: int, uuid: str) -> bool:
        plugin = self.runtimes[telegram_id].plugins[uuid]
        plugin.enabled = not plugin.enabled
        await self.db.set_plugin_enabled(telegram_id, uuid, plugin.enabled)
        return plugin.enabled

    async def delete(self, telegram_id: int, uuid: str, callback: Any = None) -> None:
        plugin_runtime = self.runtimes.get(telegram_id)
        plugin = plugin_runtime.plugins.get(uuid) if plugin_runtime else None
        if plugin and plugin.delete_handler:
            try:
                await asyncio.to_thread(
                    plugin.delete_handler, plugin_runtime.adapter, callback
                )
            except Exception:
                logger.exception("Ошибка BIND_TO_DELETE плагина %s", plugin.name)
        if plugin_runtime and plugin:
            plugin_runtime.adapter.telegram.bot.unregister_plugin(uuid)
            plugin_runtime.plugins.pop(uuid, None)
            sys.modules.pop(plugin.module.__name__, None)
        await self.db.delete_plugin(telegram_id, uuid)
        path = self.root / str(telegram_id) / f"{uuid}.py"
        path.unlink(missing_ok=True)

    async def stop_runtime(self, telegram_id: int) -> None:
        plugin_runtime = self.runtimes.get(telegram_id)
        if not plugin_runtime:
            return
        await self.dispatch(
            telegram_id, "BIND_TO_PRE_STOP", plugin_runtime.adapter
        )
        await self.dispatch(
            telegram_id, "BIND_TO_POST_STOP", plugin_runtime.adapter
        )
        self.runtimes.pop(telegram_id, None)
