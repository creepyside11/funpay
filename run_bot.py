from __future__ import annotations

import asyncio
import logging
import time

import bot


logger = logging.getLogger("funpay_bot.supervisor")
RESTART_DELAY_SECONDS = 5


def main() -> None:
    """Run the bot and recover from unexpected top-level failures.

    Account-specific connection failures (for example a Playerok proxy/cookie
    timing out) should normally be handled inside bot.py. This supervisor is a
    final safety net: if one of those failures ever reaches the process entry
    point, the container stays alive and starts a fresh bot loop instead of
    terminating permanently.
    """
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
