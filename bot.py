import asyncio
import logging
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

DB_PATH = "orders.db"


# ---------- DATABASE ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            product TEXT,
            size TEXT,
            contact TEXT,
            comment TEXT,
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_order(user_id, username, full_name, product, size, contact, comment):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders (user_id, username, full_name, product, size, contact, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            full_name,
            product,
            size,
            contact,
            comment,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id


def update_order_status(order_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()


def get_order(order_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_user_orders(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, product, size, status, created_at FROM orders WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------- STATES ----------

class OrderForm(StatesGroup):
    full_name = State()
    product = State()
    size = State()
    contact = State()
    comment = State()
    confirm = State()


# ---------- KEYBOARDS ----------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Оформить заказ")],
            [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def skip_or_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить")], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Подтвердить")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{order_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}"),
            ]
        ]
    )


# ---------- HANDLERS: BASIC ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Это бот для заказа вещей с Poizon.\n\n"
        "Заполни короткую форму — я передам заявку менеджеру, "
        "и с тобой свяжутся для уточнения деталей и оплаты.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    await message.answer(
        "Как это работает:\n"
        "1️⃣ Нажми «Оформить заказ»\n"
        "2️⃣ Укажи товар (ссылку на Poizon или описание), размер и контакты\n"
        "3️⃣ Подтверди заявку — она уйдёт менеджеру\n"
        "4️⃣ С тобой свяжутся для расчёта стоимости и оплаты\n\n"
        "Команды:\n"
        "/order — оформить новый заказ\n"
        "/myorders — посмотреть свои заявки\n"
        "/cancel — отменить текущее оформление"
    )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять 🙂", reply_markup=main_menu_kb())
        return
    await state.clear()
    await message.answer("Оформление отменено.", reply_markup=main_menu_kb())


# ---------- HANDLERS: ORDER FORM ----------

@router.message(Command("order"))
@router.message(F.text == "🛒 Оформить заказ")
async def start_order(message: Message, state: FSMContext):
    await state.set_state(OrderForm.full_name)
    await message.answer(
        "Как к тебе обращаться? Напиши имя и фамилию.",
        reply_markup=cancel_kb(),
    )


@router.message(OrderForm.full_name)
async def process_full_name(message: Message, state: FSMContext):
    if not message.text or len(message.text) < 2:
        await message.answer("Пожалуйста, введи корректное имя.")
        return
    await state.update_data(full_name=message.text)
    await state.set_state(OrderForm.product)
    await message.answer(
        "Отправь ссылку на товар с Poizon (или его название/описание, если ссылки нет).",
        reply_markup=cancel_kb(),
    )


@router.message(OrderForm.product)
async def process_product(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Пришли текстом ссылку или описание товара.")
        return
    await state.update_data(product=message.text)
    await state.set_state(OrderForm.size)
    await message.answer(
        "Укажи нужный размер (например: EU 42, US 9, L, или размер в см).",
        reply_markup=cancel_kb(),
    )


@router.message(OrderForm.size)
async def process_size(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Укажи размер текстом.")
        return
    await state.update_data(size=message.text)
    await state.set_state(OrderForm.contact)
    await message.answer(
        "Оставь контакт для связи: номер телефона или @username.",
        reply_markup=cancel_kb(),
    )


@router.message(OrderForm.contact)
async def process_contact(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Укажи контакт текстом.")
        return
    await state.update_data(contact=message.text)
    await state.set_state(OrderForm.comment)
    await message.answer(
        "Есть дополнительные пожелания или комментарий к заказу? "
        "Если нет — нажми «Пропустить».",
        reply_markup=skip_or_cancel_kb(),
    )


@router.message(OrderForm.comment)
async def process_comment(message: Message, state: FSMContext):
    comment = "" if message.text == "Пропустить" else (message.text or "")
    await state.update_data(comment=comment)

    data = await state.get_data()
    summary = (
        "Проверь данные заказа:\n\n"
        f"👤 Имя: {data['full_name']}\n"
        f"🛍 Товар: {data['product']}\n"
        f"📏 Размер: {data['size']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"💬 Комментарий: {data['comment'] or '—'}\n\n"
        "Всё верно?"
    )
    await state.set_state(OrderForm.confirm)
    await message.answer(summary, reply_markup=confirm_kb())


@router.message(OrderForm.confirm, F.text == "✅ Подтвердить")
async def confirm_order(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = save_order(
        user_id=message.from_user.id,
        username=message.from_user.username or "—",
        full_name=data["full_name"],
        product=data["product"],
        size=data["size"],
        contact=data["contact"],
        comment=data["comment"],
    )
    await state.clear()

    await message.answer(
        f"✅ Заявка №{order_id} принята!\n"
        "Менеджер свяжется с тобой в ближайшее время для уточнения стоимости и оплаты.",
        reply_markup=main_menu_kb(),
    )

    admin_text = (
        f"🆕 Новая заявка №{order_id}\n\n"
        f"👤 Имя: {data['full_name']}\n"
        f"🔗 Telegram: @{message.from_user.username or '—'} (id: {message.from_user.id})\n"
        f"🛍 Товар: {data['product']}\n"
        f"📏 Размер: {data['size']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"💬 Комментарий: {data['comment'] or '—'}"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=admin_order_kb(order_id))
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление админу {admin_id}: {e}")


# ---------- HANDLERS: MY ORDERS ----------

@router.message(Command("myorders"))
@router.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message):
    orders = get_user_orders(message.from_user.id)
    if not orders:
        await message.answer("У тебя пока нет заявок. Нажми «Оформить заказ», чтобы создать первую.")
        return

    status_labels = {
        "new": "🆕 Новая",
        "accepted": "✅ Принята",
        "rejected": "❌ Отклонена",
    }

    lines = ["Твои заявки:\n"]
    for order_id, product, size, status, created_at in orders:
        label = status_labels.get(status, status)
        product_short = product if len(product) <= 40 else product[:37] + "..."
        lines.append(f"№{order_id} • {product_short} • размер {size} • {label} • {created_at}")

    await message.answer("\n".join(lines))


# ---------- HANDLERS: ADMIN CALLBACKS ----------

@router.callback_query(F.data.startswith("accept_"))
async def admin_accept(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    update_order_status(order_id, "accepted")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"✅ Твоя заявка №{order_id} принята в работу!")
        except Exception:
            pass
    await callback.message.edit_text(callback.message.text + "\n\n✅ Принята")
    await callback.answer("Заявка принята")


@router.callback_query(F.data.startswith("reject_"))
async def admin_reject(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    update_order_status(order_id, "rejected")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"❌ Твоя заявка №{order_id} отклонена. Свяжись с менеджером для уточнения деталей.")
        except Exception:
            pass
    await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонена")
    await callback.answer("Заявка отклонена")


# ---------- FALLBACK ----------

@router.message()
async def fallback(message: Message):
    await message.answer(
        "Не совсем понял 🙂 Используй меню ниже или команду /order, чтобы оформить заказ.",
        reply_markup=main_menu_kb(),
    )


# ---------- ENTRYPOINT ----------

async def main():
    init_db()
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
