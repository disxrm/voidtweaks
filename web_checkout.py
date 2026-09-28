"""
Веб-оплата для VoidTweaks: сайт -> ЮKassa -> ключ на странице (без Telegram).

Подключение в bot.py — см. README_WEB_PAYMENT.md.

Как это работает:
  1. Сайт:  POST /api/checkout {"plan": "month"}
            -> создаём платёж в ЮKassa, кладём заказ в таблицу web_orders,
               отдаём {"url": <страница оплаты>, "token": <секретный токен заказа>}
  2. Пользователь платит на стороне ЮKassa и возвращается на
            https://<сайт>/?order=<token>
  3. Сайт:  GET /api/order?token=...   (опрос каждые 3 сек)
            -> {"status": "pending" | "canceled" | "succeeded", "key": ..., ...}
  4. Ключ выдаётся ОДИН раз, идемпотентно, из двух мест — из вебхука ЮKassa
     и из /api/order (если вебхук опоздал или не дошёл). Дубля не будет.
"""

import asyncio
import base64
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# Эти значения подставляются из bot.py через init(...)
_supabase = None
_plans = {}
_shop_id = ""
_secret_key = ""
_site_url = ""
_allowed_origins: set[str] = set()
_generate_key = None

# Защита от гонки: вебхук и /api/order могут прийти одновременно
_issue_locks: dict[str, asyncio.Lock] = {}

# Простой rate-limit на создание платежей: ip -> список временных меток
_checkout_hits: dict[str, list[float]] = {}
CHECKOUT_LIMIT = 5        # запросов
CHECKOUT_WINDOW = 60      # секунд


def init(*, supabase, plans, shop_id, secret_key, site_url, allowed_origins, generate_key):
    global _supabase, _plans, _shop_id, _secret_key, _site_url, _allowed_origins, _generate_key
    _supabase = supabase
    _plans = plans
    _shop_id = shop_id
    _secret_key = secret_key
    _site_url = site_url.rstrip("/")
    _allowed_origins = {o.rstrip("/") for o in allowed_origins if o}
    _generate_key = generate_key


# ---------------------------------------------------------------- CORS ----

@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin", "").rstrip("/")
    allowed = origin in _allowed_origins

    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e

    if allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
        resp.headers["Vary"] = "Origin"
    return resp


# ------------------------------------------------------------- ЮKassa -----

def _auth_header() -> dict:
    cred = base64.b64encode(f"{_shop_id}:{_secret_key}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


async def _yk_create(amount: int, token: str, plan_key: str, description: str):
    headers = {
        **_auth_header(),
        "Content-Type": "application/json",
        "Idempotence-Key": str(uuid.uuid4()),
    }
    body = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"{_site_url}/?order={token}",
        },
        "description": description,
        # префикс web_ — чтобы вебхук отличал веб-заказы от Telegram-заказов
        "metadata": {"order_id": f"web_{token}", "plan": plan_key},
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.yookassa.ru/v3/payments",
                json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
                if r.status != 200:
                    logger.error(f"ЮKassa create {r.status}: {data}")
                    return None, None
                return data.get("id"), data.get("confirmation", {}).get("confirmation_url")
    except Exception as e:
        logger.error(f"ЮKassa create error: {e}")
        return None, None


