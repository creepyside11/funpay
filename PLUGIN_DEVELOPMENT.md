# FunPay Telegram Bot Plugin SDK

Версия контракта: `aiogram-compat-1`

Целевой проект: `creepyside11/funpay`

Формат: одиночный Python-модуль, совместимый с основной моделью плагинов FunPayCardinal.

Этот файл предназначен одновременно для разработчика и для нейросети, которая генерирует плагин.
Он описывает **реально реализованный** интерфейс текущего репозитория. Не используйте методы и
внутренние модули оригинального FunPayCardinal, если они явно не перечислены здесь.

## 1. Короткая инструкция для нейросети

При генерации плагина соблюдайте все правила:

1. Создавайте ровно один UTF-8 файл с расширением `.py`, размером до 512 КБ.
2. Используйте только Python standard library, `FunPayAPI`, `cardinal` и перечисленные ниже классы
   `telebot.types`, если пользователь отдельно не подтвердил добавление внешней зависимости.
3. Создайте новый канонический UUID версии 4 в нижнем регистре. Не копируйте UUID из примеров.
4. Объявите все обязательные метаданные на уровне модуля.
5. Обработчики должны быть обычными синхронными функциями `def`, не `async def`.
6. Не запускайте сеть, бесконечные циклы и фоновые потоки во время импорта файла.
7. Для событий используйте только атрибуты, перечисленные в этом документе, либо проверяйте
   неизвестный атрибут через `getattr`.
8. Экранируйте пользовательские строки перед вставкой в Telegram HTML через `html.escape`.
9. Не отвечайте на собственные FunPay-сообщения: проверяйте `author_id`, `by_bot` и `by_vertex`.
10. Не регистрируйте одинаковый Telegram-обработчик несколько раз.
11. Держите `callback_data` короче 64 байт.
12. Не храните токены, golden_key, прокси и пароли в исходнике или логах.
13. `BIND_TO_PRE_DELIVERY` и `BIND_TO_POST_DELIVERY` валидируются загрузчиком, но текущий бот не
    содержит отдельного конвейера автовыдачи и поэтому эти два хука не вызываются.
14. `SETTINGS_PAGE=True` не создаёт страницу автоматически. Плагин должен сам зарегистрировать
    Telegram callback/message handlers либо использовать встроенную страницу, добавленную в `bot.py`.
15. После генерации проверьте раздел «Финальный чек-лист».

## 2. Установка и жизненный цикл

Пользователь открывает:

`Плагины → Загрузить плагин → Я понимаю риск`

и отправляет файл как Telegram-документ. Загрузчик:

1. проверяет `.py`, UTF-8, размер и маркер `noplug` в первой строке;
2. импортирует модуль;
3. проверяет метаданные, UUID4 и списки хуков;
4. сохраняет исходник и метаданные в PostgreSQL;
5. вызывает хуки инициализации и запуска;
6. восстанавливает модуль из PostgreSQL после перезапуска процесса.

Плагин можно выключить без удаления. У выключенного плагина не выполняются хуки событий и
зарегистрированные Telegram-обработчики. При удалении вызывается `BIND_TO_DELETE`, удаляются
обработчики, исходник runtime и связанные настройки.

Плагин выполняется с правами процесса бота и имеет доступ к его окружению и FunPay-аккаунту.

## 3. Обязательные поля модуля

| Поле | Точный тип | Требование |
|---|---|---|
| `NAME` | строка | Отображаемое название. |
| `VERSION` | строка | Версия, рекомендуется SemVer: `1.0.0`. |
| `DESCRIPTION` | строка | Краткое назначение. |
| `CREDITS` | строка | Автор или источник. |
| `SETTINGS_PAGE` | bool | Информационный флаг наличия настроек. |
| `UUID` | строка | Канонический UUID4 в нижнем регистре. |
| `BIND_TO_DELETE` | callable или `None` | Обработчик удаления `(cardinal, callback)`. |

Загрузчик преобразует текстовые метаданные через `str`, но нейросеть всё равно должна создавать
их строками. UUID должен пройти обе проверки:

```python
from uuid import UUID

assert UUID(UUID_TEXT, version=4)
assert str(UUID(UUID_TEXT, version=4)) == UUID_TEXT
```

Первая строка не должна содержать отдельное слово `noplug`.

## 4. Полный список хуков

Каждый `BIND_TO_*` — список или tuple вызываемых объектов. Необъявленное имя считается пустым
списком. Любое другое значение отклоняет плагин.

