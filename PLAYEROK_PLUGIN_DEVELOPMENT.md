# Playerok Plugin SDK

Версия контракта: `playerok-sdk-1`

Целевой проект: `creepyside11/funpay`

Формат: один Python-файл UTF-8 с расширением `.py`, размером не более 512 КБ.

Этот документ описывает фактически реализованный интерфейс Playerok-плагинов проекта. Его можно
передать нейросети вместе с заданием: при соблюдении контракта результат можно сразу загрузить в
Telegram-бот. PlayerokAPI является неофициальной библиотекой, поэтому плагин обязан корректно
обрабатывать сетевые ошибки и отсутствие необязательных полей.

## 1. Инструкция для нейросети

При генерации плагина обязательно соблюдайте все пункты:

1. Верните ровно один текстовый `.py`-файл UTF-8 без дополнительных файлов.
2. Размер исходника — не более 512 КБ.
3. Создайте новый UUID версии 4 в каноническом нижнем регистре. Не копируйте UUID примеров или
   официальных плагинов.
4. Объявите все обязательные метаданные, `SETTINGS`, `ACTIONS`, `BIND_TO_DELETE` и семь списков
   событийных хуков.
5. Используйте `def` для блокирующих методов PlayerokAPI. Такие обработчики автоматически
   выполняются в отдельном потоке. `async def` допустим для неблокирующей логики.
6. Не выполняйте запросы к Playerok, не запускайте потоки и циклы во время импорта модуля.
7. Получайте аккаунт только через `ctx.account`. Не создавайте глобальный экземпляр `Account`:
   PlayerokAPI использует singleton-поведение, которое ломает многопользовательский бот.
8. Получайте пользовательские настройки через `ctx.get_setting(key, default)`.
9. Для памяти между вызовами используйте `ctx.state`. Она очищается при перезапуске процесса.
10. Для постоянных пользовательских значений используйте только декларацию `SETTINGS` — бот
    сохранит их в PostgreSQL.
11. Для Telegram-уведомления из синхронного хука используйте `ctx.notify(text)`.
12. Экранируйте пользовательские строки через `html.escape`, если возвращаете или отправляете HTML.
13. Не сохраняйте и не выводите в лог cookies, token, прокси, `BOT_TOKEN`, `DATABASE_URL` и другие
    секреты.
14. Не меняйте статус сделки, объявление или баланс без явного назначения плагина и настройки,
    выключенной по умолчанию.
15. Используйте `getattr(obj, "field", default)` для полей объектов PlayerokAPI, которые могут
    отсутствовать после обновления сайта.
16. Ограничивайте пагинацию: рекомендуется не более 10 страниц по 24 объекта.
17. После генерации выполните финальный чек-лист из раздела 13.

## 2. Установка и каталог

Ручная установка:

`Playerok → Плагины → Загрузить плагин → Я понимаю риск`

Пользователь отправляет файл документом. Загрузчик:

1. проверяет расширение, размер, UTF-8 и маркер `noplug` в первой строке;
2. импортирует модуль в отдельном пространстве имён пользователя;
3. проверяет метаданные, UUID, настройки, действия и хуки;
4. сохраняет метаданные и полный исходник в PostgreSQL;
5. восстанавливает включённые и выключенные плагины после перезапуска;
6. создаёт постоянную кнопку настроек в карточке плагина.

Каталог Playerok отдельный от каталога FunPay. Пользователь может посмотреть описание и автора,
скачать исходник, установить расширение, опубликовать собственный установленный плагин и убрать
свою публикацию. Чужой или официальный UUID перезаписать нельзя. Удаление публикации не удаляет
уже установленные копии.

Исходники сообщества не модерируются. Плагин выполняется с правами процесса бота, поэтому перед
установкой его необходимо скачать и проверить.

## 3. Полный минимальный шаблон

