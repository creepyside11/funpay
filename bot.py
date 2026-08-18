import asyncio
import os
import logging
from typing import Optional
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from sqlalchemy import select, update, delete, func, Boolean, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import FunPayAPI
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent, LastChatMessageChangedEvent

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === ENV ===
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Установите BOT_TOKEN и DATABASE_URL")

# === БД ===
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
db = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class UserSettings(Base):
    __tablename__ = 'users'
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    golden_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notify_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base):
    __tablename__ = 'orders'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    order_id: Mapped[str] = mapped_column(String, unique=True)
    buyer: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = 'messages'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    chat_id: Mapped[int] = mapped_column(Integer)
    chat_name: Mapped[str] = mapped_column(String)
    sender: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    is_incoming: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ErrorLog(Base):
    __tablename__ = 'errors'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# === FSM ===
class ProxyState(StatesGroup):
    waiting = State()

class KeyState(StatesGroup):
    waiting = State()

class ReplyState(StatesGroup):
    waiting = State()


router = Router()

# === ГЛОБАЛЬНЫЕ ===
active_listeners = {}


# === УТИЛИТЫ ===
def make_account(key: str, proxy: Optional[str]) -> FunPayAPI.Account:
    """Создаёт аккаунт с увеличенным таймаутом 60с"""
    acc = FunPayAPI.Account(key, proxy=proxy)
    acc.requests_timeout = 60  # КРИТИЧНО: против таймаутов FunPay
    return acc


def reply_kb(chat_id: int, name: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"r_{chat_id}_{name}")]
    ])


async def log_error(uid: int, err: str):
    try:
        async with db() as s:
            s.add(ErrorLog(user_id=uid, error=err[:500]))
            await s.commit()
    except:
        pass


# === КЛАВИАТУРЫ ===
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Баланс", callback_data="bal"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="📦 Заказы", callback_data="ord"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="set")],
        [InlineKeyboardButton(text="🔧 Прокси", callback_data="pr"),
         InlineKeyboardButton(text="🔑 Golden Key", callback_data="key")],
        [InlineKeyboardButton(text="🔄 Рестарт", callback_data="rst")]
    ])


def set_kb(u: UserSettings):
    o = "✅" if u.notify_orders else "❌"
    m = "✅" if u.notify_messages else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📦 Заказы {o}", callback_data="to")],
        [InlineKeyboardButton(text=f"💬 Сообщения {m}", callback_data="tm")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back")]
    ])


# === КОМАНДЫ ===
@router.message(Command("start"))
async def start(m: types.Message):
    uid = m.from_user.id
    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()
        if not u:
            s.add(UserSettings(user_id=uid))
            await s.commit()

    await m.answer(
        "👋 **FunPay Bot**\n\n"
        "1. 🔧 Настрой прокси\n"
        "2. 🔑 Введи Golden Key\n"
        "3. 🔄 Нажми Рестарт\n\n"
        "/help - помощь",
        reply_markup=main_kb(), parse_mode="Markdown"
    )


@router.message(Command("help"))
async def help(m: types.Message):
    await m.answer(
        "**Команды:**\n"
        "/start - меню\n"
        "/status - статус\n"
        "/restart - рестарт слушателя\n"
        "/errors - последние ошибки\n\n"
        "📌 Golden Key: DevTools → Application → Cookies → `golden_key`",
        parse_mode="Markdown"
    )


@router.message(Command("status"))
async def status(m: types.Message):
    uid = m.from_user.id
    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()
        if not u:
            return await m.answer("❌ Нет данных")

        lst = "🟢 Работает" if uid in active_listeners else "🔴 Остановлен"
        y = datetime.utcnow() - timedelta(hours=24)
        ec = await s.execute(select(func.count(ErrorLog.id)).where(
            ErrorLog.user_id == uid, ErrorLog.created_at >= y))
        errors = ec.scalar() or 0

        await m.answer(
            f"**Статус:**\n"
            f"• Ключ: {'✅' if u.golden_key else '❌'}\n"
            f"• Прокси: {'✅' if u.proxy else '❌'}\n"
            f"• Слушатель: {lst}\n"
            f"• Ошибок за 24ч: {errors}",
            parse_mode="Markdown"
        )


@router.message(Command("restart"))
async def restart_cmd(m: types.Message):
    await restart_listener(m.from_user.id, m)