### 4.1 Жизненный цикл

| Имя | Сигнатура функции | Когда вызывается |
|---|---|---|
| `BIND_TO_PRE_INIT` | `(cardinal)` | после импорта, перед завершением инициализации |
| `BIND_TO_POST_INIT` | `(cardinal)` | после PRE_INIT |
| `BIND_TO_PRE_START` | `(cardinal)` | перед началом обычной работы runtime |
| `BIND_TO_POST_START` | `(cardinal)` | после PRE_START |
| `BIND_TO_PRE_STOP` | `(cardinal)` | перед остановкой runtime |
| `BIND_TO_POST_STOP` | `(cardinal)` | после PRE_STOP |

При первой загрузке нового плагина четыре стартовых хука вызываются только для него. При
восстановлении runtime они вызываются для всех включённых плагинов.

### 4.2 События FunPay

Все функции получают `(cardinal, event)`.

| Имя | Класс события | Основные данные |
|---|---|---|
| `BIND_TO_INIT_MESSAGE` | `events.InitialChatEvent` | `event.chat` |
| `BIND_TO_MESSAGES_LIST_CHANGED` | `events.ChatsListChangedEvent` | базовые поля события |
| `BIND_TO_LAST_CHAT_MESSAGE_CHANGED` | `events.LastChatMessageChangedEvent` | `event.chat` |
| `BIND_TO_NEW_MESSAGE` | `events.NewMessageEvent` | `event.message`, `event.stack` |
| `BIND_TO_INIT_ORDER` | `events.InitialOrderEvent` | `event.order` |
| `BIND_TO_NEW_ORDER` | `events.NewOrderEvent` | `event.order` |
| `BIND_TO_ORDERS_LIST_CHANGED` | `events.OrdersListChangedEvent` | `event.purchases`, `event.sales` |
| `BIND_TO_ORDER_STATUS_CHANGED` | `events.OrderStatusChangedEvent` | `event.order` |

Каждое событие также имеет `runner_tag`, `type` и `time`.

### 4.3 Операционные хуки

| Имя | Состояние в этом проекте |
|---|---|
| `BIND_TO_PRE_LOTS_RAISE` | вызывается перед поднятием категории; аргументы `(cardinal, category)` |
| `BIND_TO_POST_LOTS_RAISE` | вызывается после поднятия; аргументы `(cardinal, category, result_text)` |
| `BIND_TO_PRE_DELIVERY` | имя поддержано валидатором, но событие пока не генерируется |
| `BIND_TO_POST_DELIVERY` | имя поддержано валидатором, но событие пока не генерируется |

### 4.4 Удаление

`BIND_TO_DELETE` — одна функция или `None`, а не список:

```python
def on_delete(cardinal, callback):
    # callback может быть Telegram CallbackQuery или None.
    # Очистите только данные своего плагина.
    return None


BIND_TO_DELETE = on_delete
```

Ошибка обработчика записывается в лог, но удаление записи плагина всё равно продолжается.

## 5. Объект cardinal

Обработчик получает экземпляр совместимого `CardinalAdapter`.

### 5.1 Гарантированные атрибуты

| Атрибут | Значение |
|---|---|
| `cardinal.account` | активный `FunPayAPI.Account` пользователя |
| `cardinal.runner` | активный `FunPayAPI.Runner` |
| `cardinal.telegram` | Telegram-мост текущего пользователя |
| `cardinal.plugins` | dict `{uuid: PluginData}` текущего runtime |
| `cardinal.profile` | профиль после `update_lots_and_categories()`, иначе может быть `None` |
| `cardinal.curr_profile` | то же совместимое поле |
| `cardinal.tg_profile` | то же совместимое поле |
| `cardinal.lots_ids` | список ID после обновления профиля |
| `cardinal.MAIN_CFG` | совместимый `ConfigParser` с базовыми секциями |
| `cardinal.autoraise_enabled` | bool |
| `cardinal.old_mode_enabled` | всегда `False` |

### 5.2 Гарантированные методы

```python
cardinal.send_message(chat_id, message_text, chat_name=None)
```

Синхронно отправляет сообщение через FunPay и возвращает список с объектом отправленного сообщения.

```python
cardinal.get_order_from_object(obj, order_id=None)
```

Получает полный заказ. Если `order_id` не передан, пытается взять ID из `OrderShortcut` или найти
`#ORDER_ID` в строковом представлении объекта.

