import asyncio
import uuid
import aiohttp
import os
import logging
import json
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest
from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
YUKASSA_SHOP_ID = os.environ.get("YUKASSA_SHOP_ID")
YUKASSA_SECRET_KEY = os.environ.get("YUKASSA_SECRET_KEY")
PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_URL", "")
BANNER_WELCOME_URL = "https://raw.githubusercontent.com/disxrm/voidtweaks/main/banner_welcome.png"
BANNER_PLANS_URL = "https://raw.githubusercontent.com/disxrm/voidtweaks/main/banner_plans.png"

_banner_welcome_id: str | None = None
_banner_plans_id: str | None = None

async def get_banner(url: str, name: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return BufferedInputFile(data, filename=name)
    except Exception as e:
        logger.error(f"Ошибка загрузки баннера {name}: {e}")
    return None

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PLANS = {
    "month":   {"name": "1 месяц",   "price": 99,  "days": 30},
    "half":    {"name": "6 месяцев", "price": 299, "days": 180},
    "forever": {"name": "Навсегда",  "price": 499, "days": 36500},
}

user_last_action: dict[int, datetime] = {}
FLOOD_TIMEOUT = 2

def is_flood(user_id: int) -> bool:
    now = datetime.now()
    last = user_last_action.get(user_id)
    if last and (now - last).total_seconds() < FLOOD_TIMEOUT:
        return True
    user_last_action[user_id] = now
    return False

used_payment_ids: set[str] = set()

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить лицензию", callback_data="buy")],
        [InlineKeyboardButton(text="🔑 Мои лицензии",    callback_data="mylicense")],
        [InlineKeyboardButton(text="📞 Поддержка",        callback_data="support"),
         InlineKeyboardButton(text="⬇️ Скачать",         url="https://github.com/disxrm/voidtweaks/releases/download/1.0/VOIDTWEAKS.exe")],
    ])

