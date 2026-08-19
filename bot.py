import asyncio
import logging
import os
import re
import sys
import time
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Generator

import requests
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Импорт FunPayAPI
try:
    from FunPayAPI import Account, Runner, types, enums
except ImportError:
    print("❌ FunPayAPI не найден! Устанавливаю...")
    os.system("pip install FunPayAPI")
    from FunPayAPI import Account, Runner, types, enums

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
GOLDEN_KEY = os.environ.get('GOLDEN_KEY')
LOGS_DIR = 'logs'
CONFIGS_DIR = 'configs'

for d in [LOGS_DIR, CONFIGS_DIR]:
    os.makedirs(d, exist_ok=True)

# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOGS_DIR, 'bot.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ================== FSM СОСТОЯНИЯ ==================
class ProxySetupStates(StatesGroup):
    waiting_for_funpay_proxy = State()

class AutoResponseStates(StatesGroup):
    waiting_for_command = State()
    waiting_for_response = State()
    waiting_for_edit_response = State()


# ================== ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ==================
class ProxyValidator:
    """Валидатор прокси."""

    @staticmethod
    def parse_proxy(proxy_str: str) -> Optional[dict]:
        """Парсит прокси строку в формат для FunPayAPI."""
        if not proxy_str or not proxy_str.strip():
            return None

        proxy_str = proxy_str.strip()
        
        # Формат: scheme://login:password@ip:port или ip:port
        pattern = r'^(?:(?P<scheme>https?|socks[45])://)?(?:(?P<login>[^:]+):(?P<password>[^@]+)@)?(?P<ip>[\d\w\.-]+):(?P<port>\d+)$'
        match = re.match(pattern, proxy_str)

        if not match:
            return None

        scheme = match.group('scheme') or 'http'
        login = match.group('login')
        password = match.group('password')
        ip = match.group('ip')
        port = match.group('port')

        # Формируем URL
        if login and password:
            proxy_url = f"{scheme}://{login}:{password}@{ip}:{port}"
        else:
            proxy_url = f"{scheme}://{ip}:{port}"

        return {
            'http': proxy_url,
            'https': proxy_url,
            'raw': proxy_str
        }

    @staticmethod
    def test_proxy(proxy_dict: dict) -> tuple[bool, str]:
        """Тестирует прокси на работоспособность."""
        try:
            # Пробуем подключиться к FunPay через прокси
            response = requests.get(
                'https://funpay.com/',
                proxies=proxy_dict,
                timeout=10
            )
            
            if response.status_code == 200:
                return True, "✅ Прокси работает!"
            else:
                return False, f"❌ HTTP {response.status_code}"
                
        except requests.exceptions.ProxyError as e:
            return False, f"❌ Ошибка прокси: {str(e)[:50]}"
        except requests.exceptions.Timeout:
            return False, "❌ Таймаут подключения"
        except Exception as e:
            return False, f"❌ Ошибка: {str(e)[:50]}"


class ConfigManager:
    """Менеджер конфигурации."""

    DEFAULT_CONFIG = {
        "FunPay": {
            "golden_key": "",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "autoRaise": "0",
            "autoResponse": "1",
        },
        "Telegram": {
            "chat_id": ""
        },
        "Proxy": {
            "proxy": "",
        },
        "Other": {
            "requestsDelay": "4"
        }
    }

    @classmethod
    def load_or_create(cls, path: str = os.path.join(CONFIGS_DIR, 'main.cfg')) -> ConfigParser:
        config = ConfigParser(delimiters=(':',), interpolation=None)
        config.optionxform = str

        if os.path.exists(path):
            config.read(path, encoding='utf-8')
            return config

        config.read_dict(cls.DEFAULT_CONFIG)
        config.set("FunPay", "golden_key", GOLDEN_KEY or "")

        with open(path, 'w', encoding='utf-8') as f:
            config.write(f)

        return config

    @classmethod
    def save(cls, config: ConfigParser, path: str = os.path.join(CONFIGS_DIR, 'main.cfg')):
        with open(path, 'w', encoding='utf-8') as f:
            config.write(f)

    @classmethod
    def load_auto_response(cls, path: str = os.path.join(CONFIGS_DIR, 'auto_response.cfg')) -> ConfigParser:
        config = ConfigParser(delimiters=(':',), interpolation=None)
        config.optionxform = str

        if os.path.exists(path):
            config.read(path, encoding='utf-8')
        else:
            with open(path, 'w', encoding='utf-8'):
                pass
        return config

    @classmethod
    def save_auto_response(cls, config: ConfigParser, path: str = os.path.join(CONFIGS_DIR, 'auto_response.cfg')):
        with open(path, 'w', encoding='utf-8') as f:
            config.write(f)


