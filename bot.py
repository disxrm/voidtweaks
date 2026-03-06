import asyncio
import uuid
import aiohttp
import os
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from supabase import create_client

# ===== КЛЮЧИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
YUKASSA_SHOP_ID = os.environ.get("YUKASSA_SHOP_ID")
YUKASSA_SECRET_KEY = os.environ.get("YUKASSA_SECRET_KEY")
PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_URL", "")
# ==========================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PLANS = {
    "month":   {"name": "1 месяц",   "price": 99,  "days": 30},
    "half":    {"name": "6 месяцев", "price": 299, "days": 180},
    "forever": {"name": "Навсегда",  "price": 499, "days": 36500},
}

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить лицензию", callback_data="buy")],
        [InlineKeyboardButton(text="🔑 Мои лицензии",    callback_data="mylicense")],
        [InlineKeyboardButton(text="📞 Поддержка",        callback_data="support")],
    ])

def plans_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц — 99₽",    callback_data="plan_month")],
        [InlineKeyboardButton(text="6 месяцев — 299₽", callback_data="plan_half")],
        [InlineKeyboardButton(text="Навсегда — 499₽",  callback_data="plan_forever")],
        [InlineKeyboardButton(text="◀️ Назад",          callback_data="back")],
    ])

def generate_key():
    raw = str(uuid.uuid4()).upper().replace("-", "")
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"

async def create_yukassa_invoice(amount: int, order_id: str, description: str):
    import base64
    credentials = base64.b64encode(f"{YUKASSA_SHOP_ID}:{YUKASSA_SECRET_KEY}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
        "Idempotence-Key": order_id
    }
    data = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "capture": True,  # ← ИСПРАВЛЕНИЕ: без этого СБП не отображается
        "confirmation": {"type": "redirect", "return_url": "https://t.me/VOIDTWEAKS_BOT"},
        "description": description,
        "metadata": {"order_id": order_id}
    }
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.yookassa.ru/v3/payments", json=data, headers=headers) as resp:
            result = await resp.json()
            payment_id = result.get("id")
            payment_url = result.get("confirmation", {}).get("confirmation_url")
            return payment_id, payment_url

async def check_yukassa_payment(payment_id: str):
    import base64
    credentials = base64.b64encode(f"{YUKASSA_SHOP_ID}:{YUKASSA_SECRET_KEY}".encode()).decode()
    headers = {"Authorization": f"Basic {credentials}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.yookassa.ru/v3/payments/{payment_id}", headers=headers) as resp:
            result = await resp.json()
            return result.get("status")

# Антисон — пингуем себя каждые 10 минут
async def keep_alive():
    while True:
        await asyncio.sleep(600)  # 10 минут
        if RENDER_URL:
            try:
                async with aiohttp.ClientSession() as session:
                    await session.get(f"https://{RENDER_URL}/")
                    print("Ping отправлен — бот не спит!")
            except:
                pass

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Добро пожаловать в <b>VoidTweaks</b>!\n\n"
        "🚀 Программа для оптимизации Windows ПК\n"
        "⚡ Увеличивает FPS и производительность\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery):
    await callback.message.edit_text(
        "💳 <b>Выберите тариф:</b>\n\n"
        "📅 <b>1 месяц — 99₽</b>\n"
        "📅 <b>6 месяцев — 299₽</b>\n"
        "♾️ <b>Навсегда — 499₽</b>",
        parse_mode="HTML",
        reply_markup=plans_menu()
    )

@dp.callback_query(F.data.startswith("plan_"))
async def select_plan(callback: CallbackQuery):
    plan_key = callback.data.replace("plan_", "")
    plan = PLANS[plan_key]
    order_id = f"{callback.from_user.id}_{plan_key}_{int(datetime.now().timestamp())}"

    payment_id, payment_url = await create_yukassa_invoice(
        amount=plan["price"],
        order_id=order_id,
        description=f"VoidTweaks — {plan['name']}"
    )

    if payment_url:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{payment_id}_{plan_key}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="buy")],
        ])
        await callback.message.edit_text(
            f"💳 <b>Оплата — {plan['name']}</b>\n\n"
            f"💰 Сумма: <b>{plan['price']}₽</b>\n\n"
            f"1. Нажмите <b>Оплатить</b>\n"
            f"2. Выберите банк (СБП, Сбер, Тинькофф...)\n"
            f"3. Оплатите и нажмите <b>Проверить оплату</b>",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        await callback.answer("❌ Ошибка создания счёта. Попробуйте позже.", show_alert=True)

@dp.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    parts = callback.data.split("_")
    payment_id = parts[1]
    plan_key = parts[2]
    plan = PLANS[plan_key]

    status = await check_yukassa_payment(payment_id)

    if status == "succeeded":
        key = generate_key()
        expires = datetime.now() + timedelta(days=plan["days"])

        supabase.table("licenses").insert({
            "license_key": key,
            "telegram_id": callback.from_user.id,
            "plan": plan_key,
            "expires_at": expires.isoformat() if plan_key != "forever" else None,
            "is_active": True
        }).execute()

        await callback.message.edit_text(
            f"✅ <b>Оплата прошла успешно!</b>\n\n"
            f"🔑 Ваш ключ активации:\n"
            f"<code>{key}</code>\n\n"
            f"📋 Скопируйте ключ и вставьте в программу\n"
            f"📅 Тариф: {plan['name']}\n\n"
            f"⚠️ Ключ привязывается к вашему ПК при первой активации",
            parse_mode="HTML"
        )
    elif status == "pending":
        await callback.answer("⏳ Оплата ещё не прошла. Подождите и попробуйте снова.", show_alert=True)
    else:
        await callback.answer("❌ Оплата не найдена или отменена.", show_alert=True)

@dp.callback_query(F.data == "mylicense")
async def my_license(callback: CallbackQuery):
    result = supabase.table("licenses").select("*").eq(
        "telegram_id", callback.from_user.id
    ).eq("is_active", True).execute()

    if result.data:
        text = "🔑 <b>Ваши лицензии:</b>\n\n"
        for lic in result.data:
            expires = lic.get("expires_at")
            if expires:
                exp_date = datetime.fromisoformat(expires).strftime("%d.%m.%Y")
                text += f"<code>{lic['license_key']}</code>\n📅 До: {exp_date}\n\n"
            else:
                text += f"<code>{lic['license_key']}</code>\n♾️ Навсегда\n\n"
    else:
        text = "❌ У вас нет активных лицензий\n\nНажмите <b>Купить лицензию</b>"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu())

@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await callback.message.edit_text(
        "📞 <b>Поддержка</b>\n\nПо всем вопросам: @vit9aso2",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
        ])
    )

@dp.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    await callback.message.edit_text(
        "👋 Добро пожаловать в <b>VoidTweaks</b>!\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )

async def health(request):
    return web.Response(text="OK")

async def main():
    # Веб-сервер
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Веб-сервер запущен на порту {PORT}")

    # Антисон
    asyncio.create_task(keep_alive())

    # Бот
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