```python
from __future__ import annotations

NAME = "My Playerok Plugin"
VERSION = "1.0.0"
DESCRIPTION = "Краткое описание назначения плагина."
CREDITS = "AuthorName"
UUID = "СОЗДАЙТЕ-НОВЫЙ-КАНОНИЧЕСКИЙ-UUID4"
SETTINGS_PAGE = True

SETTINGS = {
    "enabled": {
        "label": "Автоматизация",
        "type": "bool",
        "default": False,
    },
}


def show_account(ctx):
    username = getattr(ctx.account, "username", None) or "—"
    return f"Аккаунт Playerok: <b>{username}</b>"


ACTIONS = {
    "show_account": {
        "label": "👤 Показать аккаунт",
        "handler": show_account,
    },
}

BIND_TO_START = []
BIND_TO_STOP = []
BIND_TO_TICK = []
BIND_TO_NEW_MESSAGE = []
BIND_TO_DEAL_CHANGED = []
BIND_TO_NEW_REVIEW = []
BIND_TO_SETTING_CHANGED = []
BIND_TO_DELETE = None
```

Замените строку UUID реальным результатом `uuid.uuid4()`. Значение должно выглядеть как
`01234567-89ab-4cde-8f01-23456789abcd` и содержать UUID версии 4.

## 4. Обязательные поля

Все поля объявляются на верхнем уровне модуля.

| Поле | Тип | Назначение |
|---|---|---|
| `NAME` | `str` | Название в интерфейсе и каталоге |
| `VERSION` | `str` | Версия, рекомендуется SemVer |
| `DESCRIPTION` | `str` | Краткое описание |
| `CREDITS` | `str` | Автор или команда |
| `UUID` | `str` | Уникальный UUID4 в нижнем регистре |
| `SETTINGS_PAGE` | `bool` | Показывать постоянную кнопку настроек |
| `SETTINGS` | `dict` | Декларативная схема настроек |
| `ACTIONS` | `dict` | Ручные действия на странице настроек |
| `BIND_TO_DELETE` | callable или `None` | Очистка при удалении |

Кроме них должны существовать все списки из раздела 7. Пустой список допустим.

## 5. Контекст `ctx`

Первым аргументом каждого хука и действия передаётся `PlayerokPluginContext`.

### 5.1 Атрибуты

| Атрибут | Значение |
|---|---|
| `ctx.telegram_id` | Telegram ID владельца |
| `ctx.plugin_uuid` | UUID текущего плагина |
| `ctx.account` | активный независимый `playerokapi.account.Account` пользователя |
| `ctx.runtime` | runtime Playerok этого пользователя |
| `ctx.state` | `dict` временного состояния текущего плагина |

Не изменяйте внутренние словари менеджера и не подменяйте `ctx.runtime.account`.

### 5.2 Настройки

```python
enabled = ctx.get_setting("enabled", False)
interval = int(ctx.get_setting("interval_minutes", 30))
```

Возвращается типизированное значение: `bool`, `int` или `str`. Все значения проходят проверку до
сохранения. Если ключ отсутствует, возвращается переданный `default`.

### 5.3 Временное состояние

```python
import time

last_run = float(ctx.state.get("last_run", 0))
ctx.state["last_run"] = time.monotonic()
```

`ctx.state` отдельный для каждого пользователя и UUID. Он не сохраняется после перезапуска.
Не помещайте туда несериализуемые долгоживущие сетевые клиенты или секреты.

### 5.4 Telegram-уведомления

```python
ctx.notify("✅ <b>Операция выполнена</b>")
```

`notify()` планирует отправку владельцу и сразу возвращает управление синхронному хуку. Допустимы
аргументы `parse_mode`, `disable_web_page_preview` и другие параметры `Bot.send_message`, однако
обычно достаточно текста. Не передавайте чужой `chat_id`: адресат уже задан контекстом.

## 6. Настройки `SETTINGS`

Формат:

