from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import bot


PLAYEROK_DISABLED_MESSAGE = "Playerok временно отключён"
RESTART_DELAY_SECONDS = 5
logger = logging.getLogger("funpay_bot.supervisor")
_ORIGINAL_FUNPAY_SEND_MESSAGE = bot.Account.send_message


async def _database_fetchval(self: Any, query: str, *args: Any) -> Any:
    """Compatibility helper matching asyncpg's fetchval API for plugins."""
    row = await self.fetchrow(query, *args)
    return None if row is None else row[0]


async def _start_saved_without_playerok(self: Any) -> None:
    """Restore only FunPay accounts while Playerok is stubbed out."""
    self.loop = asyncio.get_running_loop()
    try:
        rows = list(await self.db.active_users())
    except asyncio.CancelledError:
        raise
    except Exception:
        bot.logger.exception(
            "Не удалось получить сохранённые FunPay-аккаунты; бот продолжит запуск"
        )
        rows = []

    for row in rows:
        try:
            self.start_account_in_background(
                int(row["telegram_id"]), "funpay", row, notify=True
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            bot.logger.exception(
                "Не удалось запланировать восстановление FunPay-аккаунта; "
                "остальные аккаунты продолжат запуск"
            )

    bot.logger.warning(
        "%s: сохранённые аккаунты Playerok не запускаются и сеть Playerok не вызывается",
        PLAYEROK_DISABLED_MESSAGE,
    )


async def _disabled_start_playerok(self: Any, *args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(PLAYEROK_DISABLED_MESSAGE)


def _disabled_create_playerok_account(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(PLAYEROK_DISABLED_MESSAGE)


def _send_message_with_private_node(
    self: Any, chat_id: Any, *args: Any, **kwargs: Any
) -> Any:
    """Resolve numeric private chat IDs to FunPay's canonical users-A-B node.

    Order delivery already uses the canonical users-A-B node. Incoming message
    events, including Emerald Promo #free, expose a numeric bookmark chat ID.
    Runner keeps the corresponding interlocutor ID in users_ids, so translate
    that numeric ID before sending to make both delivery paths identical.
    """
    runner = getattr(self, "runner", None)
    account_id = getattr(self, "id", None)
    if isinstance(chat_id, int) and runner is not None and account_id is not None:
        interlocutor_id = getattr(runner, "users_ids", {}).get(chat_id)
        if interlocutor_id is not None:
            first, second = sorted((int(account_id), int(interlocutor_id)))
            chat_id = f"users-{first}-{second}"
    return _ORIGINAL_FUNPAY_SEND_MESSAGE(self, chat_id, *args, **kwargs)


def main() -> None:
    # Provide the asyncpg-style scalar query API expected by some plugins.
    bot.Database.fetchval = _database_fetchval

    # Temporary hard stub: do not restore, connect to, or validate Playerok.
    bot.RuntimeManager.start_saved = _start_saved_without_playerok
    bot.RuntimeManager.start_playerok = _disabled_start_playerok
    bot.create_playerok_account = _disabled_create_playerok_account

    # FunPay private replies from incoming message events must use the same
    # canonical node format as order delivery.
    bot.Account.send_message = _send_message_with_private_node

    while True:
        try:
            asyncio.run(bot.main())
            return
        except KeyboardInterrupt:
            return
        except Exception:
            logger.exception(
                "Основной цикл бота аварийно завершился; перезапуск через %s сек.",
                RESTART_DELAY_SECONDS,
            )
            time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    main()