```python
cardinal.add_telegram_commands(uuid, commands)
```

Сохраняет описания команд внутри `PluginData`. Общий список BotFather не меняется, потому что один
aiogram-бот обслуживает несколько FunPay-аккаунтов.

```python
cardinal.run_handlers(handlers, args)
cardinal.update_lots_and_categories()
cardinal.update_session()
```

`update_lots_and_categories()` обновляет поля профиля и `lots_ids`. `update_session()` обновляет
FunPay-сессию. Оба метода синхронные.

Также доступны импорты:

```python
from cardinal import Cardinal, get_cardinal
```

`get_cardinal()` возвращает адаптер текущего потока или `None`. Внутри обычного хука используйте
переданный аргумент `cardinal`; это понятнее и надёжнее.

## 6. Telegram API совместимости

Плагин работает не с настоящим `TeleBot`, а с синхронным фасадом над aiogram. Вызовы из хуков
переносятся в основной event loop и ждут результата до 30 секунд.

### 6.1 Отправка и изменение сообщений

```python
bot = cardinal.telegram.bot

bot.send_message(chat_id, text, reply_markup=None, **aiogram_compatible_kwargs)
bot.edit_message_text(text, chat_id, message_id, reply_markup=None, **kwargs)
bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None, **kwargs)
bot.answer_callback_query(callback_query_id, text=None, **kwargs)
bot.delete_message(chat_id, message_id)
```

`cardinal.telegram.send_notification(text, reply_markup=None, **kwargs)` отправляет сообщение
владельцу текущего FunPay-аккаунта.

### 6.2 Регистрация обработчиков

```python
bot.register_message_handler(callback, commands=["command"], content_types=["text"], func=None)
bot.register_callback_query_handler(callback, func=lambda call: call.data == "my_action")

@bot.message_handler(commands=["command"])
def command_handler(message):
    ...

@bot.callback_query_handler(func=lambda call: call.data.startswith("my_plugin:"))
def callback_handler(call):
    ...
```

Поддержаны только фильтры:

- `commands`: список имён без `/`;
- `content_types`: `text`, `document`, `photo`;
- `func`: синхронный predicate.

Обработчик получает совместимую оболочку `telebot.types.Message` или
`telebot.types.CallbackQuery`. Обработчики проверяются по порядку регистрации, и первый совпавший
останавливает дальнейший plugin-fallback.

Методы `cardinal.telegram.msg_handler()` и `cardinal.telegram.cbq_handler()` являются короткими
формами регистрации. `cardinal.telegram.add_command_to_menu()` и `set_state()` сейчас являются
совместимыми заглушками и ничего не меняют.

### 6.3 Реализованные telebot.types

```python
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)
```

`InlineKeyboardButton` поддерживает `text`, `url`, `callback_data`.

`InlineKeyboardMarkup(row_width=3)` поддерживает:

```python
markup.add(button1, button2, row_width=2)
markup.row(button1, button2)
```

Не используйте ReplyKeyboardMarkup, ForceReply, InputMedia, WebApp и специфические классы
pyTelegramBotAPI: их нет в локальном слое совместимости.

## 7. Полезные объекты FunPayAPI

### 7.1 Message

Для `event.message` безопасно использовать:

- `id`, `text`, `chat_id`, `chat_name`;
- `author`, `author_id`, `interlocutor_id`;
- `type` (`FunPayAPI.types.MessageTypes`);
- `image_link`, `image_name`;
- `by_bot`, `by_vertex`;
- `initiator_username`, `initiator_id`, `i_am_seller`, `i_am_buyer`.

Проверка входящего сообщения:

```python
def is_incoming(account, message):
    return (
        message.author_id not in {0, account.id}
        and not message.by_bot
        and not message.by_vertex
    )
```

### 7.2 OrderShortcut и Order

В событиях обычно приходит `OrderShortcut`. Для полной карточки:

```python
order = cardinal.account.get_order(event.order.id)
```

Часто используемые поля: `id`, `description`, `price`, `currency`, `buyer_username`, `buyer_id`,
`chat_id`, `status`, `date`. У полного заказа дополнительно доступны `seller_id`, `buyer_id`,
`title`, `review` и другие подробности.

### 7.3 Review

`order.review` равен `None` либо содержит:

