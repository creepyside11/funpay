# FunPay Telegram Bot

Telegram-бот на **aiogram 3** для управления одним FunPay-аккаунтом на каждого пользователя. Конфигурация хранится в PostgreSQL, поэтому проект подходит для Railway, Render, Fly.io, VPS и других Docker-хостингов.

> Проект использует неофициальный FunPay API. Изменения сайта могут нарушить работу парсинга. Используйте автоматизацию с учётом правил FunPay и только для собственных аккаунтов.

## Возможности

- подключение в обязательной последовательности: прокси → `golden_key`;
- шифрование прокси и `golden_key` перед записью в PostgreSQL;
- подробный профиль: ник, ID, онлайн, блокировка, активные сделки и количество лотов;
- проверка баланса;
- уведомления о сообщениях, новых заказах и изменениях статусов — каждый тип включается отдельно;
- автоответчик с редактируемым текстом и паузой 30 минут для каждого чата;
- список 10 последних чатов;
- ручная отправка сообщения по ID чата;
- восстановление активных подключений после перезапуска контейнера;
- Runner из актуальной ветки FunPayCardinal с корректной остановкой и обновлением сессии.

## Переменные окружения

| Переменная | Обязательна | Назначение |
|---|---:|---|
| `BOT_TOKEN` | да | токен Telegram-бота от BotFather |
| `DATABASE_URL` | да | URL PostgreSQL вида `postgresql://user:password@host:5432/db` |
| `APP_SECRET` | рекомендуется | постоянный секрет для шифрования данных; задайте до первого запуска и не меняйте |
| `LOG_LEVEL` | нет | уровень логов, по умолчанию `INFO` |

Если `APP_SECRET` не задан, ключ шифрования выводится из `BOT_TOKEN`. После смены токена старые данные подключения тогда перестанут расшифровываться, поэтому на продакшене задайте отдельный длинный `APP_SECRET`.

## Быстрый запуск

```bash
cp .env.example .env
# заполните .env
docker build -t funpay-telegram-bot .
docker run --env-file .env --restart unless-stopped funpay-telegram-bot
```

Локально без Docker:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN='...'
export DATABASE_URL='postgresql://...'
export APP_SECRET='длинная-случайная-строка'
python bot.py
```

Для PaaS используйте worker-команду `python bot.py`. Веб-порт не требуется: бот работает через long polling.

## Подключение FunPay

1. Откройте `/start`.
2. Отправьте приватный HTTP(S)/SOCKS-прокси, например `http://user:password@host:port`.
3. Отправьте значение cookie `golden_key` авторизованного аккаунта FunPay.
4. Бот проверит оба значения через FunPay, сохранит их только после успешного входа и запустит Runner.

Сообщения с прокси и `golden_key` удаляются ботом по возможности. Для этого бот должен иметь право удалять сообщения в используемом чате. Не добавляйте бота в публичные группы и не передавайте доступ к нему посторонним.

## Структура

- `bot.py` — aiogram, PostgreSQL, интерфейс, настройки и менеджер фоновых аккаунтов;
- `FunPayAPI/` — API и Runner, взятые из FunPayCardinal и адаптированные для управляемой остановки на хостинге;
- `Dockerfile` / `Procfile` — запуск на контейнерных и worker-хостингах.

## Проверка

```bash
pip install -r requirements-dev.txt
python -m compileall bot.py FunPayAPI
pytest -q
```

## Лицензия и источники

Код распространяется по GPL-3.0, см. `LICENSE`. Использованы и адаптированы:

- [LIMBODS/FunPayAPI](https://github.com/LIMBODS/FunPayAPI)
- [sidor0912/FunPayCardinal](https://github.com/sidor0912/FunPayCardinal)
