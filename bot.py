import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta

import requests

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
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

DB_PATH = "usada.db"
YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"

FREQUENCY_LABELS = {
    "none": "Разовый заказ",
    "weekly": "Каждую неделю",
    "monthly": "Каждый месяц",
}


# ---------- DATABASE ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            name TEXT,
            unit TEXT,
            price_rub REAL,
            price_tier2 REAL,
            price_tier10 REAL,
            active INTEGER DEFAULT 1
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            items_json TEXT,
            total_rub REAL,
            address TEXT,
            phone TEXT,
            payment_method TEXT,
            payment_id TEXT,
            payment_status TEXT DEFAULT 'not_required',
            frequency TEXT DEFAULT 'none',
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            items_json TEXT,
            total_rub REAL,
            address TEXT,
            phone TEXT,
            payment_method TEXT,
            frequency TEXT,
            next_run TEXT,
            active INTEGER DEFAULT 1
        )
        """
    )

    conn.commit()

    for column in ("price_tier2", "price_tier10"):
        try:
            cur.execute(f"ALTER TABLE products ADD COLUMN {column} REAL")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже существует

    conn.close()


def add_product(category, name, unit, price_rub, price_tier2=None, price_tier10=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO products (category, name, unit, price_rub, price_tier2, price_tier10) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (category, name, unit, price_rub, price_tier2, price_tier10),
    )
    conn.commit()
    product_id = cur.lastrowid
    conn.close()
    return product_id


def get_categories():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT category FROM products WHERE active = 1 ORDER BY category")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def get_products_by_category(category):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, unit, price_rub, price_tier2, price_tier10 "
        "FROM products WHERE category = ? AND active = 1 ORDER BY id",
        (category,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_products():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, category, name, unit, price_rub, price_tier2, price_tier10, active "
        "FROM products ORDER BY category, id"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_product(product_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, category, name, unit, price_rub, price_tier2, price_tier10 "
        "FROM products WHERE id = ?",
        (product_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def deactivate_product(product_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE products SET active = 0 WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()


def save_order(user_id, username, full_name, items, total_rub, address, phone,
                payment_method, payment_status, frequency):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders (
            user_id, username, full_name, items_json, total_rub, address, phone,
            payment_method, payment_status, frequency, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, username, full_name, json.dumps(items, ensure_ascii=False), total_rub,
            address, phone, payment_method, payment_status, frequency,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    order_id = cur.lastrowid
    conn.close()
    return order_id


def update_order_status(order_id, status):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()


def update_order_payment(order_id, payment_id=None, payment_status=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if payment_id is not None:
        cur.execute("UPDATE orders SET payment_id = ? WHERE id = ?", (payment_id, order_id))
    if payment_status is not None:
        cur.execute("UPDATE orders SET payment_status = ? WHERE id = ?", (payment_status, order_id))
    conn.commit()
    conn.close()


def get_order(order_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_user_orders(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, items_json, total_rub, status, created_at FROM orders WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def save_subscription(user_id, username, full_name, items, total_rub, address, phone,
                       payment_method, frequency):
    days = 7 if frequency == "weekly" else 30
    next_run = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO subscriptions (
            user_id, username, full_name, items_json, total_rub, address, phone,
            payment_method, frequency, next_run
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, username, full_name, json.dumps(items, ensure_ascii=False), total_rub,
            address, phone, payment_method, frequency, next_run,
        ),
    )
    conn.commit()
    sub_id = cur.lastrowid
    conn.close()
    return sub_id


def get_due_subscriptions():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM subscriptions WHERE active = 1 AND next_run <= ?", (today,))
    rows = cur.fetchall()
    conn.close()
    return rows


def advance_subscription(sub_id, frequency):
    days = 7 if frequency == "weekly" else 30
    next_run = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE subscriptions SET next_run = ? WHERE id = ?", (next_run, sub_id))
    conn.commit()
    conn.close()


def get_user_subscriptions(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, items_json, frequency, next_run, active FROM subscriptions WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def cancel_subscription(sub_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE subscriptions SET active = 0 WHERE id = ?", (sub_id,))
    conn.commit()
    conn.close()


# ---------- YOOKASSA ----------

def yookassa_configured():
    return bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY)


def create_payment(amount_rub, order_id, bot_username):
    try:
        response = requests.post(
            YOOKASSA_API_URL,
            auth=(config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY),
            headers={"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"},
            json={
                "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": f"https://t.me/{bot_username}"},
                "capture": True,
                "description": f"Оплата заказа №{order_id} — Вода Усада",
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data["id"], data["confirmation"]["confirmation_url"]
    except Exception as e:
        logger.error(f"Ошибка создания платежа: {e}")
        return None, None


def check_payment(payment_id):
    try:
        response = requests.get(
            f"{YOOKASSA_API_URL}/{payment_id}",
            auth=(config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY),
            timeout=15,
        )
        response.raise_for_status()
        return response.json()["status"]
    except Exception as e:
        logger.error(f"Ошибка проверки платежа: {e}")
        return None


# ---------- STATES ----------

class Checkout(StatesGroup):
    address = State()
    phone = State()
    payment_method = State()
    frequency = State()
    confirm = State()


class AddProduct(StatesGroup):
    waiting_data = State()


# ---------- KEYBOARDS ----------

def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🛒 Корзина")],
            [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="🔁 Мои подписки")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)


def categories_kb(categories):
    buttons = [[InlineKeyboardButton(text=c, callback_data=f"cat_{c}")] for c in categories]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def product_kb(product_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить в корзину", callback_data=f"add_{product_id}")]]
    )


def cart_kb(cart):
    buttons = []
    for item in cart:
        buttons.append([
            InlineKeyboardButton(text=f"➖ {item['name']}", callback_data=f"rm_{item['product_id']}"),
        ])
    if cart:
        buttons.append([InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="clearcart")])
        buttons.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payment_method_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💵 Наличными курьеру")],
            [KeyboardButton(text="💳 Онлайн в боте")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def frequency_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Разовый заказ")],
            [KeyboardButton(text="Каждую неделю")],
            [KeyboardButton(text="Каждый месяц")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def confirm_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Подтвердить")], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def admin_order_kb(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"oaccept_{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"oreject_{order_id}"),
        ]]
    )


def payment_kb(order_id, url):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=url)],
            [InlineKeyboardButton(text="✅ Я оплатил — проверить", callback_data=f"checkpay_{order_id}")],
        ]
    )


# ---------- HELPERS ----------

async def safe_edit_text(message, text, reply_markup=None):
    """Редактирует сообщение, но не падает, если Telegram считает текст неизменённым."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def apply_tier_pricing(cart):
    """Пересчитывает эффективную цену каждой позиции с учётом общего количества
    товаров с пороговыми ценами (например, бутылей 18,9л) в корзине."""
    tiered_total_qty = sum(item["qty"] for item in cart if item.get("tier2") or item.get("tier10"))

    for item in cart:
        if item.get("tier2") or item.get("tier10"):
            if item.get("tier10") and tiered_total_qty >= 10:
                item["effective_price"] = item["tier10"]
            elif item.get("tier2") and tiered_total_qty >= 2:
                item["effective_price"] = item["tier2"]
            else:
                item["effective_price"] = item["price"]
        else:
            item["effective_price"] = item["price"]

    return cart