async def _yk_status(payment_id: str):
    """Возвращает (status, paid_amount). Статус берём ТОЛЬКО из API ЮKassa."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.yookassa.ru/v3/payments/{payment_id}",
                headers=_auth_header(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
                if r.status != 200:
                    logger.error(f"ЮKassa status {r.status}: {data}")
                    return None, None
                amount = data.get("amount", {}).get("value")
                return data.get("status"), amount
    except Exception as e:
        logger.error(f"ЮKassa status error: {e}")
        return None, None


# --------------------------------------------------------- выдача ключа ---

def _row_to_response(row: dict) -> dict:
    return {
        "status": "succeeded",
        "key": row["license_key"],
        "plan": row["plan"],
        "expires_at": row.get("expires_at"),
    }


async def issue_web_license(token: str) -> dict | None:
    """
    Идемпотентно выдаёт ключ по токену веб-заказа.
    Возвращает {"key","plan","expires_at"} или None, если платёж не оплачен.
    Безопасно вызывать сколько угодно раз и параллельно.
    """
    lock = _issue_locks.setdefault(token, asyncio.Lock())
    async with lock:
        order = _supabase.table("web_orders").select("*").eq("token", token).execute()
        if not order.data:
            return None
        order = order.data[0]

        # ============================================================
        # ВРЕМЕННЫЙ БЛОК ДЛЯ ТЕСТИРОВАНИЯ.
        # Позволяет выдать ключ, если в web_orders уже стоит succeeded
        # и license_key заполнен вручную через Supabase.
        # УДАЛИТЬ ПОСЛЕ ТЕСТА, ИНАЧЕ ЛЮБОЙ СМОЖЕТ ПОДДЕЛАТЬ ОПЛАТУ,
        # ИЗМЕНИВ ЗАПИСЬ В БАЗЕ.
        # ============================================================
        if order.get("status") == "succeeded" and order.get("license_key"):
            logger.info(f"[TEST] Выдача ключа из БД по заказу {token}")
            return {
                "status": "succeeded",
                "key": order["license_key"],
                "plan": order["plan"],
                "expires_at": None,
            }
        # ============================================================
        # КОНЕЦ ВРЕМЕННОГО БЛОКА
        # ============================================================

        # уже выдан — просто отдаём тот же ключ
        if order.get("license_key"):
            lic = _supabase.table("licenses").select("*").eq(
                "license_key", order["license_key"]
            ).execute()
            if lic.data:
                return _row_to_response(lic.data[0])

        # не доверяем ни вебхуку, ни клиенту — спрашиваем ЮKassa напрямую
        status, paid = await _yk_status(order["payment_id"])
        if status != "succeeded":
            return None

        plan_key = order["plan"]
        plan = _plans.get(plan_key)
        if not plan:
            logger.error(f"Неизвестный план в заказе {token}: {plan_key}")
            return None

        # сумма должна совпасть с ценой тарифа (защита от подмены)
        try:
            if int(float(paid)) != int(plan["price"]):
                logger.error(f"Сумма не совпала: оплачено {paid}, ждали {plan['price']} (заказ {token})")
                return None
        except (TypeError, ValueError):
            return None

        key = _generate_key()
        expires = None if plan_key == "forever" else (
            datetime.now(timezone.utc) + timedelta(days=plan["days"])
        )
        row = {
            "license_key": key,
            "telegram_id": None,            # веб-покупатель без Telegram
            "plan": plan_key,
            "payment_id": order["payment_id"],
            "expires_at": expires.isoformat() if expires else None,
            "is_active": True,
        }
        try:
            _supabase.table("licenses").insert(row).execute()
        except Exception as e:
            # если уникальный индекс по payment_id сработал — ключ уже есть
            existing = _supabase.table("licenses").select("*").eq(
                "payment_id", order["payment_id"]
            ).execute()
            if existing.data:
                row = existing.data[0]
                key = row["license_key"]
            else:
                logger.error(f"Не удалось записать лицензию (заказ {token}): {e}")
                return None

        _supabase.table("web_orders").update({
            "license_key": key,
            "status": "succeeded",
        }).eq("token", token).execute()

        logger.info(f"[web] Ключ {key} выдан по заказу {token}, план {plan_key}")
        return {"key": key, "plan": plan_key,
                "expires_at": row.get("expires_at"), "status": "succeeded"}


# ----------------------------------------------------------- эндпоинты ----

def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote) or "?"


def _rate_limited(ip: str) -> bool:
    now = datetime.now().timestamp()
    hits = [t for t in _checkout_hits.get(ip, []) if now - t < CHECKOUT_WINDOW]
    if len(hits) >= CHECKOUT_LIMIT:
        _checkout_hits[ip] = hits
        return True
    hits.append(now)
    _checkout_hits[ip] = hits
    return False


async def api_checkout(request: web.Request):
    if _rate_limited(_client_ip(request)):
        return web.json_response({"error": "too_many_requests"}, status=429)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)

    plan_key = body.get("plan")
    plan = _plans.get(plan_key)
    if not plan:
        return web.json_response({"error": "bad_plan"}, status=400)

    token = secrets.token_urlsafe(24)  # ~192 бита — не подобрать перебором
    payment_id, url = await _yk_create(
        plan["price"], token, plan_key, f"VoidTweaks — {plan['name']}"
    )
    if not payment_id or not url:
        return web.json_response({"error": "payment_failed"}, status=502)

    try:
        _supabase.table("web_orders").insert({
            "token": token,
            "plan": plan_key,
            "payment_id": payment_id,
            "status": "pending",
        }).execute()
    except Exception as e:
        logger.error(f"Не удалось сохранить web_order: {e}")
        return web.json_response({"error": "db_failed"}, status=500)

    return web.json_response({"url": url, "token": token})


async def api_order(request: web.Request):
    token = request.query.get("token", "")
    if not token or len(token) > 100:
        return web.json_response({"error": "not_found"}, status=404)

    res = _supabase.table("web_orders").select("*").eq("token", token).execute()
    if not res.data:
        return web.json_response({"error": "not_found"}, status=404)
    order = res.data[0]

    # ключ уже есть -> отдаём
    result = await issue_web_license(token)
    if result:
        return web.json_response(result)

    # ключа нет: pending или canceled?
    status, _ = await _yk_status(order["payment_id"])
    if status == "canceled":
        _supabase.table("web_orders").update({"status": "canceled"}).eq("token", token).execute()
        return web.json_response({"status": "canceled"})
    if status == "succeeded":
        # деньги пришли, но ключ выдать не смогли (например, не совпала сумма)
        return web.json_response({"status": "error"}, status=409)
    return web.json_response({"status": "pending"})


def register(app: web.Application):
    app.middlewares.append(cors_middleware)
    app.router.add_post("/api/checkout", api_checkout)
    app.router.add_get("/api/order", api_order)
    app.router.add_options("/api/checkout", lambda r: web.Response(status=204))
    app.router.add_options("/api/order", lambda r: web.Response(status=204))