- `stars`: `1..5` или `None`;
- `text`: комментарий;
- `reply`: существующий ответ продавца;
- `hidden`, `anonymous`;
- `order_id`, `author`, `author_id`;
- `by_bot`, `reply_by_bot`.

Ответ продавца:

```python
cardinal.account.send_review(order.id, "Спасибо за отзыв!")
```

Текст ограничивайте 999 символами и 10 строками, как делает основной бот.

## 8. Рецепт: автоответ на отзыв

Этот пример отвечает только на новый или изменённый отзыв покупателя, только для заказа текущего
продавца и не отвечает повторно, если ответ уже существует.

```python
import re

from FunPayAPI import types

NAME = "Review Auto Reply"
VERSION = "1.0.0"
DESCRIPTION = "Безопасный автоответ на отзывы"
CREDITS = "Your name"
SETTINGS_PAGE = False
UUID = "СОЗДАЙТЕ-НОВЫЙ-КАНОНИЧЕСКИЙ-UUID4"
BIND_TO_DELETE = None


def extract_order_id(message):
    match = re.search(r"#([A-Za-z0-9_-]{4,40})", str(message or ""))
    return match.group(1) if match else None


def on_review(cardinal, event):
    message = event.message
    if message.type not in {
        types.MessageTypes.NEW_FEEDBACK,
        types.MessageTypes.FEEDBACK_CHANGED,
    }:
        return

    order_id = extract_order_id(message)
    if not order_id:
        return
    order = cardinal.account.get_order(order_id)
    review = getattr(order, "review", None)
    if not review or order.seller_id != cardinal.account.id:
        return
    if review.reply:
        return

    stars = int(review.stars or 0)
    templates = {
        1: "Спасибо за обратную связь. Напишите нам в чат — разберёмся.",
        2: "Спасибо за отзыв. Помогите нам уточнить, что пошло не так.",
        3: "Спасибо! Учтём ваши замечания.",
        4: "Спасибо за хорошую оценку и заказ!",
        5: "Спасибо за отличную оценку! Будем рады видеть вас снова.",
    }
    text = templates.get(stars)
    if text:
        cardinal.account.send_review(order.id, text[:999])


BIND_TO_NEW_MESSAGE = [on_review]
```

Основной бот уже имеет встроенный автоответ на отзывы с пятью отдельными шаблонами и переменными.
Не устанавливайте одновременно второй плагин с таким же поведением, иначе возможна гонка двух
ответов.

## 9. Рецепт: команда в FunPay-чате

```python
def on_message(cardinal, event):
    message = event.message
    if not is_incoming(cardinal.account, message):
        return
    if (message.text or "").strip().casefold() != "#help":
        return
    cardinal.account.send_message(
        message.chat_id,
        "Доступные команды: #help, #status",
        message.chat_name,
    )


BIND_TO_NEW_MESSAGE = [on_message]
```

Не используйте `time.sleep()` в обработчике: он задержит обработку событий этого плагина. Для
долгих операций лучше сделать собственный управляемый worker с остановкой в `BIND_TO_PRE_STOP`.

## 10. Рецепт: кнопка в Telegram

Регистрируйте обработчики один раз из `BIND_TO_PRE_INIT`:

```python
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


def register_telegram(cardinal):
    bot = cardinal.telegram.bot

    def show(call):
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "Настройки плагина")

    bot.register_callback_query_handler(
        show,
        func=lambda call: call.data == "example_plugin:settings",
    )

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(
            "Открыть настройки",
            callback_data="example_plugin:settings",
        )
    )
    cardinal.telegram.send_notification(
        "Плагин запущен.",
        reply_markup=markup,
    )


BIND_TO_PRE_INIT = [register_telegram]
```

Используйте уникальный префикс callback_data, чтобы не пересекаться с ботом и другими плагинами.

## 11. Полный безопасный каркас