```python
SETTINGS = {
    "ключ": {
        "label": "Название кнопки",
        "type": "bool | int | str | choice",
        "default": значение,
    },
}
```

Ключ должен быть Python-идентификатором длиной до 40 символов. `label` — 1–48 символов.

### 6.1 `bool`

```python
"notifications": {
    "label": "Уведомления",
    "type": "bool",
    "default": True,
}
```

Нажатие кнопки мгновенно переключает значение.

### 6.2 `int`

```python
"interval_minutes": {
    "label": "Интервал, минут",
    "type": "int",
    "default": 30,
    "min": 5,
    "max": 1440,
}
```

`min` и `max` обязательны по смыслу и настоятельно рекомендуются. Ввод проверяется до сохранения.

### 6.3 `str`

```python
"status_text": {
    "label": "Текст статуса",
    "type": "str",
    "default": "Магазин работает",
    "min_length": 1,
    "max_length": 500,
}
```

Пустая строка разрешается только при `min_length=0`.

### 6.4 `choice`

```python
"period": {
    "label": "Период",
    "type": "choice",
    "default": "30",
    "choices": {
        "7": "7 дней",
        "30": "30 дней",
        "90": "90 дней",
    },
}
```

Должно быть от 2 до 20 вариантов. В интерфейсе нажатие циклически выбирает следующий вариант.
В `ctx.get_setting()` возвращается ключ варианта, а не его подпись.

## 7. Событийные хуки

Значение каждого `BIND_TO_*` — список функций. Несколько функций вызываются по порядку. Ошибка
одной функции записывается в журнал и не останавливает polling или остальные плагины.

### 7.1 Запуск и остановка

```python
def on_start(ctx):
    ctx.state["started"] = True


def on_stop(ctx):
    ctx.state.clear()


BIND_TO_START = [on_start]
BIND_TO_STOP = [on_stop]
```

`BIND_TO_START` вызывается после восстановления плагинов и сразу после установки. `BIND_TO_STOP`
вызывается при отключении Playerok, переподключении и остановке процесса.

### 7.2 Периодический тик

```python
def on_tick(ctx):
    if not ctx.get_setting("enabled", False):
        return
    # короткая периодическая проверка


BIND_TO_TICK = [on_tick]
```

Тик вызывается после успешного цикла Playerok polling, сейчас примерно раз в 20 секунд. Это не
точный планировщик: сеть, лимиты и ошибки могут увеличить интервал. Собственный интервал храните
через `time.monotonic()` в `ctx.state`. Не запускайте бесконечный цикл.

### 7.3 Новое сообщение

```python
def on_message(ctx, chat, message):
    sender = getattr(message, "user", None)
    if str(getattr(sender, "id", "")) == str(ctx.account.id):
        return
    text = (getattr(message, "text", "") or "").strip()


BIND_TO_NEW_MESSAGE = [on_message]
```

Аргументы: `ctx`, объект чата и последнее новое сообщение. Событие приходит и для собственных
сообщений, поэтому при автоматическом ответе обязательно проверяйте отправителя, иначе возможен
цикл. Для отправки используйте `ctx.account.send_message(chat.id, text)`.

### 7.4 Изменение сделки

```python
def on_deal(ctx, deal, previous_status):
    current = getattr(getattr(deal, "status", None), "name", "UNKNOWN")
    if current == "PAID" and previous_status != "PAID":
        pass


BIND_TO_DEAL_CHANGED = [on_deal]
```

`previous_status` — строка предыдущего статуса или `None` для новой сделки. Не отмечайте заказ
выполненным до успешной выдачи товара. Возможные значения зависят от PlayerokAPI; известные:
`PENDING`, `PAID`, `SENT`, `CONFIRMED`, `CONFIRMED_AUTOMATICALLY`, `ROLLED_BACK`.

### 7.5 Новый отзыв

```python
def on_review(ctx, review):
    rating = int(getattr(review, "rating", 0) or 0)


BIND_TO_NEW_REVIEW = [on_review]
```

