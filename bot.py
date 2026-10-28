"""
Сервер оплаты VoidTweaks PRO (Render).

Telegram-бота больше нет: ключи продаются только на сайте, логика — в web_checkout.py.
Имя файла bot.py оставлено, чтобы не менять команду запуска на Render.
"""

import asyncio
import uuid
import aiohttp
import os
import logging
import json
from datetime import datetime
from aiohttp import web
from supabase import create_client
import web_checkout
import license_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
YUKASSA_SHOP_ID = os.environ.get("YUKASSA_SHOP_ID")
YUKASSA_SECRET_KEY = os.environ.get("YUKASSA_SECRET_KEY")
PORT = int(os.environ.get("PORT", 8080))
RENDER_URL = os.environ.get("RENDER_URL", "")
SITE_URL = os.environ.get("SITE_URL", "")  # https://voidtweaksapp.github.io/voidtweaks  (без / в конце)
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", SITE_URL).split(",") if o.strip()}
# Сайт переехал в организацию voidtweaksapp: оба адреса разрешены всегда, независимо от переменных окружения
ALLOWED_ORIGINS |= {"https://voidtweaksapp.github.io", "https://disxrm.github.io"}

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PLANS = {
    "month":   {"name": "1 месяц",   "price": 99,  "days": 30},
    "half":    {"name": "6 месяцев", "price": 299, "days": 180},
    "forever": {"name": "Навсегда",  "price": 499, "days": 36500},
}

def generate_key():
    raw = str(uuid.uuid4()).upper().replace("-", "")
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"

async def deactivate_expired_licenses():
    while True:
        await asyncio.sleep(12 * 3600)
        try:
            now_iso = datetime.now().isoformat()
            expired = supabase.table("licenses").select("license_key").eq(
                "is_active", True
            ).lt("expires_at", now_iso).execute()

            for lic in expired.data or []:
                supabase.table("licenses").update({"is_active": False}).eq(
                    "license_key", lic["license_key"]
                ).execute()
                logger.info(f"Деактивирована лицензия {lic['license_key']}")

        except Exception as e:
            logger.error(f"Ошибка в deactivate_expired_licenses: {e}")

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

import ipaddress

# Официальные сети ЮKassa: https://yookassa.ru/developers/using-api/webhooks
# (сверь актуальный список в документации — он иногда меняется)
YUKASSA_NETWORKS = [ipaddress.ip_network(n) for n in (
    "185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25",
    "77.75.156.11/32", "77.75.156.35/32", "77.75.154.128/25",
    "2a02:5180::/32",
)]

def is_yukassa_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in YUKASSA_NETWORKS)

async def yukassa_webhook(request: web.Request):
    # IP — лишь первый фильтр (X-Forwarded-For задаёт клиент). Настоящая проверка
    # в issue_web_license: заказ ищется по секретному токену, а статус и сумма
    # платежа перечитываются из API ЮKassa.
    peer = request.headers.get("X-Forwarded-For", request.remote or "")
    client_ip = peer.split(",")[0].strip()
    if not is_yukassa_ip(client_ip):
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
            order_id = payment.get("metadata", {}).get("order_id", "")

            # ключ выдаём и по вебхуку, и по опросу сайта (идемпотентно)
            if order_id.startswith("web_"):
                await web_checkout.issue_web_license(order_id[4:])
            else:
                logger.warning(f"Webhook: платёж {payment_id} не с сайта (order_id={order_id!r}), пропущен")

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

    web_checkout.init(
        supabase=supabase, plans=PLANS,
        shop_id=YUKASSA_SHOP_ID, secret_key=YUKASSA_SECRET_KEY,
        site_url=SITE_URL, allowed_origins=ALLOWED_ORIGINS,
        generate_key=generate_key,
    )
    web_checkout.register(app)

    license_api.init(supabase=supabase)
    license_api.register(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {PORT}")

    asyncio.create_task(keep_alive())
    asyncio.create_task(deactivate_expired_licenses())

    await asyncio.Event().wait()  # работаем, пока Render не остановит процесс

if __name__ == "__main__":
    asyncio.run(main())