@router.message(Command("errors"))
async def errors_cmd(m: types.Message):
    uid = m.from_user.id
    async with db() as s:
        r = await s.execute(
            select(ErrorLog).where(ErrorLog.user_id == uid)
            .order_by(ErrorLog.created_at.desc()).limit(5))
        errs = r.scalars().all()

    if not errs:
        return await m.answer("✅ Ошибок нет")

    txt = "**Последние ошибки:**\n\n"
    for e in errs:
        txt += f"[{e.created_at:%d.%m %H:%M}] {e.error[:100]}\n"
    await m.answer(txt, parse_mode="Markdown")


# === ПРОКСИ ===
@router.callback_query(F.data == "pr")
async def pr_cb(c: types.CallbackQuery, state: FSMContext):
    await c.message.answer(
        "🔧 Введи прокси:\n"
        "`http://user:pass@ip:port`\n"
        "`socks5://user:pass@ip:port`\n\n"
        "/skip - пропустить",
        parse_mode="Markdown"
    )
    await state.set_state(ProxyState.waiting)


@router.message(ProxyState.waiting)
async def pr_msg(m: types.Message, state: FSMContext):
    uid = m.from_user.id
    proxy = None if m.text.lower() == "/skip" else m.text.strip()

    async with db() as s:
        await s.execute(update(UserSettings).where(UserSettings.user_id == uid).values(proxy=proxy))
        await s.commit()

    await state.clear()
    await m.answer(f"✅ Прокси: {proxy or 'пропущен'}", reply_markup=main_kb())


# === GOLDEN KEY ===
@router.callback_query(F.data == "key")
async def key_cb(c: types.CallbackQuery, state: FSMContext):
    await c.message.answer("🔑 Введи Golden Key (DevTools → Cookies → `golden_key`):")
    await state.set_state(KeyState.waiting)


@router.message(KeyState.waiting)
async def key_msg(m: types.Message, state: FSMContext):
    uid = m.from_user.id
    key = m.text.strip()

    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()

        await m.answer("⏳ Проверяю ключ...")
        try:
            acc = make_account(key, u.proxy if u else None)
            acc.get()
            user = acc.username

            await s.execute(update(UserSettings).where(UserSettings.user_id == uid)
                            .values(golden_key=key, is_active=True))
            await s.commit()
            await state.clear()

            await m.answer(
                f"✅ **Ключ принят!**\n"
                f"Пользователь: `{user}`\n\n"
                f"Нажми 🔄 Рестарт для запуска слушателя",
                reply_markup=main_kb(), parse_mode="Markdown"
            )
        except Exception as e:
            await log_error(uid, f"Key check: {e}")
            await m.answer(f"❌ Ошибка: {e}")


# === БАЛАНС ===
@router.callback_query(F.data == "bal")
async def bal_cb(c: types.CallbackQuery):
    uid = c.from_user.id
    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()

    if not u or not u.golden_key:
        return await c.answer("❌ Сначала настрой ключ!", show_alert=True)

    await c.answer("⏳")
    try:
        acc = make_account(u.golden_key, u.proxy)
        acc.get()
        bal = acc.balance
        txt = "**💰 Баланс:**\n\n"
        for cur, amt in bal.items():
            txt += f"• {cur}: {amt}\n"
        await c.message.answer(txt, parse_mode="Markdown")
    except Exception as e:
        await log_error(uid, f"Balance: {e}")
        await c.message.answer(f"❌ Ошибка: {e}")


# === СТАТИСТИКА ===
@router.callback_query(F.data == "stats")
async def stats_cb(c: types.CallbackQuery):
    uid = c.from_user.id
    today = datetime.utcnow().date()
    week = today - timedelta(days=7)
    month = today - timedelta(days=30)

    async with db() as s:
        t = await s.execute(select(func.count(Order.id), func.sum(Order.price))
                            .where(Order.user_id == uid, func.date(Order.created_at) == today))
        tc, ts = t.one()
        w = await s.execute(select(func.count(Order.id), func.sum(Order.price))
                            .where(Order.user_id == uid, func.date(Order.created_at) >= week))
        wc, ws = w.one()
        mo = await s.execute(select(func.count(Order.id), func.sum(Order.price))
                             .where(Order.user_id == uid, func.date(Order.created_at) >= month))
        mc, ms = mo.one()
        all_ = await s.execute(select(func.count(Order.id), func.sum(Order.price))
                               .where(Order.user_id == uid))
        ac, as_ = all_.one()

    await c.message.answer(
        f"**📊 Статистика:**\n\n"
        f"• Сегодня: {ts or 0}₽ ({tc or 0} зак.)\n"
        f"• Неделя: {ws or 0}₽ ({wc or 0} зак.)\n"
        f"• Месяц: {ms or 0}₽ ({mc or 0} зак.)\n"
        f"• Всего: {as_ or 0}₽ ({ac or 0} зак.)",
        parse_mode="Markdown"
    )