Используйте `getattr` для `creator`, `deal`, `rating` и `text`.

### 7.6 Изменение настройки

```python
def on_setting_changed(ctx, key, value):
    if key == "enabled" and value:
        ctx.state["last_run"] = 0


BIND_TO_SETTING_CHANGED = [on_setting_changed]
```

Хук вызывается после успешной записи в PostgreSQL.

## 8. Ручные действия `ACTIONS`

Действия отображаются кнопками на постоянной странице настроек.

```python
def check_now(ctx):
    account = ctx.account.get()
    return f"✅ Аккаунт: <b>{account.username}</b>"


ACTIONS = {
    "check_now": {
        "label": "🔄 Проверить сейчас",
        "handler": check_now,
    },
}
```

ID действия — Python-идентификатор до 32 символов, подпись — 1–48 символов, `handler` — функция.
Результат `None` ничего не отправляет. Остальные результаты преобразуются в строку и отправляются
как Telegram HTML, максимум 3900 символов. Экранируйте внешние строки.

Обработчик может быть `async def`, но синхронный вариант предпочтителен для PlayerokAPI.

## 9. Удаление

```python
def on_delete(ctx):
    ctx.state.clear()


BIND_TO_DELETE = on_delete
```

Или `BIND_TO_DELETE = None`. Функция вызывается перед удалением записи, настроек и файла. Ошибка
очистки записывается в лог, но не должна использоваться для запрета удаления.

## 10. Доступные возможности PlayerokAPI

Через `ctx.account` доступен активный объект `playerokapi.account.Account`. В закреплённой версии
проекта используются, среди прочих, методы:

- `get()` — данные аккаунта;
- `get_chats()` и `get_chat_messages()`;
- `send_message()` и `mark_chat_as_read()`;
- `get_deals()` и `get_deal()`;
- `update_deal()`;
- `get_my_items()` и `get_item()`;
- `create_item()`, `publish_item()`;
- `get_item_priority_statuses()`;
- `get_my_reviews()`;
- `get_games()`, `get_game()`, методы категорий и полей;
- `get_transactions()` и методы профиля/баланса, если они доступны версии библиотеки.

Точные сигнатуры сверяйте с закреплённой зависимостью в `requirements.txt`. Не импортируйте
внутренние приватные поля `Account` и не вызывайте `Account(...)` самостоятельно.

### Пагинация

Типичный шаблон:

```python
items = []
cursor = None
for _ in range(5):
    page = ctx.account.get_deals(count=24, after_cursor=cursor)
    batch = list(getattr(page, "deals", []) or [])
    items.extend(batch)
    info = getattr(page, "page_info", None)
    cursor = getattr(info, "end_cursor", None)
    if not batch or not getattr(info, "has_next_page", False) or not cursor:
        break
```

Всегда задавайте ограничение количества страниц.

## 11. Синхронность, ошибки и производительность

Обычный `def` выполняется через `asyncio.to_thread`, поэтому блокирующие HTTP-запросы PlayerokAPI
не замораживают Telegram-бот. `async def` выполняется в основном event loop; внутри него нельзя
напрямую вызывать блокирующие методы PlayerokAPI — используйте `await asyncio.to_thread(...)`.

Рекомендуемый синхронный обработчик:

```python
def safe_action(ctx):
    try:
        page = ctx.account.get_my_items(count=24)
    except Exception as exc:
        return f"❌ Playerok недоступен: <code>{html.escape(str(exc)[:500])}</code>"
    return f"Найдено: <b>{len(getattr(page, 'items', []) or [])}</b>"
```

Не делайте больше необходимого числа запросов, учитывайте ограничения площадки, не повторяйте
изменяющий запрос автоматически без защиты от дублей.

## 12. Безопасность

