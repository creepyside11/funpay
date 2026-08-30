from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable

from telethon import TelegramClient
from telethon.sessions import StringSession


class TelethonConfigurationError(RuntimeError):
    pass


class PluginTelethonBridge:
    """Синхронный мост из потоков Cardinal-плагинов к официальному Telethon client."""

    def __init__(self, service: PluginTelethonService, telegram_id: int):
        self._service = service
        self.telegram_id = telegram_id

    def get_client(self, plugin_uuid: str) -> TelegramClient | None:
        return self._service.get_client(self.telegram_id, plugin_uuid)

    def is_connected(self, plugin_uuid: str) -> bool:
        client = self.get_client(plugin_uuid)
        return bool(client and client.is_connected())

    def run(self, awaitable: Awaitable[Any], timeout: float = 60) -> Any:
        loop = self._service.loop
        if loop is None or not loop.is_running():
            raise RuntimeError("Telethon event loop ещё не запущен")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            raise RuntimeError(
                "cardinal.telethon.run() нельзя вызывать из event loop; используйте await"
            )
        return asyncio.run_coroutine_threadsafe(awaitable, loop).result(timeout=timeout)


class PluginTelethonService:
    """Управляет отдельной зашифрованной Telethon-сессией каждого FunPay-плагина."""

    def __init__(self, db: Any, secrets: Any):
        self.db = db
        self.secrets = secrets
        self.clients: dict[tuple[int, str], TelegramClient] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def api_id(self) -> int | None:
        raw = os.getenv("TELETHON_API_ID", "").strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    @property
    def api_hash(self) -> str:
        return os.getenv("TELETHON_API_HASH", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def require_configured(self) -> tuple[int, str]:
        api_id = self.api_id
        api_hash = self.api_hash
        if not api_id or not api_hash:
            raise TelethonConfigurationError(
                "Администратор должен задать TELETHON_API_ID и TELETHON_API_HASH"
            )
        return api_id, api_hash

    def create_client(self, session: str = "") -> TelegramClient:
        api_id, api_hash = self.require_configured()
        return TelegramClient(StringSession(session), api_id, api_hash)

    def bridge(self, telegram_id: int) -> PluginTelethonBridge:
        return PluginTelethonBridge(self, telegram_id)

    def get_client(self, telegram_id: int, plugin_uuid: str) -> TelegramClient | None:
        return self.clients.get((telegram_id, plugin_uuid))

    async def start_plugin(
        self, telegram_id: int, plugin_uuid: str
    ) -> TelegramClient | None:
        self.loop = asyncio.get_running_loop()
        current = self.get_client(telegram_id, plugin_uuid)
        if current and current.is_connected():
            return current
        if current:
            self.clients.pop((telegram_id, plugin_uuid), None)
            await current.disconnect()
        row = await self.db.get_plugin_telethon_session(telegram_id, plugin_uuid)
        if not row or not self.configured:
            return None
        client = self.create_client(self.secrets.decrypt(row["session_enc"]))
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        self.clients[(telegram_id, plugin_uuid)] = client
        return client

    async def activate(
        self,
        telegram_id: int,
        plugin_uuid: str,
        phone: str,
        client: TelegramClient,
    ) -> Any:
        self.loop = asyncio.get_running_loop()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована")
        me = await client.get_me()
        session = client.session.save()
        await self.db.save_plugin_telethon_session(
            telegram_id,
            plugin_uuid,
            self.secrets.encrypt(phone),
            self.secrets.encrypt(session),
            int(me.id),
            getattr(me, "username", None),
        )
        old = self.clients.pop((telegram_id, plugin_uuid), None)
        if old and old is not client:
            await old.disconnect()
        self.clients[(telegram_id, plugin_uuid)] = client
        return me

    async def stop_plugin(
        self, telegram_id: int, plugin_uuid: str, *, delete_session: bool = False
    ) -> None:
        client = self.clients.pop((telegram_id, plugin_uuid), None)
        if client:
            await client.disconnect()
        if delete_session:
            await self.db.delete_plugin_telethon_session(telegram_id, plugin_uuid)

    async def stop_user(self, telegram_id: int) -> None:
        keys = [key for key in self.clients if key[0] == telegram_id]
        for _, plugin_uuid in keys:
            await self.stop_plugin(telegram_id, plugin_uuid)

    async def close(self) -> None:
        for telegram_id, plugin_uuid in list(self.clients):
            await self.stop_plugin(telegram_id, plugin_uuid)