def cart_total(cart):
    cart = apply_tier_pricing(cart)
    return round(sum(item["effective_price"] * item["qty"] for item in cart), 2)


def cart_text(cart):
    if not cart:
        return "Корзина пуста. Загляни в 🛍 Каталог, чтобы выбрать товары."
    cart = apply_tier_pricing(cart)
    lines = ["🛒 Ваша корзина:\n"]
    for item in cart:
        price = item["effective_price"]
        discount_note = " 🔻" if price < item["price"] else ""
        lines.append(f"• {item['name']} × {item['qty']} = {price * item['qty']:.0f} ₽{discount_note}")
    if any(item["effective_price"] < item["price"] for item in cart):
        lines.append("\n🔻 — применена скидка за объём заказа")
    lines.append(f"\n💰 Итого: {cart_total(cart):.0f} ₽")
    return "\n".join(lines)


# ---------- HANDLERS: BASIC ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Добро пожаловать в «Вода Усада»!\n\n"
        "Заказывайте питьевую воду, кулеры и всё необходимое с доставкой на дом или в офис. "
        "Можно оформить разовый заказ или подписаться на регулярную доставку.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    await message.answer(
        "Как заказать:\n"
        "1️⃣ Открой 🛍 Каталог и добавь товары в корзину\n"
        "2️⃣ Перейди в 🛒 Корзину и нажми «Оформить заказ»\n"
        "3️⃣ Укажи адрес, телефон и способ оплаты\n"
        "4️⃣ При желании — подключи регулярную доставку\n\n"
        "Команды:\n"
        "/catalog — открыть каталог\n"
        "/cart — открыть корзину\n"
        "/myorders — мои заказы\n"
        "/mysubs — мои подписки\n"
        "/cancel — отменить оформление"
    )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять 🙂", reply_markup=main_menu_kb())
        return
    await state.clear()
    await message.answer("Оформление отменено.", reply_markup=main_menu_kb())


