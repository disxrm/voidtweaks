import asyncio
import uuid
import aiohttp
import os
import logging
import json
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest
from supabase import create_client

# ===== ЛОГИРОВАНИЕ =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

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

# Антифлуд
user_last_action: dict[int, datetime] = {}
FLOOD_TIMEOUT = 2  # секунды

def is_flood(user_id: int) -> bool:
    now = datetime.now()
    last = user_last_action.get(user_id)
    if last and (now - last).total_seconds() < FLOOD_TIMEOUT:
        return True
    user_last_action[user_id] = now
    return False

# Защита от двойной выдачи ключей
used_payment_ids: set[str] = set()

# ===== МЕНЮ =====

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

# ===== ЮKASSA =====

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
        "capture": True,  # Обязательно для СБП
        "confirmation": {"type": "redirect", "return_url": "https://t.me/VOIDTWEAKS_BOT"},
        "description": description,
        "metadata": {"order_id": order_id}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.yookassa.ru/v3/payments",
                json=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                result = await resp.json()
                if resp.status != 200:
                    logger.error(f"ЮKassa ошибка {resp.status}: {result}")
                    return None, None
                payment_id = result.get("id")
                payment_url = result.get("confirmation", {}).get("confirmation_url")
                logger.info(f"Создан платёж {payment_id} на {amount}₽")
                return payment_id, payment_url
    except Exception as e:
        logger.error(f"Ошибка создания платежа: {e}")
        return None, None

async def check_yukassa_payment(payment_id: str):
    import base64
    credentials = base64.b64encode(f"{YUKASSA_SHOP_ID}:{YUKASSA_SECRET_KEY}".encode()).decode()
    headers = {"Authorization": f"Basic {credentials}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.yookassa.ru/v3/payments/{payment_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                result = await resp.json()
                status = result.get("status")
                logger.info(f"Платёж {payment_id}: {status}")
                return status
    except Exception as e:
        logger.error(f"Ошибка проверки платежа {payment_id}: {e}")
        return None

# ===== ВЫДАЧА ЛИЦЕНЗИИ =====

async def issue_license(telegram_id: int, plan_key: str, payment_id: str) -> str | None:
    """Выдаёт лицензию. Защита от двойной выдачи по payment_id."""
    if payment_id in used_payment_ids:
        logger.warning(f"Двойная выдача по payment_id {payment_id} заблокирована")
        return None

    # Проверка в БД
    existing = supabase.table("licenses").select("license_key").eq("payment_id", payment_id).execute()
    if existing.data:
        logger.warning(f"Лицензия по {payment_id} уже в БД")
        used_payment_ids.add(payment_id)
        return existing.data[0]["license_key"]

    plan = PLANS[plan_key]
    key = generate_key()
    expires = datetime.now() + timedelta(days=plan["days"])

    try:
        supabase.table("licenses").insert({
            "license_key": key,
            "telegram_id": telegram_id,
            "plan": plan_key,
            "payment_id": payment_id,
            "expires_at": expires.isoformat() if plan_key != "forever" else None,
            "is_active": True
        }).execute()
        used_payment_ids.add(payment_id)
        logger.info(f"Лицензия {key} выдана пользователю {telegram_id}, план {plan_key}")
        return key
    except Exception as e:
        logger.error(f"Ошибка записи лицензии: {e}")
        return None

# ===== ФОНОВЫЕ ЗАДАЧИ =====

async def notify_expiring_licenses():
    """Каждые 12 часов уведомляет об истекающих лицензиях и деактивирует просроченные."""
    while True:
        await asyncio.sleep(12 * 3600)
        try:
            now_iso = datetime.now().isoformat()
            soon_iso = (datetime.now() + timedelta(days=3)).isoformat()

            # Уведомляем об истечении через 3 дня
            expiring = supabase.table("licenses").select("*").eq(
                "is_active", True
            ).gte("expires_at", now_iso).lte("expires_at", soon_iso).execute()

            for lic in expiring.data or []:
                try:
                    exp_date = datetime.fromisoformat(lic["expires_at"]).strftime("%d.%m.%Y")
                    await bot.send_message(
                        lic["telegram_id"],
                        f"⚠️ <b>Лицензия истекает через 3 дня!</b>\n\n"
                        f"🔑 Ключ: <code>{lic['license_key']}</code>\n"
                        f"📅 Истекает: {exp_date}\n\n"
                        f"Продлите лицензию, чтобы не потерять доступ:",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💳 Продлить", callback_data="buy")]
                        ])
                    )
                    logger.info(f"Уведомление отправлено пользователю {lic['telegram_id']}")
                except Exception as e:
                    logger.error(f"Не удалось уведомить {lic['telegram_id']}: {e}")

            # Деактивируем просроченные
            expired = supabase.table("licenses").select("license_key").eq(
                "is_active", True
            ).lt("expires_at", now_iso).execute()

            for lic in expired.data or []:
                supabase.table("licenses").update({"is_active": False}).eq(
                    "license_key", lic["license_key"]
                ).execute()
                logger.info(f"Деактивирована лицензия {lic['license_key']}")

        except Exception as e:
            logger.error(f"Ошибка в notify_expiring_licenses: {e}")

async def keep_alive():
    """Пингуем себя каждые 10 минут чтобы не засыпать на Render."""
    while True:
        await asyncio.sleep(600)
        if RENDER_URL:
            try:
                async with aiohttp.ClientSession() as session:
                    await session.get(f"https://{RENDER_URL}/")
                    logger.info("Ping отправлен — бот не спит!")
            except Exception as e:
                logger.warning(f"Ping не удался: {e}")