Python-плагин технически способен читать окружение процесса, выполнять сетевые запросы и управлять
аккаунтом. Декларативные настройки не являются песочницей. Соблюдайте правила:

- никогда не читайте и не показывайте `ctx.account.cookies`, `ctx.account.token` или прокси;
- не отправляйте данные аккаунта сторонним сервисам;
- не используйте `eval`, `exec`, небезопасную десериализацию и shell-команды;
- не логируйте входящие сообщения целиком без необходимости;
- изменяющие и платёжные функции делайте выключенными по умолчанию;
- ограничивайте частоту, объём выборки и количество повторов;
- проверяйте статус сделки перед выдачей и сохраняйте защиту от дублей, если реализуете выдачу;
- не добавляйте внешние зависимости без явного описания в карточке каталога и Docker-образе.

## 13. Финальный чек-лист

Перед передачей или публикацией плагина проверьте:

- [ ] один файл `.py`, UTF-8, до 512 КБ;
- [ ] новый уникальный канонический UUID4;
- [ ] заполнены NAME, VERSION, DESCRIPTION, CREDITS, UUID;
- [ ] заданы SETTINGS_PAGE, SETTINGS и ACTIONS;
- [ ] заданы все семь списков `BIND_TO_*`;
- [ ] BIND_TO_DELETE — функция или None;
- [ ] SETTINGS использует только bool, int, str, choice;
- [ ] у int есть разумные min/max, у str — max_length;
- [ ] ключи настроек и действий — Python-идентификаторы;
- [ ] нет сетевой активности при импорте;
- [ ] Account не создаётся глобально;
- [ ] блокирующие PlayerokAPI-вызовы находятся в синхронных функциях;
- [ ] входящие данные читаются через getattr;
- [ ] пользовательские строки экранируются для Telegram HTML;
- [ ] нет cookies, токенов, прокси и секретов в коде или логах;
- [ ] автоматизация выключена по умолчанию;
- [ ] есть ограничение пагинации и частоты;
- [ ] исключена повторная выдача/повторное изменение сделки;
- [ ] плагин проверен на тестовом аккаунте;
- [ ] описание каталога перечисляет функции, настройки, зависимости и ограничения.

## 14. Пример плагина с событием и настройками

```python
from __future__ import annotations

import html

NAME = "High Value Deal Notice"
VERSION = "1.0.0"
DESCRIPTION = "Дополнительное уведомление о дорогих сделках."
CREDITS = "Example Author"
UUID = "СОЗДАЙТЕ-СОБСТВЕННЫЙ-UUID4"
SETTINGS_PAGE = True
SETTINGS = {
    "enabled": {"label": "Уведомления", "type": "bool", "default": False},
    "minimum_price": {"label": "Минимальная цена", "type": "int", "default": 1000, "min": 1, "max": 10000000},
}


def on_deal(ctx, deal, previous_status):
    if not ctx.get_setting("enabled", False):
        return
    status = getattr(getattr(deal, "status", None), "name", "")
    item = getattr(deal, "item", None)
    price = float(getattr(item, "price", 0) or 0)
    if status not in {"PENDING", "PAID"} or price < int(ctx.get_setting("minimum_price", 1000)):
        return
    name = html.escape(str(getattr(item, "name", "Без названия"))[:300])
    ctx.notify(f"💎 <b>Крупная сделка</b>\\n{name}\\nСумма: <b>{price:.2f} ₽</b>")


ACTIONS = {}
BIND_TO_START = []
BIND_TO_STOP = []
BIND_TO_TICK = []
BIND_TO_NEW_MESSAGE = []
BIND_TO_DEAL_CHANGED = [on_deal]
BIND_TO_NEW_REVIEW = []
BIND_TO_SETTING_CHANGED = []
BIND_TO_DELETE = None
```

Перед использованием замените UUID. Пример не хранит защиту от повторов самостоятельно, потому
что системный hook вызывается только при обнаруженном изменении статуса в текущем runtime.