# ---------- HANDLERS: CATALOG ----------

@router.message(Command("catalog"))
@router.message(F.text == "🛍 Каталог")
async def show_catalog(message: Message):
    categories = get_categories()
    if not categories:
        await message.answer("Каталог пока пуст. Загляните позже 🙂")
        return
    await message.answer("Выберите категорию:", reply_markup=categories_kb(categories))


@router.callback_query(F.data.startswith("cat_"))
async def show_category_products(callback: CallbackQuery):
    category = callback.data[len("cat_"):]
    products = get_products_by_category(category)
    if not products:
        await callback.answer("В этой категории пока нет товаров.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(f"📦 Категория: {category}")
    for product_id, name, unit, price, tier2, tier10 in products:
        price_lines = [f"{unit} — {price:.0f} ₽"]
        if tier2:
            price_lines.append(f"от 2 шт — {tier2:.0f} ₽/шт")
        if tier10:
            price_lines.append(f"от 10 шт — {tier10:.0f} ₽/шт")
        await callback.message.answer(
            f"<b>{name}</b>\n" + "\n".join(price_lines),
            parse_mode="HTML",
            reply_markup=product_kb(product_id),
        )


@router.callback_query(F.data.startswith("add_"))
async def add_to_cart(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data[len("add_"):])
    product = get_product(product_id)
    if not product:
        await callback.answer("Товар недоступен.", show_alert=True)
        return

    _, category, name, unit, price, tier2, tier10 = product
    data = await state.get_data()
    cart = data.get("cart", [])

    for item in cart:
        if item["product_id"] == product_id:
            item["qty"] += 1
            break
    else:
        cart.append({
            "product_id": product_id,
            "category": category,
            "name": name,
            "unit": unit,
            "price": price,
            "tier2": tier2,
            "tier10": tier10,
            "qty": 1,
        })

    await state.update_data(cart=cart)
    await callback.answer(f"Добавлено: {name}")


# ---------- HANDLERS: CART ----------

@router.message(Command("cart"))
@router.message(F.text == "🛒 Корзина")
async def show_cart(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", [])
    await message.answer(cart_text(cart), reply_markup=cart_kb(cart))


@router.callback_query(F.data.startswith("rm_"))
async def remove_from_cart(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data[len("rm_"):])
    data = await state.get_data()
    cart = data.get("cart", [])

    for item in cart:
        if item["product_id"] == product_id:
            item["qty"] -= 1
            if item["qty"] <= 0:
                cart.remove(item)
            break

    await state.update_data(cart=cart)
    await callback.answer("Обновлено")
    await safe_edit_text(callback.message, cart_text(cart), reply_markup=cart_kb(cart))


@router.callback_query(F.data == "clearcart")
async def clear_cart(callback: CallbackQuery, state: FSMContext):
    await state.update_data(cart=[])
    await callback.answer("Корзина очищена")
    await safe_edit_text(callback.message, cart_text([]), reply_markup=cart_kb([]))


# ---------- HANDLERS: CHECKOUT ----------

@router.callback_query(F.data == "checkout")
async def start_checkout(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", [])
    if not cart:
        await callback.answer("Корзина пуста.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(Checkout.address)
    await callback.message.answer(
        "Укажи адрес доставки (город, улица, дом, квартира/офис).",
        reply_markup=cancel_kb(),
    )


@router.message(Checkout.address)
async def checkout_address(message: Message, state: FSMContext):
    if not message.text or len(message.text) < 5:
        await message.answer("Пожалуйста, укажи адрес подробнее.")
        return
    await state.update_data(address=message.text)
    await state.set_state(Checkout.phone)
    await message.answer("Укажи номер телефона для связи с курьером.", reply_markup=cancel_kb())


@router.message(Checkout.phone)
async def checkout_phone(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Укажи телефон текстом.")
        return
    await state.update_data(phone=message.text)
    await state.set_state(Checkout.payment_method)
    await message.answer("Как удобнее оплатить?", reply_markup=payment_method_kb())


@router.message(Checkout.payment_method, F.text.in_(["💵 Наличными курьеру", "💳 Онлайн в боте"]))
async def checkout_payment_method(message: Message, state: FSMContext):
    method = "cash" if "Наличными" in message.text else "online"
    await state.update_data(payment_method=method)
    await state.set_state(Checkout.frequency)
    await message.answer(
        "Оформить как разовый заказ или подключить регулярную доставку?",
        reply_markup=frequency_kb(),
    )


@router.message(Checkout.payment_method)
async def checkout_payment_invalid(message: Message):
    await message.answer("Выбери способ оплаты кнопкой ниже 👇")


@router.message(Checkout.frequency, F.text.in_(list(FREQUENCY_LABELS.values())))
async def checkout_frequency(message: Message, state: FSMContext):
    freq_map = {v: k for k, v in FREQUENCY_LABELS.items()}
    frequency = freq_map[message.text]
    await state.update_data(frequency=frequency)

    data = await state.get_data()
    cart = data.get("cart", [])
    total = cart_total(cart)

    summary = (
        "Проверь заказ:\n\n"
        f"{cart_text(cart)}\n\n"
        f"📍 Адрес: {data['address']}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"💳 Оплата: {'Наличными курьеру' if data['payment_method'] == 'cash' else 'Онлайн в боте'}\n"
        f"🔁 Формат: {FREQUENCY_LABELS[frequency]}\n\n"
        "Всё верно?"
    )
    await state.set_state(Checkout.confirm)
    await message.answer(summary, reply_markup=confirm_kb())


@router.message(Checkout.frequency)
async def checkout_frequency_invalid(message: Message):
    await message.answer("Выбери вариант кнопкой ниже 👇")


@router.message(Checkout.confirm, F.text == "✅ Подтвердить")
async def checkout_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    cart = apply_tier_pricing(data.get("cart", []))
    total = cart_total(cart)

    # Фиксируем итоговую цену на момент заказа — дальше она не должна пересчитываться
    for item in cart:
        item["price"] = item["effective_price"]

    address = data["address"]
    phone = data["phone"]
    payment_method = data["payment_method"]
    frequency = data["frequency"]

    payment_status = "not_required"
    if payment_method == "online" and yookassa_configured():
        payment_status = "pending"

    order_id = save_order(
        user_id=message.from_user.id,
        username=message.from_user.username or "—",
        full_name=message.from_user.full_name or "—",
        items=cart,
        total_rub=total,
        address=address,
        phone=phone,
        payment_method=payment_method,
        payment_status=payment_status,
        frequency=frequency,
    )

    if frequency != "none":
        save_subscription(
            user_id=message.from_user.id,
            username=message.from_user.username or "—",
            full_name=message.from_user.full_name or "—",
            items=cart,
            total_rub=total,
            address=address,
            phone=phone,
            payment_method=payment_method,
            frequency=frequency,
        )

    await state.update_data(cart=[])
    await state.set_state(None)

    admin_text = (
        f"🆕 Новый заказ №{order_id}\n\n"
        f"{cart_text(cart)}\n\n"
        f"👤 {message.from_user.full_name} (@{message.from_user.username or '—'})\n"
        f"📍 {address}\n"
        f"📞 {phone}\n"
        f"💳 {'Наличными' if payment_method == 'cash' else 'Онлайн'}\n"
        f"🔁 {FREQUENCY_LABELS[frequency]}"
    )

    if payment_status == "pending":
        bot_info = await bot.get_me()
        payment_id, payment_url = create_payment(total, order_id, bot_info.username)
        if payment_id and payment_url:
            update_order_payment(order_id, payment_id=payment_id)
            await message.answer(
                f"✅ Заказ №{order_id} оформлен!\n💰 К оплате: {total:.0f} ₽",
                reply_markup=main_menu_kb(),
            )
            await message.answer("Оплата заказа:", reply_markup=payment_kb(order_id, payment_url))
            admin_text += "\n\n💳 Ожидает оплаты в боте"
        else:
            await message.answer(
                f"✅ Заказ №{order_id} оформлен!\nОплата наличными курьеру при получении.",
                reply_markup=main_menu_kb(),
            )
            admin_text += "\n\n⚠️ Не удалось создать онлайн-платёж — оплата курьеру"
    elif payment_method == "online" and not yookassa_configured():
        await message.answer(
            f"✅ Заказ №{order_id} оформлен!\n"
            "Онлайн-оплата пока недоступна — с тобой свяжутся, чтобы уточнить оплату.",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer(
            f"✅ Заказ №{order_id} оформлен!\n"
            f"{'Оплата наличными курьеру при получении.' if payment_method == 'cash' else ''}",
            reply_markup=main_menu_kb(),
        )

    if frequency != "none":
        await message.answer(
            f"🔁 Подключена регулярная доставка: {FREQUENCY_LABELS[frequency]}. "
            "Отменить можно в разделе «🔁 Мои подписки»."
        )

    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=admin_order_kb(order_id))
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа {admin_id}: {e}")


# ---------- HANDLERS: PAYMENT CHECK ----------

@router.callback_query(F.data.startswith("checkpay_"))
async def check_payment_callback(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    order = get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    payment_id = order[9]  # колонка payment_id
    if not payment_id:
        await callback.answer("Платёж ещё не создан.", show_alert=True)
        return

    status = check_payment(payment_id)
    if status == "succeeded":
        update_order_payment(order_id, payment_status="paid")
        await callback.message.answer(f"✅ Оплата заказа №{order_id} подтверждена! Спасибо 🙌")
        await callback.answer("Оплата подтверждена!")
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"💳 Заказ №{order_id} оплачен онлайн!")
            except Exception:
                pass
    elif status == "pending":
        await callback.answer("Оплата ещё не поступила. Попробуй через минуту.", show_alert=True)
    else:
        await callback.answer("Не получилось проверить статус оплаты.", show_alert=True)


# ---------- HANDLERS: MY ORDERS / SUBSCRIPTIONS ----------

@router.message(Command("myorders"))
@router.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message):
    orders = get_user_orders(message.from_user.id)
    if not orders:
        await message.answer("У тебя пока нет заказов.")
        return
    lines = ["Твои заказы:\n"]
    for order_id, items_json, total, status, created_at in orders:
        items = json.loads(items_json)
        items_short = ", ".join(f"{i['name']} ×{i['qty']}" for i in items)
        lines.append(f"№{order_id} • {items_short} • {total:.0f}₽ • {status} • {created_at}")
    await message.answer("\n".join(lines))


@router.message(Command("mysubs"))
@router.message(F.text == "🔁 Мои подписки")
async def my_subscriptions(message: Message):
    subs = get_user_subscriptions(message.from_user.id)
    if not subs:
        await message.answer("У тебя пока нет активных подписок на регулярную доставку.")
        return

    for sub_id, items_json, frequency, next_run, active in subs:
        if not active:
            continue
        items = json.loads(items_json)
        items_short = ", ".join(f"{i['name']} ×{i['qty']}" for i in items)
        text = (
            f"🔁 Подписка №{sub_id}\n"
            f"{items_short}\n"
            f"Периодичность: {FREQUENCY_LABELS.get(frequency, frequency)}\n"
            f"Следующая доставка: {next_run}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить подписку", callback_data=f"cancelsub_{sub_id}")]]
        )
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cancelsub_"))
async def cancel_sub_callback(callback: CallbackQuery):
    sub_id = int(callback.data.split("_")[1])
    cancel_subscription(sub_id)
    await callback.answer("Подписка отменена")
    await safe_edit_text(callback.message, callback.message.text + "\n\n❌ Отменена")


# ---------- HANDLERS: ADMIN — CATALOG MANAGEMENT ----------

@router.message(Command("addproduct"))
async def admin_add_product(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return
    await message.answer(
        "Пришли товар в одном из форматов:\n\n"
        "Без скидок за объём:\n"
        "Категория | Название | Единица | Цена\n\n"
        "С скидкой за объём (2-9 шт и 10+ шт):\n"
        "Категория | Название | Единица | Цена | Цена от 2шт | Цена от 10шт\n\n"
        "Например:\nВода | Усада 18,9л Минеральная | 1 бутыль | 450 | 310 | 290"
    )


@router.message(F.text.regexp(r"^[^|]+\|[^|]+\|[^|]+\|[^|]+(\|[^|]+\|[^|]+)?\s*$"))
async def admin_parse_product(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    parts = [p.strip() for p in message.text.split("|")]
    if len(parts) not in (4, 6):
        return

    category, name, unit, price_text = parts[0], parts[1], parts[2], parts[3]
    try:
        price = float(price_text.replace(",", "."))
    except ValueError:
        await message.answer("Не понял цену за 1 шт. Проверь формат.")
        return

    tier2 = tier10 = None
    if len(parts) == 6:
        tier2_text, tier10_text = parts[4], parts[5]
        if tier2_text != "-":
            try:
                tier2 = float(tier2_text.replace(",", "."))
            except ValueError:
                await message.answer("Не понял цену от 2 шт. Проверь формат.")
                return
        if tier10_text != "-":
            try:
                tier10 = float(tier10_text.replace(",", "."))
            except ValueError:
                await message.answer("Не понял цену от 10 шт. Проверь формат.")
                return

    product_id = add_product(category, name, unit, price, tier2, tier10)
    tier_note = ""
    if tier2 or tier10:
        tier_note = f" (от 2шт: {tier2 or '—'}₽, от 10шт: {tier10 or '—'}₽)"
    await message.answer(f"✅ Товар добавлен (id {product_id}): {name} — {price:.0f} ₽{tier_note}")


@router.message(Command("products"))
async def admin_list_products(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return
    products = get_all_products()
    if not products:
        await message.answer("Каталог пуст.")
        return
    lines = ["📦 Каталог:\n"]
    for pid, category, name, unit, price, tier2, tier10, active in products:
        mark = "" if active else " (скрыт)"
        tier_note = ""
        if tier2 or tier10:
            tier_note = f" [от2: {tier2 or '—'}₽ / от10: {tier10 or '—'}₽]"
        lines.append(f"#{pid} [{category}] {name} — {unit}, {price:.0f}₽{tier_note}{mark}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


@router.message(Command("delproduct"))
async def admin_del_product(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи id товара: /delproduct 3")
        return
    try:
        product_id = int(parts[1].strip())
    except ValueError:
        await message.answer("id должен быть числом.")
        return
    deactivate_product(product_id)
    await message.answer(f"Товар #{product_id} скрыт из каталога.")


# ---------- HANDLERS: ADMIN — ALL ORDERS ----------

@router.message(Command("allorders"))
async def admin_all_orders(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_name, total_rub, address, status, payment_status, created_at "
        "FROM orders ORDER BY id DESC LIMIT 30"
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    lines = ["📋 Последние заказы:\n"]
    for order_id, full_name, total, address, status, payment_status, created_at in rows:
        lines.append(f"№{order_id} • {full_name} • {total:.0f}₽ • {address} • {status}/{payment_status} • {created_at}")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


@router.callback_query(F.data.startswith("oaccept_"))
async def admin_order_accept(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    update_order_status(order_id, "accepted")
    order = get_order(order_id)
    if order:
        try:
            await bot.send_message(order[1], f"✅ Твой заказ №{order_id} принят в работу!")
        except Exception:
            pass
    await safe_edit_text(callback.message, callback.message.text + "\n\n✅ Принят")
    await callback.answer("Принят")


@router.callback_query(F.data.startswith("oreject_"))
async def admin_order_reject(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    update_order_status(order_id, "rejected")
    order = get_order(order_id)
    if order:
        try:
            await bot.send_message(order[1], f"❌ Твой заказ №{order_id} отклонён. Свяжись с нами для уточнения.")
        except Exception:
            pass
    await safe_edit_text(callback.message, callback.message.text + "\n\n❌ Отклонён")
    await callback.answer("Отклонён")


# ---------- FALLBACK ----------

@router.message()
async def fallback(message: Message):
    await message.answer(
        "Не совсем понял 🙂 Используй меню ниже или /catalog, чтобы посмотреть товары.",
        reply_markup=main_menu_kb(),
    )


# ---------- SUBSCRIPTION SCHEDULER ----------

async def subscription_checker():
    while True:
        try:
            due = get_due_subscriptions()
            for sub in due:
                (sub_id, user_id, username, full_name, items_json, total_rub,
                 address, phone, payment_method, frequency, next_run, active) = sub

                items = json.loads(items_json)
                order_id = save_order(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                    items=items,
                    total_rub=total_rub,
                    address=address,
                    phone=phone,
                    payment_method=payment_method,
                    payment_status="not_required",
                    frequency=frequency,
                )
                advance_subscription(sub_id, frequency)

                try:
                    await bot.send_message(
                        user_id,
                        f"🔁 Оформлен регулярный заказ №{order_id} по подписке.\n"
                        f"{cart_text(items)}\n\nМы скоро свяжемся для подтверждения доставки.",
                    )
                except Exception:
                    pass

                for admin_id in config.ADMIN_IDS:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"🔁 Автозаказ по подписке №{order_id}\n{full_name}, {address}, {phone}\n"
                            f"{cart_text(items)}",
                            reply_markup=admin_order_kb(order_id),
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Ошибка проверки подписок: {e}")

        await asyncio.sleep(12 * 60 * 60)  # проверка каждые 12 часов


# ---------- ENTRYPOINT ----------

async def main():
    init_db()
    asyncio.create_task(subscription_checker())
    logger.info("Бот Усада запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
