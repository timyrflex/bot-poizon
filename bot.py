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
DELIVERY_USD_PER_KG = 8  # стоимость авто-доставки: 8$ за кг


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
            photo_id TEXT,
            price_cny TEXT,
            price_rub TEXT,
            delivery_type TEXT,
            weight_kg TEXT,
            delivery_rub TEXT,
            total_rub TEXT,
            size TEXT,
            contact TEXT,
            comment TEXT,
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()

    # Миграция: добавляем недостающие колонки, если таблица создана более старой версией бота
    for column in ("photo_id", "price_cny", "price_rub", "delivery_type", "weight_kg", "delivery_rub", "total_rub"):
        try:
            cur.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже существует

    conn.close()


def get_setting(key: str, default: str = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def save_order(user_id, username, full_name, product, photo_id, price_cny, price_rub,
                delivery_type, weight_kg, delivery_rub, total_rub, size, contact, comment):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders (
            user_id, username, full_name, product, photo_id, price_cny, price_rub,
            delivery_type, weight_kg, delivery_rub, total_rub,
            size, contact, comment, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            full_name,
            product,
            photo_id,
            price_cny,
            price_rub,
            delivery_type,
            weight_kg,
            delivery_rub,
            total_rub,
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
    price_cny = State()
    delivery_type = State()
    weight_kg = State()
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


def delivery_type_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Авто (быстро)")],
            [KeyboardButton(text="✈️ Авиа")],
            [KeyboardButton(text="❌ Отмена")],
        ],
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

    rate = get_setting("yuan_rate")
    rate_line = f"\n💱 Курс юаня: 1 CNY = {rate} ₽\n" if rate else ""

    await message.answer(
        "👋 Привет! Это бот для заказа вещей с Poizon.\n"
        f"{rate_line}\n"
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
        "2️⃣ Укажи товар (ссылкой или фото), цену в юанях, способ доставки, размер и контакты\n"
        "3️⃣ Подтверди заявку — она уйдёт менеджеру\n"
        "4️⃣ С тобой свяжутся для уточнения деталей и оплаты\n\n"
        "Команды:\n"
        "/order — оформить новый заказ\n"
        "/myorders — посмотреть свои заявки\n"
        "/rate — курс юаня\n"
        "/usdrate — курс доллара (для расчёта доставки)\n"
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


# ---------- HANDLERS: RATES ----------

@router.message(Command("setrate"))
async def set_rate(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи курс после команды, например:\n/setrate 12.85")
        return
    rate_text = parts[1].replace(",", ".").strip()
    try:
        rate_value = float(rate_text)
    except ValueError:
        await message.answer("Не понял курс. Пример: /setrate 12.85")
        return
    set_setting("yuan_rate", str(rate_value))
    await message.answer(f"✅ Курс юаня обновлён: 1 CNY = {rate_value} ₽")


@router.message(Command("rate"))
async def show_rate(message: Message):
    rate = get_setting("yuan_rate")
    if rate is None:
        await message.answer("Курс юаня пока не установлен.")
        return
    await message.answer(f"💱 Текущий курс: 1 CNY = {rate} ₽")


@router.message(Command("setusdrate"))
async def set_usd_rate(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи курс после команды, например:\n/setusdrate 95.40")
        return
    rate_text = parts[1].replace(",", ".").strip()
    try:
        rate_value = float(rate_text)
    except ValueError:
        await message.answer("Не понял курс. Пример: /setusdrate 95.40")
        return
    set_setting("usd_rate", str(rate_value))
    await message.answer(f"✅ Курс доллара обновлён: 1 USD = {rate_value} ₽")


@router.message(Command("usdrate"))
async def show_usd_rate(message: Message):
    rate = get_setting("usd_rate")
    if rate is None:
        await message.answer("Курс доллара пока не установлен.")
        return
    await message.answer(f"💵 Текущий курс доллара: 1 USD = {rate} ₽")


# ---------- HANDLERS: ORDER FORM ----------

@router.message(Command("order"))
@router.message(F.text == "🛒 Оформить заказ")
async def start_order(message: Message, state: FSMContext):
    rate = get_setting("yuan_rate")
    if rate:
        await message.answer(f"💱 Курс юаня сейчас: 1 CNY = {rate} ₽")

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
        "Отправь ссылку на товар с Poizon.\n"
        "Если ссылки нет — просто пришли фото товара 📸",
        reply_markup=cancel_kb(),
    )


@router.message(OrderForm.product, F.photo)
async def process_product_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id  # берём фото в максимальном качестве
    caption = message.caption or ""
    await state.update_data(product=f"[Фото] {caption}".strip(), photo_id=photo_id)
    await state.set_state(OrderForm.price_cny)
    await message.answer(
        "Фото получено ✅\n\n"
        "Укажи цену товара в юанях (просто число, например: 350).\n"
        "Если не знаешь цену — нажми «Пропустить».",
        reply_markup=skip_or_cancel_kb(),
    )


@router.message(OrderForm.product, F.text)
async def process_product_text(message: Message, state: FSMContext):
    await state.update_data(product=message.text, photo_id="")
    await state.set_state(OrderForm.price_cny)
    await message.answer(
        "Укажи цену товара в юанях (просто число, например: 350).\n"
        "Если не знаешь цену — нажми «Пропустить».",
        reply_markup=skip_or_cancel_kb(),
    )


@router.message(OrderForm.product)
async def process_product_invalid(message: Message, state: FSMContext):
    await message.answer("Пришли ссылку текстом или фото товара 📸")


@router.message(OrderForm.price_cny)
async def process_price(message: Message, state: FSMContext):
    if message.text == "Пропустить":
        await state.update_data(price_cny="", price_rub="")
    else:
        price_text = (message.text or "").replace(",", ".").strip()
        try:
            price_value = float(price_text)
        except ValueError:
            await message.answer(
                "Не понял цену. Введи число (например 350) или нажми «Пропустить»."
            )
            return

        rate = get_setting("yuan_rate")
        if rate:
            price_rub = round(price_value * float(rate), 2)
            await state.update_data(price_cny=str(price_value), price_rub=str(price_rub))
            await message.answer(f"💰 Стоимость товара: {price_rub} ₽")
        else:
            await state.update_data(price_cny=str(price_value), price_rub="")
            await message.answer("Курс юаня пока не задан менеджером, посчитаем позже.")

    await state.set_state(OrderForm.delivery_type)
    await message.answer(
        "Выбери способ доставки:\n\n"
        "🚀 Авто (быстро) — рассчитывается автоматически: 8$ за кг\n"
        "✈️ Авиа — стоимость уточняется у менеджера отдельно",
        reply_markup=delivery_type_kb(),
    )


@router.message(OrderForm.delivery_type, F.text == "🚀 Авто (быстро)")
async def process_delivery_auto(message: Message, state: FSMContext):
    await state.update_data(delivery_type="Авто (быстро)")
    await state.set_state(OrderForm.weight_kg)
    await message.answer(
        "Укажи примерный вес посылки в кг (например: 1.5).",
        reply_markup=cancel_kb(),
    )


@router.message(OrderForm.delivery_type, F.text == "✈️ Авиа")
async def process_delivery_avia(message: Message, state: FSMContext):
    await state.update_data(
        delivery_type="Авиа",
        weight_kg="",
        delivery_rub="",
    )
    await message.answer("✈️ Стоимость авиадоставки менеджер посчитает и уточнит отдельно.")
    await proceed_to_size(message, state)


@router.message(OrderForm.delivery_type)
async def process_delivery_invalid(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, выбери способ доставки кнопкой ниже 👇")


@router.message(OrderForm.weight_kg)
async def process_weight(message: Message, state: FSMContext):
    weight_text = (message.text or "").replace(",", ".").strip()
    try:
        weight_value = float(weight_text)
        if weight_value <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Не понял вес. Введи число больше нуля, например: 1.5")
        return

    usd_rate = get_setting("usd_rate")
    delivery_usd = round(weight_value * DELIVERY_USD_PER_KG, 2)

    if usd_rate:
        delivery_rub = round(delivery_usd * float(usd_rate), 2)
        await state.update_data(weight_kg=str(weight_value), delivery_rub=str(delivery_rub))
        await message.answer(
            f"🚚 Доставка: {weight_value} кг × {DELIVERY_USD_PER_KG}$ = "
            f"{delivery_usd}$ (≈ {delivery_rub} ₽)"
        )
    else:
        await state.update_data(weight_kg=str(weight_value), delivery_rub="")
        await message.answer(
            f"🚚 Доставка: {weight_value} кг × {DELIVERY_USD_PER_KG}$ = {delivery_usd}$\n"
            "Курс доллара пока не задан менеджером — сумму в рублях посчитаем позже."
        )

    await proceed_to_size(message, state)


async def proceed_to_size(message: Message, state: FSMContext):
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


def build_price_lines(data: dict) -> tuple[str, str]:
    """Возвращает (текст с расшифровкой цены/доставки, итоговая сумма в рублях или '')"""
    lines = []
    total_rub = None

    if data.get("price_cny"):
        line = f"💴 Цена товара: {data['price_cny']} CNY"
        if data.get("price_rub"):
            line += f" (≈ {data['price_rub']} ₽)"
            total_rub = float(data["price_rub"])
        lines.append(line)

    delivery_type = data.get("delivery_type", "")
    if delivery_type == "Авто (быстро)":
        line = f"🚚 Доставка: Авто, {data.get('weight_kg', '?')} кг"
        if data.get("delivery_rub"):
            line += f" (≈ {data['delivery_rub']} ₽)"
            total_rub = (total_rub or 0) + float(data["delivery_rub"])
        else:
            line += " (сумма в рублях уточняется)"
        lines.append(line)
    elif delivery_type == "Авиа":
        lines.append("🚚 Доставка: Авиа — стоимость уточняется у менеджера")

    text = "\n".join(lines) + ("\n" if lines else "")
    total_line = f"💰 Итого (без учёта неизвестных пунктов): {round(total_rub, 2)} ₽\n" if total_rub else ""
    return text, total_line


@router.message(OrderForm.comment)
async def process_comment(message: Message, state: FSMContext):
    comment = "" if message.text == "Пропустить" else (message.text or "")
    await state.update_data(comment=comment)

    data = await state.get_data()
    price_lines, total_line = build_price_lines(data)

    product_line = data["product"]
    if data.get("photo_id"):
        product_line += " (см. фото выше)"

    summary = (
        "Проверь данные заказа:\n\n"
        f"👤 Имя: {data['full_name']}\n"
        f"🛍 Товар: {product_line}\n"
        f"{price_lines}"
        f"{total_line}"
        f"📏 Размер: {data['size']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"💬 Комментарий: {data['comment'] or '—'}\n\n"
        "Всё верно?"
    )

    if data.get("photo_id"):
        await message.answer_photo(photo=data["photo_id"], caption=summary, reply_markup=confirm_kb())
    else:
        await message.answer(summary, reply_markup=confirm_kb())

    await state.set_state(OrderForm.confirm)


@router.message(OrderForm.confirm, F.text == "✅ Подтвердить")
async def confirm_order(message: Message, state: FSMContext):
    data = await state.get_data()

    price_lines, total_line = build_price_lines(data)
    total_rub_value = ""
    if total_line:
        try:
            total_rub_value = total_line.split(":")[1].split("₽")[0].strip()
        except Exception:
            total_rub_value = ""

    order_id = save_order(
        user_id=message.from_user.id,
        username=message.from_user.username or "—",
        full_name=data["full_name"],
        product=data["product"],
        photo_id=data.get("photo_id", ""),
        price_cny=data.get("price_cny", ""),
        price_rub=data.get("price_rub", ""),
        delivery_type=data.get("delivery_type", ""),
        weight_kg=data.get("weight_kg", ""),
        delivery_rub=data.get("delivery_rub", ""),
        total_rub=total_rub_value,
        size=data["size"],
        contact=data["contact"],
        comment=data["comment"],
    )
    await state.clear()

    await message.answer(
        f"✅ Заявка №{order_id} принята!\n"
        "Менеджер свяжется с тобой в ближайшее время для уточнения деталей и оплаты.",
        reply_markup=main_menu_kb(),
    )

    admin_text = (
        f"🆕 Новая заявка №{order_id}\n\n"
        f"👤 Имя: {data['full_name']}\n"
        f"🔗 Telegram: @{message.from_user.username or '—'} (id: {message.from_user.id})\n"
        f"🛍 Товар: {data['product']}\n"
        f"{price_lines}"
        f"{total_line}"
        f"📏 Размер: {data['size']}\n"
        f"📞 Контакт: {data['contact']}\n"
        f"💬 Комментарий: {data['comment'] or '—'}"
    )

    for admin_id in config.ADMIN_IDS:
        try:
            if data.get("photo_id"):
                await bot.send_photo(
                    admin_id,
                    photo=data["photo_id"],
                    caption=admin_text,
                    reply_markup=admin_order_kb(order_id),
                )
            else:
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


# ---------- HANDLERS: ADMIN — ALL ORDERS ----------

@router.message(Command("allorders"))
async def all_orders(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_name, product, total_rub, size, contact, status, created_at "
        "FROM orders ORDER BY id DESC LIMIT 30"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await message.answer("Заказов пока нет.")
        return

    status_labels = {
        "new": "🆕 Новая",
        "accepted": "✅ Принята",
        "rejected": "❌ Отклонена",
    }

    lines = ["📋 Последние заказы (макс. 30):\n"]
    for order_id, full_name, product, total_rub, size, contact, status, created_at in rows:
        label = status_labels.get(status, status)
        product_short = product if len(product) <= 30 else product[:27] + "..."
        total_part = f" • {total_rub}₽" if total_rub else ""
        lines.append(
            f"№{order_id} • {full_name} • {product_short}{total_part} • {size} • {contact} • {label} • {created_at}"
        )

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


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
    if callback.message.text:
        await callback.message.edit_text(callback.message.text + "\n\n✅ Принята")
    else:
        await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n✅ Принята")
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
    if callback.message.text:
        await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонена")
    else:
        await callback.message.edit_caption(caption=(callback.message.caption or "") + "\n\n❌ Отклонена")
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