def plans_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц — 99₽", callback_data="plan_month")],
        [InlineKeyboardButton(text="6 месяцев — 299₽", callback_data="plan_half")],
        [InlineKeyboardButton(text="Навсегда — 499₽", callback_data="plan_forever")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")],
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
        "capture": True,
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

async def issue_license(telegram_id: int, plan_key: str, payment_id: str) -> str | None:
    if payment_id in used_payment_ids:
        logger.warning(f"Двойная выдача по payment_id {payment_id} заблокирована")
        return None

    existing = supabase.table("licenses").select("license_key").eq("payment_id", payment_id).execute()
    if existing.data:
        logger.warning(f"Лицензия по {payment_id} уже в БД")
        used_payment_ids.add(payment_id)
        return existing.data[0]["license_key"]

    try:
        old = supabase.table("licenses").select("license_key, plan").eq(
            "telegram_id", telegram_id
        ).eq("is_active", True).execute()
        for old_lic in old.data or []:
            supabase.table("licenses").update({"is_active": False}).eq(
                "license_key", old_lic["license_key"]
            ).execute()
            logger.info(f"Апгрейд: деактивирована лицензия {old_lic['license_key']} (план {old_lic['plan']})")
    except Exception as e:
        logger.warning(f"Не удалось деактивировать старые лицензии: {e}")

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

async def notify_expiring_licenses():
    while True:
        await asyncio.sleep(12 * 3600)
        try:
            now_iso = datetime.now().isoformat()
            soon_iso = (datetime.now() + timedelta(days=3)).isoformat()

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
    while True:
        await asyncio.sleep(300)
        try:
            url = f"https://{RENDER_URL}/" if RENDER_URL else f"http://localhost:{PORT}/"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logger.info(f"Keep-alive ping: {resp.status}")
        except Exception as e:
            logger.warning(f"Keep-alive ping не удался: {e}")

@dp.message(CommandStart())
async def start(message: Message):
    global _banner_welcome_id
    banner = _banner_welcome_id or await get_banner(BANNER_WELCOME_URL, "banner_welcome.png")
    if banner:
        msg = await message.answer_photo(
            photo=banner,
            caption="👋 Добро пожаловать в <b>VoidTweaks</b>!\n\n"
                    "🚀 Программа для оптимизации Windows ПК\n"
                    "⚡ Увеличивает FPS и производительность\n\n"
                    "Выберите действие:",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        if msg.photo:
            _banner_welcome_id = msg.photo[-1].file_id
    else:
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
        await callback.message.delete()
    except Exception:
        pass
    global _banner_plans_id
    banner = _banner_plans_id or await get_banner(BANNER_PLANS_URL, "banner_plans.png")
    if banner:
        msg = await callback.message.answer_photo(
            photo=banner,
            caption="💳 <b>Выберите тариф:</b>",
            parse_mode="HTML",
            reply_markup=plans_menu()
        )
        if hasattr(msg, 'photo') and msg.photo:
            _banner_plans_id = msg.photo[-1].file_id
    else:
        await callback.message.answer(
            "💳 <b>Выберите тариф:</b>\n\n"
            "📅 <b>1 месяц — 99₽</b>\n"
            "📅 <b>6 месяцев — 299₽</b>\n"
            "♾️ <b>Навсегда — 499₽</b>",
            parse_mode="HTML",
            reply_markup=plans_menu()
        )

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
        except Exception:
            await callback.message.answer(
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
            except Exception:
                await callback.message.answer(
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

        supabase.table("licenses").update({"is_active": False}).eq(
            "telegram_id", callback.from_user.id
        ).eq("is_active", True).lt("expires_at", now_iso).execute()

        result = supabase.table("licenses").select("*").eq(
            "telegram_id", callback.from_user.id
        ).eq("is_active", True).execute()

        has_expiring = False
        if result.data:
            text = "🔑 <b>Ваши активные лицензии:</b>\n\n"
            for lic in result.data:
                expires = lic.get("expires_at")
                plan_name = PLANS.get(lic.get("plan", ""), {}).get("name", lic.get("plan", "—"))
                hwid = lic.get("hwid")
                hwid_text = f"💻 ПК: <code>{hwid}</code>\n" if hwid else ""
                if expires:
                    exp_dt = datetime.fromisoformat(expires)
                    exp_date = exp_dt.strftime("%d.%m.%Y")
                    days_left = (exp_dt - datetime.now()).days
                    if days_left <= 0:
                        days_text = "⏰ Истекает сегодня!"
                    elif days_left == 1:
                        days_text = "⏰ Остался 1 день!"
                    else:
                        days_text = f"⏳ Осталось дней: {days_left}"
                    if days_left <= 7:
                        has_expiring = True
                    text += (
                        f"<code>{lic['license_key']}</code>\n"
                        f"📦 Тариф: {plan_name}\n"
                        f"{hwid_text}"
                        f"📅 До: {exp_date}\n"
                        f"{days_text}\n\n"
                    )
                else:
                    text += (
                        f"<code>{lic['license_key']}</code>\n"
                        f"📦 Тариф: {plan_name}\n"
                        f"{hwid_text}"
                        f"♾️ Навсегда\n\n"
                    )
        else:
            text = "❌ У вас нет активных лицензий\n\nНажмите <b>Купить лицензию</b>"

        keyboard = []
        if has_expiring:
            keyboard.append([InlineKeyboardButton(text="🔄 Продлить со скидкой 10%", callback_data="renew")])
        keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back")])

        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    except Exception as e:
        logger.error(f"Ошибка в my_license: {e}")
        await callback.answer("❌ Ошибка загрузки лицензий.", show_alert=True)

@dp.callback_query(F.data == "renew")
async def renew(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    renew_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"1 месяц — {int(99*0.9)}₽ (-10%)",      callback_data="plan_month_renew")],
        [InlineKeyboardButton(text=f"6 месяцев — {int(299*0.9)}₽ (-10%)",   callback_data="plan_half_renew")],
        [InlineKeyboardButton(text=f"Навсегда — {int(499*0.9)}₽ (-10%)",    callback_data="plan_forever_renew")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")],
    ])
    await callback.message.answer(
        "🔄 <b>Продление лицензии</b>\n\n"
        "Скидка 10% за продление — спасибо, что остаётесь с нами! 🙏\n\n"
        "Выберите тариф:",
        parse_mode="HTML",
        reply_markup=renew_keyboard
    )

@dp.callback_query(F.data.startswith("plan_") & F.data.endswith("_renew"))
async def select_plan_renew(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    plan_key = callback.data.replace("plan_", "").replace("_renew", "")
    if plan_key not in PLANS:
        await callback.answer("❌ Неверный тариф", show_alert=True)
        return
    plan = PLANS[plan_key]
    discounted_price = int(plan["price"] * 0.9)
    order_id = f"{callback.from_user.id}_{plan_key}_{int(datetime.now().timestamp())}"
    payment_id, payment_url = await create_yukassa_invoice(
        discounted_price, order_id,
        f"VoidTweaks — продление {plan['name']} (скидка 10%)"
    )
    if not payment_url:
        await callback.answer("❌ Ошибка создания платежа. Попробуйте позже.", show_alert=True)
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"💳 <b>Продление: {plan['name']}</b>\n\n"
        f"💰 Сумма со скидкой: <b>{discounted_price}₽</b> (было {plan['price']}₽)\n\n"
        f"После оплаты старая лицензия будет заменена новой.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_{payment_id}_{plan_key}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")],
        ])
    )