# === ЗАКАЗЫ ===
@router.callback_query(F.data == "ord")
async def ord_cb(c: types.CallbackQuery):
    uid = c.from_user.id
    async with db() as s:
        r = await s.execute(select(Order).where(Order.user_id == uid)
                            .order_by(Order.created_at.desc()).limit(10))
        orders = r.scalars().all()

    if not orders:
        return await c.message.answer("📭 Заказов нет")

    txt = "**📦 Последние заказы:**\n\n"
    for o in orders:
        txt += f"• `#{o.order_id}` - {o.buyer} - {o.price}₽\n  _{o.description[:40]}_\n\n"
    await c.message.answer(txt, parse_mode="Markdown")


# === НАСТРОЙКИ ===
@router.callback_query(F.data == "set")
async def set_cb(c: types.CallbackQuery):
    uid = c.from_user.id
    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()
    await c.message.edit_text("**⚙️ Настройки:**", reply_markup=set_kb(u), parse_mode="Markdown")


@router.callback_query(F.data == "to")
async def to_cb(c: types.CallbackQuery):
    uid = c.from_user.id
    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()
        u.notify_orders = not u.notify_orders
        await s.commit()
    await c.answer(f"✅ {'Вкл' if u.notify_orders else 'Выкл'}")
    await set_cb(c)


@router.callback_query(F.data == "tm")
async def tm_cb(c: types.CallbackQuery):
    uid = c.from_user.id
    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()
        u.notify_messages = not u.notify_messages
        await s.commit()
    await c.answer(f"✅ {'Вкл' if u.notify_messages else 'Выкл'}")
    await set_cb(c)


@router.callback_query(F.data == "back")
async def back_cb(c: types.CallbackQuery):
    await c.message.edit_text("Главное меню:", reply_markup=main_kb())


# === ОТВЕТ НА СООБЩЕНИЕ ===
@router.callback_query(F.data.startswith("r_"))
async def r_cb(c: types.CallbackQuery, state: FSMContext):
    parts = c.data.split("_")
    chat_id = parts[1]
    name = "_".join(parts[2:])
    await state.update_data(cid=chat_id, cname=name)
    await state.set_state(ReplyState.waiting)
    await c.message.answer(f"💬 Ответ в **{name}** (или /cancel):", parse_mode="Markdown")


@router.message(ReplyState.waiting)
async def r_msg(m: types.Message, state: FSMContext):
    if m.text == "/cancel":
        await state.clear()
        return await m.answer("❌ Отменено")

    uid = m.from_user.id
    data = await state.get_data()

    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()
        if not u or not u.golden_key:
            return await m.answer("❌ Настрой ключ")

        try:
            acc = make_account(u.golden_key, u.proxy)
            acc.get()
            acc.send_message(int(data['cid']), m.text, data['cname'])

            s.add(Message(user_id=uid, chat_id=int(data['cid']), chat_name=data['cname'],
                          sender="me", text=m.text, is_incoming=False))
            await s.commit()

            await m.answer(f"✅ Отправлено в {data['cname']}")
        except Exception as e:
            await log_error(uid, f"Reply: {e}")
            await m.answer(f"❌ Ошибка: {e}")

    await state.clear()


# === РЕСТАРТ СЛУШАТЕЛЯ ===
@router.callback_query(F.data == "rst")
async def rst_cb(c: types.CallbackQuery):
    await restart_listener(c.from_user.id, c.message)


async def restart_listener(uid: int, msg):
    # Остановить старый
    if uid in active_listeners:
        active_listeners[uid].cancel()
        del active_listeners[uid]
        await msg.answer("🛑 Старый слушатель остановлен")

    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()
        if not u or not u.golden_key:
            return await msg.answer("❌ Сначала настрой Golden Key")

        try:
            acc = make_account(u.golden_key, u.proxy)
            acc.get()
        except Exception as e:
            await log_error(uid, f"Listener start: {e}")
            return await msg.answer(f"❌ Ошибка запуска: {e}")

    # Запустить новый
    bot = Bot(token=BOT_TOKEN)
    task = asyncio.create_task(listener_loop(uid, bot))
    active_listeners[uid] = task
    await msg.answer("✅ Слушатель запущен! 🚀")