```python
from __future__ import annotations

import html
import logging

from FunPayAPI import types

logger = logging.getLogger("fpc_plugin.my_plugin")

NAME = "My Plugin"
VERSION = "1.0.0"
DESCRIPTION = "Краткое описание"
CREDITS = "Author"
SETTINGS_PAGE = False
UUID = "СОЗДАЙТЕ-НОВЫЙ-КАНОНИЧЕСКИЙ-UUID4"


def pre_init(cardinal):
    # Регистрируйте Telegram handlers здесь, не при каждом событии.
    return None


def new_message(cardinal, event):
    message = event.message
    if message.author_id in {0, cardinal.account.id}:
        return
    if message.by_bot or message.by_vertex:
        return
    text = (message.text or "").strip()
    if text.casefold() != "#example":
        return
    cardinal.account.send_message(
        message.chat_id,
        "Получено: " + text,
        message.chat_name,
    )


def new_order(cardinal, event):
    order = event.order
    cardinal.telegram.send_notification(
        "🛒 Новый заказ <code>#" + html.escape(str(order.id)) + "</code>"
    )


def on_delete(cardinal, callback):
    logger.info("Plugin %s deleted", UUID)


BIND_TO_PRE_INIT = [pre_init]
BIND_TO_POST_INIT = []
BIND_TO_PRE_START = []
BIND_TO_POST_START = []
BIND_TO_PRE_STOP = []
BIND_TO_POST_STOP = []
BIND_TO_INIT_MESSAGE = []
BIND_TO_MESSAGES_LIST_CHANGED = []
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = []
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_INIT_ORDER = []
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_ORDERS_LIST_CHANGED = []
BIND_TO_ORDER_STATUS_CHANGED = []
BIND_TO_PRE_DELIVERY = []
BIND_TO_POST_DELIVERY = []
BIND_TO_PRE_LOTS_RAISE = []
BIND_TO_POST_LOTS_RAISE = []
BIND_TO_DELETE = on_delete
```

## 12. Ошибки, которых следует избегать

- Неверный UUID: не UUID4, верхний регистр или фигурные скобки.
- `BIND_TO_DELETE = []`: это поле должно быть функцией или `None`.
- `BIND_TO_NEW_MESSAGE = on_message`: должен быть список `[on_message]`.
- `async def` в списке хуков: загрузчик вызовет её как обычную функцию и coroutine не выполнится.
- Telegram-вызов из потока, созданного самим плагином после остановки runtime.
- Прямое использование глобального Telegram chat ID другого пользователя.
- Ответ на системное или собственное сообщение без проверок.
- Повторная регистрация Telegram handlers на каждом `NEW_MESSAGE`.
- Блокирующий бесконечный цикл в PRE_START.
- Импорт `telebot.TeleBot`: локальный shim предоставляет только `telebot.types`.
- Ожидание, что `SETTINGS_PAGE=True` автоматически создаст кнопку.
- Использование delivery hooks как единственного механизма выдачи: сейчас они не генерируются.
- Хранение состояния только в глобальной переменной без учёта перезапуска процесса.
- Вставка пользовательского текста в Telegram HTML без `html.escape`.

## 13. Финальный чек-лист

Перед выдачей файла нейросеть должна проверить:

- [ ] один `.py` файл, UTF-8, меньше 512 КБ;
- [ ] первая строка не содержит `noplug`;
- [ ] уникальные NAME и канонический UUID4;
- [ ] все семь обязательных полей объявлены;
- [ ] каждый `BIND_TO_*` — list/tuple функций;
- [ ] `BIND_TO_DELETE` — функция или `None`;
- [ ] нет `async def` в хуках;
- [ ] нет сетевой работы на уровне импорта;
- [ ] используются только документированные adapter/Telegram методы;
- [ ] входящие сообщения отделены от собственных и системных;
- [ ] пользовательские данные экранируются для Telegram HTML;
- [ ] callback_data уникальны и короче 64 байт;
- [ ] автоответ на отзыв не дублирует уже существующий `review.reply`;
- [ ] исключения внешнего API обрабатываются там, где возможна безопасная деградация;
- [ ] внешние зависимости перечислены отдельно и добавлены в Docker-образ;
- [ ] плагин можно выключить и удалить без оставшегося фонового процесса.

## 14. Готовый запрос для другой нейросети

Скопируйте этот файл вместе с запросом:

> Создай один production-ready `.py` плагин для контракта `aiogram-compat-1`, строго соблюдая
> `PLUGIN_DEVELOPMENT.md`. Не выдумывай методы. Сначала перечисли выбранные хуки и объясни, почему
> они реально вызываются. Затем выдай полный файл. Создай новый UUID4. Не используй внешние
> зависимости. Добавь проверки от собственных сообщений, повторной отправки и отсутствующих
> атрибутов. В конце самостоятельно пройди финальный чек-лист из документа.

Даже при соблюдении контракта FunPay остаётся внешним неофициальным API: изменения HTML сайта или
ограничения конкретного аккаунта невозможно полностью исключить статической проверкой.