# ================== ГЛАВНЫЙ КЛАСС БОТА ==================
class FunPayBot:
    """Главный класс бота."""

    def __init__(self):
        self.config = ConfigManager.load_or_create()
        self.ar_config = ConfigManager.load_auto_response()
        self.account: Optional[Account] = None
        self.runner: Optional[Runner] = None
        self.profile = None
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.is_running = False
        self.runner_task: Optional[asyncio.Task] = None
        self.proxy_dict: Optional[dict] = None

    async def init_account(self) -> tuple[bool, str]:
        """Инициализирует FunPay аккаунт."""
        try:
            # Получаем прокси из конфига
            proxy_str = self.config.get("Proxy", "proxy", fallback="")
            
            if proxy_str:
                self.proxy_dict = ProxyValidator.parse_proxy(proxy_str)
                logger.info(f"🌐 Используем прокси: {proxy_str}")
            else:
                self.proxy_dict = None

            # Создаём аккаунт
            self.account = Account(
                GOLDEN_KEY,
                user_agent=self.config["FunPay"].get("user_agent", ""),
                proxy=self.proxy_dict
            )

            # Инициализируем аккаунт (получаем данные)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.account.get)

            # Проверяем что аккаунт инициализирован
            if not self.account.is_initiated:
                return False, "❌ Аккаунт не инициализирован"

            logger.info(f"✅ Аккаунт инициализирован: {self.account.username} (ID: {self.account.id})")
            return True, f"✅ Успешный вход как {self.account.username}"

        except Exception as e:
            logger.error(f"❌ Ошибка инициализации аккаунта: {e}")
            return False, f"❌ Ошибка: {str(e)[:100]}"

    async def get_profile(self):
        """Получает профиль пользователя."""
        if not self.account or not self.account.is_initiated:
            return None

        try:
            loop = asyncio.get_event_loop()
            self.profile = await loop.run_in_executor(
                None,
                lambda: self.account.get_user(self.account.id)
            )
            return self.profile
        except Exception as e:
            logger.error(f"Ошибка получения профиля: {e}")
            return None

    async def send_funpay_message(self, chat_id: int, text: str) -> bool:
        """Отправляет сообщение в чат FunPay."""
        if not self.account:
            return False

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self.account.send_message(chat_id, text)
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            return False

    async def runner_loop(self):
        """Бесконечный цикл обработки событий FunPay."""
        logger.info("🔄 Runner запущен")
        
        self.runner = Runner(self.account)
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()

        def sync_listen():
            try:
                for event in self.runner.listen(
                    requests_delay=int(self.config["Other"].get("requestsDelay", 4))
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as e:
                logger.error(f"Ошибка в runner: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, sync_listen)

        while self.is_running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                if event is None:
                    break
                await self._handle_event(event)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Ошибка обработки события: {e}")

    async def _handle_event(self, event):
        """Обрабатывает события FunPay."""
        try:
            if event.type is enums.EventTypes.NEW_MESSAGE:
                await self._handle_new_message(event)
            elif event.type is enums.EventTypes.NEW_ORDER:
                await self._handle_new_order(event)
            elif event.type is enums.EventTypes.ORDER_STATUS_CHANGED:
                await self._handle_order_status(event)
        except Exception as e:
            logger.error(f"Ошибка обработки события: {e}")

    async def _handle_new_message(self, event):
        """Обработка нового сообщения."""
        msg = event.message
        
        # Игнорируем свои сообщения
        if msg.author_id == self.account.id:
            return

        chat_name = msg.chat_name or "Неизвестно"
        author = msg.author or "Неизвестно"
        text = msg.text or msg.image_link or "📷 Изображение"

        logger.info(f"💬 Новое сообщение от {author} в чате {chat_name}: {text}")

        # Автоответчик
        await self._check_auto_response(msg.chat_id, text, chat_name)

        # Уведомление
        await self._send_notification(
            f"💬 <b>Новое сообщение</b>\n\n"
            f"👤 <b>От:</b> {author}\n"
            f"💬 <b>Чат:</b> {chat_name}\n\n"
            f"<code>{text}</code>"
        )

    async def _handle_new_order(self, event):
        """Обработка нового заказа."""
        order = event.order

        logger.info(f"🛒 Новый заказ #{order.id}: {order.description}")

        await self._send_notification(
            f"🛒 <b>Новый заказ!</b>\n\n"
            f"🆔 <b>ID:</b> <code>{order.id}</code>\n"
            f"👤 <b>Покупатель:</b> {order.buyer_username}\n"
            f"📦 <b>Товар:</b> {order.description}\n"
            f"💰 <b>Цена:</b> {order.price} {order.currency.name}"
        )

    async def _handle_order_status(self, event):
        """Обработка изменения статуса заказа."""
        order = event.order
        logger.info(f"📋 Статус заказа #{order.id}: {order.status}")

        status_map = {
            'PAID': '💰 Оплачен',
            'CLOSED': '✅ Закрыт',
            'REFUNDED': '🔙 Возвращён'
        }

        await self._send_notification(
            f"📋 <b>Изменение статуса</b>\n\n"
            f"🆔 <b>ID:</b> <code>{order.id}</code>\n"
            f"👤 <b>Покупатель:</b> {order.buyer_username}\n"
            f"📊 <b>Статус:</b> {status_map.get(str(order.status.name), order.status)}"
        )

    async def _check_auto_response(self, chat_id: int, text: str, chat_name: str):
        """Проверяет автоответчик."""
        if not self.config["FunPay"].getboolean("autoResponse"):
            return

        command = text.strip().lower()

        if command not in self.ar_config.sections():
            return

        if not self.ar_config[command].getboolean("enabled", fallback=True):
            return

        response = self.ar_config[command].get("response", "")
        if not response:
            return

        logger.info(f"⚡ Автоответ на команду '{command}' в чате {chat_name}")

        await self.send_funpay_message(chat_id, response)

        await self._send_notification(
            f"⚡ <b>Автоответ отправлен</b>\n\n"
            f"💬 <b>Чат:</b> {chat_name}\n"
            f"📥 <b>Команда:</b> <code>{command}</code>\n"
            f"📤 <b>Ответ:</b> <code>{response}</code>"
        )

    async def _send_notification(self, text: str, parse_mode: str = ParseMode.HTML):
        """Отправляет уведомление в Telegram."""
        if not self.bot:
            return

        chat_id = self.config.get("Telegram", "chat_id", fallback=None)
        if not chat_id:
            return

        try:
            await self.bot.send_message(
                int(chat_id),
                text,
                parse_mode=parse_mode,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")


# ================== БОТ ==================
bot_instance = FunPayBot()
router = Router()


# ================== КЛАВИАТУРЫ ==================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="⚡ Автоответчик", callback_data="auto_response")],
        [InlineKeyboardButton(text="🔔 Уведомления", callback_data="notifications")],
        [InlineKeyboardButton(text="🌐 Прокси", callback_data="proxy_settings")],
        [InlineKeyboardButton(text="▶️ Запустить Runner", callback_data="start_runner")],
        [InlineKeyboardButton(text="⏹️ Остановить Runner", callback_data="stop_runner")],
    ])


def auto_response_kb() -> InlineKeyboardMarkup:
    kb = []
    for section in bot_instance.ar_config.sections():
        enabled = bot_instance.ar_config[section].getboolean("enabled", fallback=True)
        status = "✅" if enabled else "❌"
        kb.append([InlineKeyboardButton(
            text=f"{status} /{section}",
            callback_data=f"ar_view:{section}"
        )])

    kb.append([InlineKeyboardButton(text="➕ Добавить команду", callback_data="ar_add")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
    ])


def proxy_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Настроить FunPay прокси", callback_data="setup_fp_proxy")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])


# ================== ХЕНДЛЕРЫ ==================
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Старт бота."""
    chat_id = message.chat.id

    # Сохраняем chat_id
    bot_instance.config.set("Telegram", "chat_id", str(chat_id))
    ConfigManager.save(bot_instance.config)

    # Проверяем настроен ли прокси
    fp_proxy = bot_instance.config.get("Proxy", "proxy", fallback="")

    if not fp_proxy:
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Давай настроим прокси для FunPay.\n\n"
            "Отправь прокси в формате:\n"
            "<code>scheme://login:password@ip:port</code>\n\n"
            "Примеры:\n"
            "<code>http://user:pass@1.2.3.4:8080</code>\n"
            "<code>socks5://user:pass@1.2.3.4:1080</code>\n"
            "<code>1.2.3.4:8080</code>\n\n"
            "Или отправь <b>/skip</b> чтобы работать без прокси.",
            parse_mode=ParseMode.HTML
        )
        await state.set_state(ProxySetupStates.waiting_for_funpay_proxy)
        return

    await message.answer(
        "✅ <b>Бот запущен!</b>\n\nИспользуй меню:",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML
    )


@router.message(Command("skip"), StateFilter(ProxySetupStates.waiting_for_funpay_proxy))
async def skip_fp_proxy(message: types.Message, state: FSMContext):
    """Пропустить FunPay прокси."""
    await state.clear()
    await message.answer(
        "⏭️ Работа без прокси.\n\n"
        "✅ <b>Настройка завершена!</b>",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML
    )


@router.message(StateFilter(ProxySetupStates.waiting_for_funpay_proxy))
async def process_fp_proxy(message: types.Message, state: FSMContext):
    """Обработка FunPay прокси."""
    proxy_str = message.text.strip()
    parsed = ProxyValidator.parse_proxy(proxy_str)

    if not parsed:
        await message.answer(
            "❌ <b>Неверный формат прокси!</b>\n\n"
            "Формат: <code>scheme://login:password@ip:port</code>\n\n"
            "Попробуй ещё раз:",
            parse_mode=ParseMode.HTML
        )
        return

    # Тестируем прокси
    await message.answer("🔄 Проверяю прокси...")
    
    success, msg = ProxyValidator.test_proxy(parsed)
    
    if not success:
        await message.answer(
            f"{msg}\n\nПопробуй другой прокси или отправь /skip",
            parse_mode=ParseMode.HTML
        )
        return

    await message.answer(f"{msg}\n\n🔄 Пытаюсь войти в FunPay...")

    # Сохраняем прокси
    bot_instance.config.set("Proxy", "proxy", proxy_str)
    ConfigManager.save(bot_instance.config)

    # Пытаемся авторизоваться
    auth_success, auth_msg = await bot_instance.init_account()

    if auth_success:
        await message.answer(
            f"{auth_msg}\n\n"
            f"✅ <b>Настройка завершена!</b>",
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML
        )
        await state.clear()
    else:
        await message.answer(
            f"{auth_msg}\n\n"
            f"Проверь GOLDEN_KEY в переменных окружения.\n"
            f"Отправь новый прокси или /skip",
            parse_mode=ParseMode.HTML
        )


@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    """Показать меню."""
    await message.answer("📋 <b>Главное меню</b>", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "back_to_menu")
async def callback_back_to_menu(callback: types.CallbackQuery):
    """Вернуться в меню."""
    await callback.message.edit_text("📋 <b>Главное меню</b>", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data == "profile")
async def callback_profile(callback: types.CallbackQuery):
    """Показать профиль."""
    await callback.answer("Загружаю профиль...")

    # Инициализируем аккаунт если нужно
    if not bot_instance.account or not bot_instance.account.is_initiated:
        success, msg = await bot_instance.init_account()
        if not success:
            await callback.message.edit_text(
                f"❌ {msg}",
                reply_markup=back_to_menu_kb(),
                parse_mode=ParseMode.HTML
            )
            return

    profile = await bot_instance.get_profile()
    if not profile:
        await callback.message.edit_text(
            "❌ Не удалось загрузить профиль!",
            reply_markup=back_to_menu_kb(),
            parse_mode=ParseMode.HTML
        )
        return

    lots_count = len(profile.get_lots()) if profile.get_lots() else 0

    text = (
        f"👤 <b>Профиль FunPay</b>\n\n"
        f"📛 <b>Никнейм:</b> {bot_instance.account.username}\n"
        f"🆔 <b>ID:</b> <code>{bot_instance.account.id}</code>\n\n"
        f"📦 <b>Активных лотов:</b> {lots_count}"
    )

    await callback.message.edit_text(text, reply_markup=back_to_menu_kb(), parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "auto_response")
async def callback_auto_response(callback: types.CallbackQuery):
    """Автоответчик."""
    await callback.message.edit_text(
        "⚡ <b>Автоответчик</b>",
        reply_markup=auto_response_kb(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ar_view:"))
async def callback_ar_view(callback: types.CallbackQuery):
    """Просмотр команды."""
    command = callback.data.split(":", 1)[1]

    if command not in bot_instance.ar_config.sections():
        await callback.answer("Команда не найдена!", show_alert=True)
        return

    enabled = bot_instance.ar_config[command].getboolean("enabled", fallback=True)
    response = bot_instance.ar_config[command].get("response", "")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Включить" if not enabled else "❌ Выключить",
            callback_data=f"ar_toggle:{command}"
        )],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ar_edit:{command}")],
        [InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"ar_delete:{command}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="auto_response")]
    ])

    await callback.message.edit_text(
        f"⚡ <b>Команда:</b> <code>/{command}</code>\n\n"
        f"📊 <b>Статус:</b> {'✅ Включена' if enabled else '❌ Выключена'}\n\n"
        f"📤 <b>Ответ:</b>\n<code>{response}</code>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ar_toggle:"))
async def callback_ar_toggle(callback: types.CallbackQuery):
    """Переключить статус."""
    command = callback.data.split(":", 1)[1]

    if command not in bot_instance.ar_config.sections():
        await callback.answer("Команда не найдена!", show_alert=True)
        return

    current = bot_instance.ar_config[command].getboolean("enabled", fallback=True)
    bot_instance.ar_config[command]["enabled"] = str(not current).lower()
    ConfigManager.save_auto_response(bot_instance.ar_config)

    await callback.answer(f"Команда {'выключена' if current else 'включена'}!", show_alert=True)
    await callback_ar_view(callback)


@router.callback_query(F.data == "ar_add")
async def callback_ar_add(callback: types.CallbackQuery, state: FSMContext):
    """Добавить команду."""
    await callback.message.edit_text(
        "➕ <b>Добавление команды</b>\n\nОтправь команду (без /):",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(AutoResponseStates.waiting_for_command)
    await callback.answer()


@router.message(StateFilter(AutoResponseStates.waiting_for_command))
async def process_ar_command(message: types.Message, state: FSMContext):
    """Обработка команды."""
    command = message.text.strip().lower()

    if not command or not re.match(r'^[a-zа-яё0-9_]+$', command):
        await message.answer("❌ Неверный формат команды!", parse_mode=ParseMode.HTML)
        return

    if command in bot_instance.ar_config.sections():
        await message.answer(f"❌ Команда <code>/{command}</code> уже существует!", parse_mode=ParseMode.HTML)
        await state.clear()
        return

    await state.update_data(command=command)
    await state.set_state(AutoResponseStates.waiting_for_response)
    await message.answer(f"Отправь ответ для команды <code>/{command}</code>:", parse_mode=ParseMode.HTML)


@router.message(StateFilter(AutoResponseStates.waiting_for_response))
async def process_ar_response(message: types.Message, state: FSMContext):
    """Обработка ответа."""
    data = await state.get_data()
    command = data.get("command")
    response = message.text.strip()

    bot_instance.ar_config.add_section(command)
    bot_instance.ar_config[command]["enabled"] = "true"
    bot_instance.ar_config[command]["response"] = response
    ConfigManager.save_auto_response(bot_instance.ar_config)

    await message.answer(
        f"✅ <b>Команда <code>/{command}</code> добавлена!</b>\n\n"
        f"📤 <b>Ответ:</b>\n<code>{response}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_to_menu_kb()
    )
    await state.clear()


@router.callback_query(F.data.startswith("ar_edit:"))
async def callback_ar_edit(callback: types.CallbackQuery, state: FSMContext):
    """Редактировать команду."""
    command = callback.data.split(":", 1)[1]
    await state.update_data(command=command)
    await state.set_state(AutoResponseStates.waiting_for_edit_response)

    await callback.message.edit_text(
        f"✏️ <b>Редактирование <code>/{command}</code></b>\n\nОтправь новый ответ:",
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.message(StateFilter(AutoResponseStates.waiting_for_edit_response))
async def process_ar_edit_response(message: types.Message, state: FSMContext):
    """Обработка редактирования."""
    data = await state.get_data()
    command = data.get("command")
    response = message.text.strip()

    if command not in bot_instance.ar_config.sections():
        await message.answer("❌ Команда не найдена!")
        await state.clear()
        return

    bot_instance.ar_config[command]["response"] = response
    ConfigManager.save_auto_response(bot_instance.ar_config)

    await message.answer(
        f"✅ <b>Команда обновлена!</b>\n\n📤 <b>Ответ:</b>\n<code>{response}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_to_menu_kb()
    )
    await state.clear()


@router.callback_query(F.data.startswith("ar_delete:"))
async def callback_ar_delete(callback: types.CallbackQuery):
    """Удалить команду."""
    command = callback.data.split(":", 1)[1]

    if command not in bot_instance.ar_config.sections():
        await callback.answer("Команда не найдена!", show_alert=True)
        return

    bot_instance.ar_config.remove_section(command)
    ConfigManager.save_auto_response(bot_instance.ar_config)

    await callback.answer(f"Команда /{command} удалена!", show_alert=True)

    await callback.message.edit_text(
        "⚡ <b>Автоответчик</b>",
        reply_markup=auto_response_kb(),
        parse_mode=ParseMode.HTML
    )


@router.callback_query(F.data == "notifications")
async def callback_notifications(callback: types.CallbackQuery):
    """Уведомления."""
    chat_id = bot_instance.config.get("Telegram", "chat_id", fallback="Не установлен")

    await callback.message.edit_text(
        f"🔔 <b>Уведомления</b>\n\n📍 <b>Chat ID:</b> <code>{chat_id}</code>",
        reply_markup=back_to_menu_kb(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data == "proxy_settings")
async def callback_proxy_settings(callback: types.CallbackQuery):
    """Настройки прокси."""
    fp_proxy = bot_instance.config.get("Proxy", "proxy", fallback="Не настроен")

    await callback.message.edit_text(
        f"🌐 <b>Настройки прокси</b>\n\n"
        f"🎮 <b>FunPay:</b>\n<code>{fp_proxy[:40] + '...' if len(fp_proxy) > 40 else fp_proxy}</code>",
        reply_markup=proxy_settings_kb(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data == "setup_fp_proxy")
async def callback_setup_fp_proxy(callback: types.CallbackQuery, state: FSMContext):
    """Настроить FunPay прокси."""
    await state.set_state(ProxySetupStates.waiting_for_funpay_proxy)
    await callback.message.edit_text(
        "🎮 <b>Настройка FunPay прокси</b>\n\n"
        "Отправь прокси:\n"
        "<code>scheme://login:password@ip:port</code>\n\n"
        "Примеры:\n"
        "<code>http://user:pass@1.2.3.4:8080</code>\n"
        "<code>socks5://user:pass@1.2.3.4:1080</code>\n"
        "<code>1.2.3.4:8080</code>",
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data == "start_runner")
async def callback_start_runner(callback: types.CallbackQuery):
    """Запустить Runner."""
    if bot_instance.is_running:
        await callback.answer("⚠️ Runner уже запущен!", show_alert=True)
        return

    await callback.answer("🔄 Запускаю Runner...")

    # Инициализируем аккаунт если нужно
    if not bot_instance.account or not bot_instance.account.is_initiated:
        success, msg = await bot_instance.init_account()
        if not success:
            await callback.message.edit_text(
                f"❌ {msg}",
                reply_markup=back_to_menu_kb(),
                parse_mode=ParseMode.HTML
            )
            return

    bot_instance.is_running = True
    bot_instance.runner_task = asyncio.create_task(bot_instance.runner_loop())

    await callback.message.edit_text(
        "✅ <b>Runner запущен!</b>",
        reply_markup=back_to_menu_kb(),
        parse_mode=ParseMode.HTML
    )

    await bot_instance._send_notification("✅ <b>Runner запущен!</b>")


@router.callback_query(F.data == "stop_runner")
async def callback_stop_runner(callback: types.CallbackQuery):
    """Остановить Runner."""
    if not bot_instance.is_running:
        await callback.answer("⚠️ Runner не запущен!", show_alert=True)
        return

    await callback.answer("⏹️ Останавливаю Runner...")

    bot_instance.is_running = False
    
    if bot_instance.runner_task and not bot_instance.runner_task.done():
        bot_instance.runner_task.cancel()
        try:
            await bot_instance.runner_task
        except asyncio.CancelledError:
            pass

    await callback.message.edit_text(
        "⏹️ <b>Runner остановлен!</b>",
        reply_markup=back_to_menu_kb(),
        parse_mode=ParseMode.HTML
    )
    await bot_instance._send_notification("⏹️ <b>Runner остановлен!</b>")


# ================== ЗАПУСК ==================
async def main():
    """Главная функция."""
    logger.info("🚀 Запуск бота...")

    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не установлен!")
        sys.exit(1)

    if not GOLDEN_KEY:
        logger.warning("⚠️ GOLDEN_KEY не установлен!")

    bot_instance.bot = Bot(token=BOT_TOKEN)
    bot_instance.dp = Dispatcher(storage=MemoryStorage())
    bot_instance.dp.include_router(router)

    await bot_instance.bot.delete_webhook(drop_pending_updates=True)

    logger.info("✅ Бот готов к работе!")

    try:
        await bot_instance.dp.start_polling(bot_instance.bot)
    finally:
        await bot_instance.bot.session.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