# === ОСНОВНОЙ СЛУШАТЕЛЬ FUNPAY ===
async def listener_loop(uid: int, bot: Bot):
    """Цикл слушателя с автоматическим рестартом при ошибках"""
    restarts = 0
    max_restarts = 20

    while restarts < max_restarts:
        try:
            async with db() as s:
                r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
                u = r.scalar_one_or_none()
                if not u or not u.golden_key:
                    return

            acc = make_account(u.golden_key, u.proxy)
            acc.get()
            runner = FunPayAPI.Runner(acc)
            logger.info(f"Слушатель запущен для {uid}")

            for event in runner.listen(requests_delay=5):
                if uid not in active_listeners:
                    return

                try:
                    if isinstance(event, NewOrderEvent):
                        await handle_order(event, uid, acc, bot)
                    elif isinstance(event, NewMessageEvent):
                        await handle_msg(event, uid, acc, bot)
                    elif isinstance(event, LastChatMessageChangedEvent):
                        await handle_last_msg(event, uid, bot)
                except Exception as e:
                    logger.error(f"Event error {uid}: {e}")
                    await log_error(uid, f"Event: {e}")
                    continue

        except asyncio.CancelledError:
            logger.info(f"Слушатель {uid} остановлен вручную")
            return
        except Exception as e:
            restarts += 1
            delay = min(30 * restarts, 300)
            logger.error(f"Ошибка слушателя {uid} (рестарт {restarts}): {e}")
            await log_error(uid, f"Listener crash: {e}")
            try:
                await bot.send_message(uid, f"⚠️ Слушатель упал, рестарт #{restarts} через {delay}с\n`{e}`",
                                        parse_mode="Markdown")
            except:
                pass
            await asyncio.sleep(delay)

    logger.error(f"Слушатель {uid} остановлен после {max_restarts} рестартов")
    try:
        await bot.send_message(uid, "❌ Слушатель остановлен после максимума рестартов. Нажми /restart")
    except:
        pass


async def handle_order(ev: NewOrderEvent, uid: int, acc: FunPayAPI.Account, bot: Bot):
    o = ev.order

    # Сохраняем заказ
    async with db() as s:
        r = await s.execute(select(Order).where(Order.order_id == o.id))
        if r.scalar_one_or_none():
            return  # уже обработан
        s.add(Order(user_id=uid, order_id=o.id, buyer=o.buyer_username,
                    description=o.description, price=o.price))
        await s.commit()

        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()

        if u and u.notify_orders:
            txt = (f"📦 **Новый заказ!**\n"
                   f"• ID: `{o.id}`\n"
                   f"• Покупатель: {o.buyer_username}\n"
                   f"• {o.description[:100]}\n"
                   f"• {o.price}₽")
            try:
                await bot.send_message(uid, txt, parse_mode="Markdown",
                                        reply_markup=reply_kb(o.buyer_username, o.buyer_username))
            except Exception as e:
                await log_error(uid, f"Order notify: {e}")


async def handle_msg(ev: NewMessageEvent, uid: int, acc: FunPayAPI.Account, bot: Bot):
    if ev.message.author_id == acc.id:
        return

    m = ev.message
    async with db() as s:
        # Сохраняем
        s.add(Message(user_id=uid, chat_id=m.chat_id, chat_name=m.chat_name,
                      sender=m.author, text=str(m), is_incoming=True))

        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()

        if u and u.notify_messages:
            txt = (f"💬 **Новое сообщение!**\n"
                   f"• От: {m.author}\n"
                   f"• Чат: {m.chat_name}\n"
                   f"• {m.text[:200]}")
            try:
                await bot.send_message(uid, txt, parse_mode="Markdown",
                                        reply_markup=reply_kb(m.chat_id, m.chat_name))
            except Exception as e:
                await log_error(uid, f"Msg notify: {e}")

        await s.commit()


async def handle_last_msg(ev: LastChatMessageChangedEvent, uid: int, bot: Bot):
    if not ev.chat.unread or ev.chat.last_by_bot:
        return

    async with db() as s:
        r = await s.execute(select(UserSettings).where(UserSettings.user_id == uid))
        u = r.scalar_one_or_none()

        if u and u.notify_messages:
            txt = f"💬 От {ev.chat.name}: {str(ev.chat)[:200]}"
            try:
                await bot.send_message(uid, txt, reply_markup=reply_kb(ev.chat.id, ev.chat.name))
            except:
                pass


# === ЗАПУСК АКТИВНЫХ СЛУШАТЕЛЕЙ ===
async def start_listeners(bot: Bot):
    async with db() as s:
        r = await s.execute(select(UserSettings).where(
            UserSettings.golden_key.isnot(None), UserSettings.is_active == True))
        users = r.scalars().all()

    for u in users:
        task = asyncio.create_task(listener_loop(u.user_id, bot))
        active_listeners[u.user_id] = task
        logger.info(f"Автозапуск слушателя для {u.user_id}")


# === MAIN ===
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await start_listeners(bot)
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
