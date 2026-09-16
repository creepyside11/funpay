from __future__ import annotations

import asyncio
from typing import Any

import bot


PLAYEROK_DISABLED_MESSAGE = "Playerok временно отключён"


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


def main() -> None:
    # Temporary hard stub: do not restore, connect to, or validate Playerok.
    bot.RuntimeManager.start_saved = _start_saved_without_playerok
    bot.RuntimeManager.start_playerok = _disabled_start_playerok
    bot.create_playerok_account = _disabled_create_playerok_account
    asyncio.run(bot.main())


if __name__ == "__main__":
    main()
