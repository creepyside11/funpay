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

    def get_clients(self, plugin_uuid: str) -> list[TelegramClient]:
        return self._service.get_clients(self.telegram_id, plugin_uuid)

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
        self.clients: dict[tuple[int, str, int], TelegramClient] = {}
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
        clients = self.get_clients(telegram_id, plugin_uuid)
        return clients[0] if clients else None

    def get_clients(self, telegram_id: int, plugin_uuid: str) -> list[TelegramClient]:
        return [
            client
            for (owner_id, uuid, _session_id), client in self.clients.items()
            if owner_id == telegram_id and uuid == plugin_uuid
        ]

    def get_client_by_session(
        self, telegram_id: int, plugin_uuid: str, session_id: int
    ) -> TelegramClient | None:
        return self.clients.get((telegram_id, plugin_uuid, session_id))

    def session_id_for_client(
        self, telegram_id: int, plugin_uuid: str, client: TelegramClient
    ) -> int | None:
        for (owner_id, uuid, session_id), current in self.clients.items():
            if owner_id == telegram_id and uuid == plugin_uuid and current is client:
                return session_id
        return None

    async def start_plugin(
        self, telegram_id: int, plugin_uuid: str
    ) -> TelegramClient | None:
        self.loop = asyncio.get_running_loop()
        existing = self.get_clients(telegram_id, plugin_uuid)
        if existing and all(client.is_connected() for client in existing):
            return existing[0]
        await self.stop_plugin(telegram_id, plugin_uuid)
        rows = await self.db.get_plugin_telethon_sessions(telegram_id, plugin_uuid)
        if not rows or not self.configured:
            return None
        for row in rows:
            client = None
            try:
                client = self.create_client(self.secrets.decrypt(row["session_enc"]))
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    continue
                self.clients[(telegram_id, plugin_uuid, int(row["id"]))] = client
            except Exception:
                if client:
                    await client.disconnect()
        return self.get_client(telegram_id, plugin_uuid)

    async def activate(
        self,
        telegram_id: int,
        plugin_uuid: str,
        phone: str,
        client: TelegramClient,
        *,
        password: str | None = None,
    ) -> Any:
        self.loop = asyncio.get_running_loop()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована")
        me = await client.get_me()
        session = client.session.save()
        row = await self.db.save_plugin_telethon_session(
            telegram_id,
            plugin_uuid,
            self.secrets.encrypt(phone),
            self.secrets.encrypt(session),
            self.secrets.encrypt(password) if password else None,
            int(me.id),
            getattr(me, "username", None),
        )
        session_id = int(row["id"])
        old = self.clients.pop((telegram_id, plugin_uuid, session_id), None)
        if old and old is not client:
            await old.disconnect()
        self.clients[(telegram_id, plugin_uuid, session_id)] = client
        return me

    async def get_2fa_password(
        self, telegram_id: int, plugin_uuid: str, session_id: int | None = None
    ) -> str | None:
        row = await self.db.get_plugin_telethon_session(
            telegram_id, plugin_uuid, session_id
        )
        encrypted = row["password_enc"] if row else None
        return self.secrets.decrypt(encrypted) if encrypted else None

    async def stop_plugin(
        self, telegram_id: int, plugin_uuid: str, *, delete_session: bool = False,
        session_id: int | None = None,
    ) -> None:
        keys = [
            key for key in self.clients
            if key[0] == telegram_id and key[1] == plugin_uuid
            and (session_id is None or key[2] == session_id)
        ]
        for key in keys:
            client = self.clients.pop(key)
            await client.disconnect()
        if delete_session:
            await self.db.delete_plugin_telethon_session(
                telegram_id, plugin_uuid, session_id
            )

    async def stop_user(self, telegram_id: int) -> None:
        plugin_uuids = {key[1] for key in self.clients if key[0] == telegram_id}
        for plugin_uuid in plugin_uuids:
            await self.stop_plugin(telegram_id, plugin_uuid)

    async def close(self) -> None:
        for telegram_id, plugin_uuid in {
            (key[0], key[1]) for key in self.clients
        }:
            await self.stop_plugin(telegram_id, plugin_uuid)