# ===== ХЭНДЛЕРЫ =====

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
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    try:
        await callback.message.edit_text(
            "💳 <b>Выберите тариф:</b>\n\n"
            "📅 <b>1 месяц — 99₽</b>\n"
            "📅 <b>6 месяцев — 299₽</b>\n"
            "♾️ <b>Навсегда — 499₽</b>",
            parse_mode="HTML",
            reply_markup=plans_menu()
        )
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data.startswith("plan_"))
async def select_plan(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return

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
        try:
            await callback.message.edit_text(
                f"💳 <b>Оплата — {plan['name']}</b>\n\n"
                f"💰 Сумма: <b>{plan['price']}₽</b>\n\n"
                f"1. Нажмите <b>Оплатить</b>\n"
                f"2. Выберите банк (СБП, Сбер, Тинькофф...)\n"
                f"3. Оплатите и нажмите <b>Проверить оплату</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except TelegramBadRequest:
            pass
    else:
        await callback.answer("❌ Ошибка создания счёта. Попробуйте позже.", show_alert=True)

@dp.callback_query(F.data.startswith("check_"))
async def check_payment(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return

    parts = callback.data.split("_")
    payment_id = parts[1]
    plan_key = parts[2]
    plan = PLANS[plan_key]

    status = await check_yukassa_payment(payment_id)

    if status is None:
        await callback.answer("❌ Ошибка связи с платёжной системой. Попробуйте позже.", show_alert=True)
        return

    if status == "succeeded":
        key = await issue_license(callback.from_user.id, plan_key, payment_id)
        if key:
            expires_text = ""
            if plan_key != "forever":
                exp_date = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")
                expires_text = f"\n📅 Действует до: {exp_date}"
            try:
                await callback.message.edit_text(
                    f"✅ <b>Оплата прошла успешно!</b>\n\n"
                    f"🔑 Ваш ключ активации:\n"
                    f"<code>{key}</code>\n\n"
                    f"📋 Скопируйте ключ и вставьте в программу\n"
                    f"📅 Тариф: {plan['name']}{expires_text}\n\n"
                    f"⚠️ Ключ привязывается к вашему ПК при первой активации",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back")]
                    ])
                )
            except TelegramBadRequest:
                pass
        else:
            await callback.answer("⚠️ Лицензия уже была выдана по этому платежу.", show_alert=True)

    elif status == "pending":
        await callback.answer("⏳ Оплата ещё не прошла. Подождите и попробуйте снова.", show_alert=True)
    elif status == "canceled":
        await callback.answer("❌ Платёж отменён. Создайте новый.", show_alert=True)
    else:
        await callback.answer("❌ Оплата не найдена.", show_alert=True)

@dp.callback_query(F.data == "mylicense")
async def my_license(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    try:
        now_iso = datetime.now().isoformat()

        # Деактивируем просроченные прямо здесь
        supabase.table("licenses").update({"is_active": False}).eq(
            "telegram_id", callback.from_user.id
        ).eq("is_active", True).lt("expires_at", now_iso).execute()

        result = supabase.table("licenses").select("*").eq(
            "telegram_id", callback.from_user.id
        ).eq("is_active", True).execute()

        if result.data:
            text = "🔑 <b>Ваши активные лицензии:</b>\n\n"
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
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error(f"Ошибка в my_license: {e}")
        await callback.answer("❌ Ошибка загрузки лицензий.", show_alert=True)

@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    try:
        await callback.message.edit_text(
            "📞 <b>Поддержка</b>\n\nПо всем вопросам: @vit9aso2",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
            ])
        )
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    try:
        await callback.message.edit_text(
            "👋 Добро пожаловать в <b>VoidTweaks</b>!\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except TelegramBadRequest:
        pass

# ===== WEBHOOK ОТ ЮKASSA (авто-выдача без нажатия кнопки) =====
# Настрой в личном кабинете ЮKassa: HTTP-уведомления → URL: https://ВАШ_ДОМЕН/webhook/yukassa

async def yukassa_webhook(request: web.Request):
    try:
        body = await request.read()
        data = json.loads(body)

        event = data.get("event")
        payment = data.get("object", {})
        payment_id = payment.get("id")

        logger.info(f"ЮKassa webhook: {event}, payment_id={payment_id}")

        if event == "payment.succeeded":
            metadata = payment.get("metadata", {})
            order_id = metadata.get("order_id", "")

            # order_id формат: {telegram_id}_{plan_key}_{timestamp}
            parts = order_id.split("_")
            if len(parts) >= 2:
                telegram_id = int(parts[0])
                plan_key = parts[1]

                if plan_key in PLANS:
                    key = await issue_license(telegram_id, plan_key, payment_id)
                    if key:
                        plan = PLANS[plan_key]
                        expires_text = ""
                        if plan_key != "forever":
                            exp_date = (datetime.now() + timedelta(days=plan["days"])).strftime("%d.%m.%Y")
                            expires_text = f"\n📅 Действует до: {exp_date}"
                        try:
                            await bot.send_message(
                                telegram_id,
                                f"✅ <b>Оплата подтверждена!</b>\n\n"
                                f"🔑 Ваш ключ активации:\n"
                                f"<code>{key}</code>\n\n"
                                f"📅 Тариф: {plan['name']}{expires_text}\n\n"
                                f"⚠️ Ключ привязывается к вашему ПК при первой активации",
                                parse_mode="HTML",
                                reply_markup=main_menu()
                            )
                        except Exception as e:
                            logger.error(f"Не удалось отправить ключ {telegram_id}: {e}")

        return web.Response(text="OK", status=200)

    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        return web.Response(text="Error", status=500)

async def health(request):
    return web.Response(text="OK")

async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/webhook/yukassa", yukassa_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {PORT}")

    asyncio.create_task(keep_alive())
    asyncio.create_task(notify_expiring_licenses())

    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