@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "📞 <b>Поддержка</b>\n\nПо всем вопросам: @disxrm",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
        ])
    )

@dp.callback_query(F.data == "back")
async def back(callback: CallbackQuery):
    if is_flood(callback.from_user.id):
        await callback.answer("⏳ Не так быстро!")
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    global _banner_welcome_id
    banner = _banner_welcome_id or await get_banner(BANNER_WELCOME_URL, "banner_welcome.png")
    if banner:
        msg = await callback.message.answer_photo(
            photo=banner,
            caption="👋 Добро пожаловать в <b>VoidTweaks</b>!\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        if hasattr(msg, 'photo') and msg.photo:
            _banner_welcome_id = msg.photo[-1].file_id
    else:
        await callback.message.answer(
            "👋 Добро пожаловать в <b>VoidTweaks</b>!\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

YUKASSA_IPS = {
    "185.71.76.0", "185.71.77.0", "77.75.153.0", "77.75.156.11",
    "77.75.156.35", "77.75.154.128", "2a02:5180::/32"
}

async def yukassa_webhook(request: web.Request):
    peer = request.headers.get("X-Forwarded-For", request.remote or "")
    client_ip = peer.split(",")[0].strip()
    if client_ip not in YUKASSA_IPS:
        logger.warning(f"Webhook отклонён: неизвестный IP {client_ip}")
        return web.Response(text="Forbidden", status=403)

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

                        await asyncio.sleep(1)
                        try:
                            await bot.send_message(
                                telegram_id,
                                "📋 <b>Как активировать VoidTweaks:</b>\n\n"
                                "1️⃣ Нажмите <b>⬇️ Скачать</b> в меню и установите программу\n"
                                "2️⃣ Запустите <b>VOIDTWEAKS.exe</b>\n"
                                "3️⃣ Нажмите кнопку <b>«Запустить оптимизацию»</b>\n"
                                "4️⃣ В появившемся окне вставьте ключ выше\n"
                                "5️⃣ Нажмите <b>«Активировать»</b> — готово! 🚀\n\n"
                                "❓ Если что-то не работает — пишите @disxrm",
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"Не удалось отправить онбординг {telegram_id}: {e}")

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
    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logger.error(f"Polling упал: {e}. Перезапуск через 5 секунд...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
