import asyncio
import json
import logging
import os
import re
import sys
import time
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any, Generator

import aiohttp
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

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
GOLDEN_KEY = os.environ.get('GOLDEN_KEY')
LOGS_DIR = 'logs'
CONFIGS_DIR = 'configs'
STORAGE_DIR = 'storage'
PRODUCTS_DIR = os.path.join(STORAGE_DIR, 'products')

for d in [LOGS_DIR, CONFIGS_DIR, STORAGE_DIR, PRODUCTS_DIR]:
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
    waiting_for_telegram_proxy = State()
    waiting_for_funpay_proxy = State()

class AutoResponseStates(StatesGroup):
    waiting_for_command = State()
    waiting_for_response = State()
    waiting_for_edit_command = State()
    waiting_for_edit_response = State()


# ================== FUNPAY API ТИПЫ ==================
class OrderStatus(Enum):
    UNPAID = "unpaid"
    PAID = "paid"
    CLOSED = "closed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


@dataclass
class Message:
    id: int
    text: str
    chat_id: int
    author: str
    author_id: int
    by_bot: bool = False
    image_link: Optional[str] = None


@dataclass
class ChatShortcut:
    id: int
    name: str
    unread: bool = False
    last_message_text: str = ""


@dataclass
class Order:
    id: str
    description: str
    price: float
    currency: str
    buyer_username: str
    buyer_id: int
    chat_id: int
    status: OrderStatus
    created_at: datetime


@dataclass
class Lot:
    id: int
    description: str
    price: float
    active: bool


@dataclass
class UserProfile:
    id: int
    username: str
    lots: List[Lot]


# ================== FUNPAY API КЛАССЫ ==================
class FunPayAPI:
    """Минимальная реализация FunPay API."""

    BASE_URL = "https://funpay.com"

    def __init__(self, golden_key: str, user_agent: str, proxy: Optional[dict] = None):
        self.golden_key = golden_key
        self.user_agent = user_agent
        self.proxy = proxy or {}
        self.id: Optional[int] = None
        self.username: Optional[str] = None
        self.csrf_token: Optional[str] = None
        self.phpsessid: Optional[str] = None
        self._session = requests.Session()
        self._setup_session()

    def _setup_session(self):
        """Настройка сессии requests."""
        self._session.cookies.set('golden_key', self.golden_key, domain='funpay.com')
        self._session.headers.update({
            'User-Agent': self.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
        })
        if self.proxy:
            self._session.proxies.update(self.proxy)

    def get(self, update_phpsessid: bool = False):
        """Инициализация аккаунта и получение базовой информации."""
        try:
            response = self._session.get(f"{self.BASE_URL}/", timeout=30)
            response.raise_for_status()

            # Парсим CSRF токен
            csrf_match = re.search(r'"csrf-token":"([^"]+)"', response.text)
            if csrf_match:
                self.csrf_token = csrf_match.group(1)

            # Парсим ID и username из страницы
            user_match = re.search(r'/users/(\d+)/', response.text)
            if user_match:
                self.id = int(user_match.group(1))

            username_match = re.search(r'<div class="media-user-name">([^<]+)</div>', response.text)
            if username_match:
                self.username = username_match.group(1).strip()

            # Получаем PHPSESSID
            if update_phpsessid or not self.phpsessid:
                phpsessid = self._session.cookies.get('PHPSESSID', domain='funpay.com')
                if phpsessid:
                    self.phpsessid = phpsessid

            logger.info(f"✅ Аккаунт инициализирован: {self.username} (ID: {self.id})")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка инициализации аккаунта: {e}")
            raise

    def get_user(self, user_id: int) -> UserProfile:
        """Получает профиль пользователя."""
        try:
            response = self._session.get(
                f"{self.BASE_URL}/users/{user_id}/",
                timeout=30
            )
            response.raise_for_status()

            # Парсим лоты со страницы профиля
            lots = self._parse_lots(response.text)

            return UserProfile(
                id=user_id,
                username=self.username,
                lots=lots
            )

        except Exception as e:
            logger.error(f"Ошибка получения профиля: {e}")
            raise

    def _parse_lots(self, html: str) -> List[Lot]:
        """Парсит лоты с HTML страницы."""
        lots = []
        # Упрощённый парсинг лотов
        lot_pattern = re.findall(
            r'<div class="tc-item.*?data-id="(\d+)".*?>.*?<div[^>]*>([^<]+)</div>.*?</div>',
            html,
            re.DOTALL
        )

        for lot_id, description in lot_pattern[:50]:  # Ограничиваем 50 лотами
            try:
                lots.append(Lot(
                    id=int(lot_id),
                    description=description.strip(),
                    price=0.0,
                    active=True
                ))
            except:
                continue

        return lots

    def send_message(self, chat_id: int, text: str, chat_name: Optional[str] = None) -> bool:
        """Отправляет сообщение в чат."""
        try:
            payload = {
                'action': 'add_chat_message',
                'data': {
                    'node': chat_id,
                    'content': text
                }
            }

            headers = {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
            }

            if self.csrf_token:
                headers['X-CSRF-Token'] = self.csrf_token

            response = self._session.post(
                f"{self.BASE_URL}/runner/",
                json=payload,
                headers=headers,
                timeout=30
            )

            return response.status_code == 200

        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            return False

    def get_chats(self) -> List[Dict[str, Any]]:
        """Получает список чатов."""
        try:
            payload = {
                'action': 'get_chats',
                'data': {}
            }

            headers = {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
            }

            if self.csrf_token:
                headers['X-CSRF-Token'] = self.csrf_token

            response = self._session.post(
                f"{self.BASE_URL}/runner/",
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                return data.get('response', {}).get('chats', [])

            return []

        except Exception as e:
            logger.error(f"Ошибка получения чатов: {e}")
            return []

    def get_chat_messages(self, chat_id: int, last_message_id: int = 0) -> List[Dict[str, Any]]:
        """Получает сообщения из чата."""
        try:
            payload = {
                'action': 'get_chat_messages',
                'data': {
                    'node': chat_id,
                    'last_message': last_message_id
                }
            }

            headers = {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
            }

            if self.csrf_token:
                headers['X-CSRF-Token'] = self.csrf_token

            response = self._session.post(
                f"{self.BASE_URL}/runner/",
                json=payload,
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                return data.get('response', {}).get('messages', [])

            return []

        except Exception as e:
            logger.error(f"Ошибка получения сообщений: {e}")
            return []

    def get_orders(self) -> List[Dict[str, Any]]:
        """Получает список заказов."""
        try:
            response = self._session.get(
                f"{self.BASE_URL}/orders/",
                timeout=30
            )
            response.raise_for_status()

            # Упрощённый парсинг заказов
            orders = []
            order_pattern = re.findall(
                r'<a href="/orders/([^"]+)/".*?<div[^>]*>([^<]+)</div>.*?<span class="tc-price">([^<]+)</span>',
                response.text,
                re.DOTALL
            )

            for order_id, description, price in order_pattern[:20]:
                try:
                    price_value = float(re.sub(r'[^\d.]', '', price))
                    orders.append({
                        'id': order_id,
                        'description': description.strip(),
                        'price': price_value
                    })
                except:
                    continue

            return orders

        except Exception as e:
            logger.error(f"Ошибка получения заказов: {e}")
            return []

    def raise_lots(self, category_id: int) -> bool:
        """Поднимает лоты в категории."""
        try:
            payload = {
                'action': 'raise_lots',
                'data': {
                    'game_id': category_id
                }
            }

            headers = {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
            }

            if self.csrf_token:
                headers['X-CSRF-Token'] = self.csrf_token

            response = self._session.post(
                f"{self.BASE_URL}/runner/",
                json=payload,
                headers=headers,
                timeout=30
            )

            return response.status_code == 200

        except Exception as e:
            logger.error(f"Ошибка поднятия лотов: {e}")
            return False


class FunPayRunner:
    """Runner для прослушивания событий FunPay."""

    def __init__(self, account: FunPayAPI):
        self.account = account
        self._last_chat_ids = set()
        self._last_message_ids: Dict[int, int] = {}
        self._last_order_ids = set()
        self._running = False

    def listen(self, requests_delay: int = 4) -> Generator:
        """Генератор событий FunPay."""
        self._running = True
        logger.info("🔄 Runner запущен")

        while self._running:
            try:
                # Проверяем новые чаты
                yield from self._check_new_chats()

                # Проверяем новые сообщения в чатах
                yield from self._check_new_messages()

                # Проверяем новые заказы
                yield from self._check_new_orders()

                time.sleep(requests_delay)

            except Exception as e:
                logger.error(f"Ошибка в runner: {e}")
                time.sleep(5)

    def _check_new_chats(self):
        """Проверяет появление новых чатов."""
        try:
            chats = self.account.get_chats()

            for chat_data in chats:
                chat_id = chat_data.get('id')
                if chat_id and chat_id not in self._last_chat_ids:
                    self._last_chat_ids.add(chat_id)

                    # Создаём событие нового сообщения
                    if chat_data.get('unread'):
                        yield {
                            'type': 'new_message',
                            'chat_id': chat_id,
                            'chat_name': chat_data.get('name', 'Неизвестно'),
                            'text': chat_data.get('last_message', ''),
                            'author': chat_data.get('name', 'Неизвестно')
                        }

        except Exception as e:
            logger.error(f"Ошибка проверки чатов: {e}")

    def _check_new_messages(self):
        """Проверяет новые сообщения в известных чатах."""
        try:
            for chat_id in list(self._last_chat_ids)[:10]:  # Проверяем первые 10 чатов
                last_msg_id = self._last_message_ids.get(chat_id, 0)
                messages = self.account.get_chat_messages(chat_id, last_msg_id)

                for msg_data in messages:
                    msg_id = msg_data.get('id')
                    if msg_id and msg_id > last_msg_id:
                        self._last_message_ids[chat_id] = msg_id

                        # Игнорируем свои сообщения
                        if msg_data.get('author_id') == self.account.id:
                            continue

                        yield {
                            'type': 'new_message',
                            'chat_id': chat_id,
                            'chat_name': msg_data.get('chat_name', 'Неизвестно'),
                            'text': msg_data.get('text', ''),
                            'author': msg_data.get('author', 'Неизвестно'),
                            'author_id': msg_data.get('author_id'),
                            'image_link': msg_data.get('image_link')
                        }

        except Exception as e:
            logger.error(f"Ошибка проверки сообщений: {e}")

    def _check_new_orders(self):
        """Проверяет новые заказы."""
        try:
            orders = self.account.get_orders()

            for order_data in orders:
                order_id = order_data.get('id')
                if order_id and order_id not in self._last_order_ids:
                    self._last_order_ids.add(order_id)

                    yield {
                        'type': 'new_order',
                        'order_id': order_id,
                        'description': order_data.get('description', ''),
                        'price': order_data.get('price', 0),
                        'buyer_username': order_data.get('buyer_username', 'Неизвестно')
                    }

        except Exception as e:
            logger.error(f"Ошибка проверки заказов: {e}")

    def stop(self):
        """Останавливает runner."""
        self._running = False


# ================== ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ ==================
class ProxyValidator:
    """Валидатор и парсер прокси."""

    @staticmethod
    def parse_proxy(proxy_str: str) -> Optional[dict]:
        """Парсит прокси строку."""
        if not proxy_str or not proxy_str.strip():
            return None

        proxy_str = proxy_str.strip()
        pattern = r'^(?:(?P<scheme>https?|socks[45])://)?(?:(?P<login>[^:]+):(?P<password>[^@]+)@)?(?P<ip>[\d\w\.-]+):(?P<port>\d+)$'
        match = re.match(pattern, proxy_str)

        if not match:
            return None

        scheme = match.group('scheme') or 'http'
        login = match.group('login')
        password = match.group('password')
        ip = match.group('ip')
        port = match.group('port')

        return {
            'scheme': scheme,
            'login': login,
            'password': password,
            'ip': ip,
            'port': int(port),
            'raw': proxy_str
        }

    @staticmethod
    def build_proxy_url(parsed: dict) -> str:
        """Собирает URL прокси."""
        if parsed['login'] and parsed['password']:
            return f"{parsed['scheme']}://{parsed['login']}:{parsed['password']}@{parsed['ip']}:{parsed['port']}"
        return f"{parsed['scheme']}://{parsed['ip']}:{parsed['port']}"

    @staticmethod
    def get_requests_proxy(parsed: dict) -> dict:
        """Возвращает словарь прокси для requests."""
        proxy_url = ProxyValidator.build_proxy_url(parsed)
        return {'http': proxy_url, 'https': proxy_url}


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
            "enabled": "1",
            "token": "",
            "proxy": "",
            "chat_id": ""
        },
        "Proxy": {
            "enable": "0",
            "proxy": "",
        },
        "Other": {
            "watermark": "🤖",
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
        config.set("Telegram", "token", BOT_TOKEN or "")

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
        self.account: Optional[FunPayAPI] = None
        self.runner: Optional[FunPayRunner] = None
        self.profile = None
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.is_running = False
        self.runner_task: Optional[asyncio.Task] = None

    async def init_account(self, proxy: Optional[dict] = None) -> bool:
        """Инициализирует FunPay аккаунт."""
        try:
            self.account = FunPayAPI(
                GOLDEN_KEY,
                self.config["FunPay"].get("user_agent", ""),
                proxy=proxy or {}
            )

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.account.get)

            logger.info(f"✅ Аккаунт инициализирован: {self.account.username}")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка инициализации аккаунта: {e}")
            return False

    async def get_profile(self):
        """Получает профиль пользователя."""
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
        self.runner = FunPayRunner(self.account)
        loop = asyncio.get_event_loop()

        queue = asyncio.Queue()

        def sync_listen():
            try:
                for event in self.runner.listen(
                    requests_delay=int(self.config["Other"].get("requestsDelay", 4))
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as e:
                logger.error(f"Ошибка в sync_listen: {e}")

        loop.run_in_executor(None, sync_listen)

        while self.is_running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                await self._handle_event(event)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Ошибка обработки события: {e}")

    async def _handle_event(self, event: dict):
        """Обрабатывает события FunPay."""
        event_type = event.get('type')

        if event_type == 'new_message':
            await self._handle_new_message(event)
        elif event_type == 'new_order':
            await self._handle_new_order(event)

    async def _handle_new_message(self, event: dict):
        """Обработка нового сообщения."""
        chat_id = event.get('chat_id')
        chat_name = event.get('chat_name', 'Неизвестно')
        author = event.get('author', 'Неизвестно')
        text = event.get('text', '')

        logger.info(f"💬 Новое сообщение от {author} в чате {chat_name}: {text}")

        # Автоответчик
        await self._check_auto_response(chat_id, text, chat_name)

        # Уведомление
        await self._send_notification(
            f"💬 <b>Новое сообщение</b>\n\n"
            f"👤 <b>От:</b> {author}\n"
            f"💬 <b>Чат:</b> {chat_name}\n\n"
            f"<code>{text}</code>"
        )

    async def _handle_new_order(self, event: dict):
        """Обработка нового заказа."""
        order_id = event.get('order_id')
        description = event.get('description', '')
        price = event.get('price', 0)
        buyer = event.get('buyer_username', 'Неизвестно')

        logger.info(f"🛒 Новый заказ #{order_id}: {description}")

        await self._send_notification(
            f"🛒 <b>Новый заказ!</b>\n\n"
            f"🆔 <b>ID:</b> <code>{order_id}</code>\n"
            f"👤 <b>Покупатель:</b> {buyer}\n"
            f"📦 <b>Товар:</b> {description}\n"
            f"💰 <b>Цена:</b> {price}₽"
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
        [InlineKeyboardButton(text="📱 Настроить Telegram прокси", callback_data="setup_tg_proxy")],
        [InlineKeyboardButton(text="🎮 Настроить FunPay прокси", callback_data="setup_fp_proxy")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])


# ================== ХЕНДЛЕРЫ ==================
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Старт бота."""
    chat_id = message.chat.id

    bot_instance.config.set("Telegram", "chat_id", str(chat_id))
    ConfigManager.save(bot_instance.config)

    tg_proxy = bot_instance.config.get("Telegram", "proxy", fallback="")
    fp_proxy = bot_instance.config.get("Proxy", "proxy", fallback="")

    if not tg_proxy and not fp_proxy:
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Давай настроим прокси. Отправь прокси для Telegram:\n"
            "<code>scheme://login:password@ip:port</code>\n\n"
            "Или отправь <b>/skip</b>, чтобы пропустить.",
            parse_mode=ParseMode.HTML
        )
        await state.set_state(ProxySetupStates.waiting_for_telegram_proxy)
        return

    await message.answer(
        "✅ <b>Бот запущен!</b>\n\nИспользуй меню:",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML
    )


@router.message(Command("skip"), StateFilter(ProxySetupStates.waiting_for_telegram_proxy))
async def skip_tg_proxy(message: types.Message, state: FSMContext):
    """Пропустить Telegram прокси."""
    await state.set_state(ProxySetupStates.waiting_for_funpay_proxy)
    await message.answer(
        "⏭️ Telegram прокси пропущен.\n\n"
        "Теперь отправь прокси для FunPay:\n"
        "<code>scheme://login:password@ip:port</code>\n\n"
        "Или отправь <b>/skip</b>.",
        parse_mode=ParseMode.HTML
    )


@router.message(StateFilter(ProxySetupStates.waiting_for_telegram_proxy))
async def process_tg_proxy(message: types.Message, state: FSMContext):
    """Обработка Telegram прокси."""
    proxy_str = message.text.strip()
    parsed = ProxyValidator.parse_proxy(proxy_str)

    if not parsed:
        await message.answer(
            "❌ <b>Неверный формат прокси!</b>\n\n"
            "Формат: <code>scheme://login:password@ip:port</code>",
            parse_mode=ParseMode.HTML
        )
        return

    await message.answer("🔄 Проверяю прокси...")

    try:
        proxy_url = ProxyValidator.build_proxy_url(parsed)
        session = AiohttpSession(proxy=proxy_url)
        test_bot = Bot(token=BOT_TOKEN, session=session)
        bot_info = await test_bot.get_me()
        await test_bot.session.close()

        bot_instance.config.set("Telegram", "proxy", proxy_str)
        ConfigManager.save(bot_instance.config)

        await message.answer(
            f"✅ <b>Telegram прокси настроен!</b>\n"
            f"🤖 Бот: @{bot_info.username}",
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        await message.answer(
            f"❌ <b>Ошибка прокси:</b>\n<code>{str(e)}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    await state.set_state(ProxySetupStates.waiting_for_funpay_proxy)
    await message.answer(
        "Теперь отправь прокси для FunPay:\n"
        "<code>scheme://login:password@ip:port</code>\n\n"
        "Или отправь <b>/skip</b>.",
        parse_mode=ParseMode.HTML
    )


@router.message(Command("skip"), StateFilter(ProxySetupStates.waiting_for_funpay_proxy))
async def skip_fp_proxy(message: types.Message, state: FSMContext):
    """Пропустить FunPay прокси."""
    await state.clear()
    await message.answer(
        "⏭️ FunPay прокси пропущен.\n\n"
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
        await message.answer("❌ Неверный формат прокси!", parse_mode=ParseMode.HTML)
        return

    proxy_url = ProxyValidator.build_proxy_url(parsed)
    bot_instance.config.set("Proxy", "enable", "1")
    bot_instance.config.set("Proxy", "proxy", proxy_str)
    ConfigManager.save(bot_instance.config)

    await message.answer(
        f"✅ <b>FunPay прокси настроен!</b>\n"
        f"🌐 Прокси: <code>{proxy_url}</code>",
        parse_mode=ParseMode.HTML
    )

    await state.clear()
    await message.answer(
        "✅ <b>Настройка завершена!</b>",
        reply_markup=main_menu_kb(),
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

    if not bot_instance.account:
        fp_proxy = bot_instance.config.get("Proxy", "proxy", fallback="")
        proxy = {}
        if fp_proxy:
            parsed = ProxyValidator.parse_proxy(fp_proxy)
            if parsed:
                proxy = ProxyValidator.get_requests_proxy(parsed)

        success = await bot_instance.init_account(proxy if fp_proxy else None)
        if not success:
            await callback.message.edit_text(
                "❌ Ошибка инициализации аккаунта!",
                reply_markup=back_to_menu_kb(),
                parse_mode=ParseMode.HTML
            )
            return

    profile = await bot_instance.get_profile()
    if not profile:
        await callback.message.edit_text("❌ Не удалось загрузить профиль!", reply_markup=back_to_menu_kb(), parse_mode=ParseMode.HTML)
        return

    lots_count = len(profile.lots) if profile.lots else 0

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
    tg_proxy = bot_instance.config.get("Telegram", "proxy", fallback="Не настроен")
    fp_proxy = bot_instance.config.get("Proxy", "proxy", fallback="Не настроен")

    await callback.message.edit_text(
        f"🌐 <b>Настройки прокси</b>\n\n"
        f"📱 <b>Telegram:</b>\n<code>{tg_proxy[:30] + '...' if len(tg_proxy) > 30 else tg_proxy}</code>\n\n"
        f"🎮 <b>FunPay:</b>\n<code>{fp_proxy[:30] + '...' if len(fp_proxy) > 30 else fp_proxy}</code>",
        reply_markup=proxy_settings_kb(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data == "setup_tg_proxy")
async def callback_setup_tg_proxy(callback: types.CallbackQuery, state: FSMContext):
    """Настроить Telegram прокси."""
    await state.set_state(ProxySetupStates.waiting_for_telegram_proxy)
    await callback.message.edit_text(
        "📱 <b>Настройка Telegram прокси</b>\n\n"
        "Отправь прокси:\n<code>scheme://login:password@ip:port</code>",
        parse_mode=ParseMode.HTML
    )
    await callback.answer()


@router.callback_query(F.data == "setup_fp_proxy")
async def callback_setup_fp_proxy(callback: types.CallbackQuery, state: FSMContext):
    """Настроить FunPay прокси."""
    await state.set_state(ProxySetupStates.waiting_for_funpay_proxy)
    await callback.message.edit_text(
        "🎮 <b>Настройка FunPay прокси</b>\n\n"
        "Отправь прокси:\n<code>scheme://login:password@ip:port</code>",
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

    if not bot_instance.account:
        fp_proxy = bot_instance.config.get("Proxy", "proxy", fallback="")
        proxy = {}
        if fp_proxy:
            parsed = ProxyValidator.parse_proxy(fp_proxy)
            if parsed:
                proxy = ProxyValidator.get_requests_proxy(parsed)

        success = await bot_instance.init_account(proxy if fp_proxy else None)
        if not success:
            await callback.message.edit_text(
                "❌ Ошибка инициализации аккаунта!",
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
    if bot_instance.runner:
        bot_instance.runner.stop()

    if bot_instance.runner_task and not bot_instance.runner_task.done():
        bot_instance.runner_task.cancel()
        try:
            await bot_instance.runner_task
        except asyncio.CancelledError:
            pass

    await callback.message.edit_text("⏹️ <b>Runner остановлен!</b>", reply_markup=back_to_menu_kb(), parse_mode=ParseMode.HTML)
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

    tg_proxy = bot_instance.config.get("Telegram", "proxy", fallback="")
    session = None

    if tg_proxy:
        parsed = ProxyValidator.parse_proxy(tg_proxy)
        if parsed:
            proxy_url = ProxyValidator.build_proxy_url(parsed)
            session = AiohttpSession(proxy=proxy_url)
            logger.info(f"🌐 Telegram прокси: {proxy_url}")

    bot_instance.bot = Bot(token=BOT_TOKEN, session=session or AiohttpSession())
    bot_instance.dp = Dispatcher(storage=MemoryStorage())
    bot_instance.dp.include_router(router)

    await bot_instance.bot.delete_webhook(drop_pending_updates=True)

    logger.info("✅ Бот готов к работе!")

    try:
        await bot_instance.dp.start_polling(bot_instance.bot)
    finally:
        if session:
            await session.close()
        await bot_instance.bot.session.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
