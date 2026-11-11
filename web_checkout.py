"""
Веб-оплата для VoidTweaks: сайт -> ЮKassa -> ключ на странице.

Подключается в bot.py через init(...) и register(app).

Как это работает:
  1. Сайт:  POST /api/checkout {"plan": "month"}
            -> создаём платёж в ЮKassa, кладём заказ в таблицу web_orders,
               отдаём {"url": <страница оплаты>, "token": <секретный токен заказа>}
  2. Пользователь платит на стороне ЮKassa и возвращается на
            https://<сайт>/?order=<token>
  3. Сайт:  GET /api/order?token=...   (опрос каждые 3 сек)
            -> {"status": "pending" | "canceled" | "succeeded", "key": ..., ...}
  4. Ключ выдаётся ОДИН раз, идемпотентно: из /api/order, а также при
     сверке в кабинете и админке, если покупатель закрыл вкладку.
"""

import asyncio
import base64
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

import account_api

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

# Простой rate-limit: ключ (ip/user) -> список временных меток.
# IP-лимит защищает от анонимного флуда, user-лимит — от спама заказами.
_checkout_hits: dict[str, list[float]] = {}
CHECKOUT_IP_LIMIT = 20
CHECKOUT_USER_LIMIT = 5
ORDER_IP_LIMIT = 60
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
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
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
        # префикс web_ — по нему можно отличить заказы сайта
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

        # уже выдан — просто отдаём тот же ключ
        if order.get("license_key"):
            lic = _supabase.table("licenses").select("*").eq(
                "license_key", order["license_key"]
            ).execute()
            if lic.data:
                lic_row = lic.data[0]
                # заказ могли привязать к аккаунту уже после выдачи ключа
                if order.get("user_id") and not (lic_row.get("user_id") or "").strip():
                    try:
                        _supabase.table("licenses").update(
                            {"user_id": order["user_id"]}
                        ).eq("license_key", lic_row["license_key"]).execute()
                        lic_row["user_id"] = order["user_id"]
                    except Exception as e:
                        logger.warning(f"Не удалось привязать ключ к аккаунту: {e}")
                return _row_to_response(lic_row)

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
            "telegram_id": None,            # колонка осталась от Telegram-бота
            "plan": plan_key,
            "payment_id": order["payment_id"],
            "expires_at": expires.isoformat() if expires else None,
            "is_active": True,
        }
        if order.get("user_id"):
            row["user_id"] = order["user_id"]

        def _insert_license(data: dict):
            _supabase.table("licenses").insert(data).execute()

        try:
            _insert_license(row)
        except Exception as e:
            # если уникальный индекс по payment_id сработал — ключ уже есть
            existing = _supabase.table("licenses").select("*").eq(
                "payment_id", order["payment_id"]
            ).execute()
            if existing.data:
                row = existing.data[0]
                key = row["license_key"]
                if order.get("user_id") and not (row.get("user_id") or "").strip():
                    try:
                        _supabase.table("licenses").update(
                            {"user_id": order["user_id"]}
                        ).eq("license_key", key).execute()
                        row["user_id"] = order["user_id"]
                    except Exception as e:
                        logger.warning(f"Не удалось привязать существующий ключ: {e}")
            elif "user_id" in row:
                # миграция с user_id ещё не применена — выдаём ключ без привязки
                logger.warning(f"Повторная вставка лицензии без user_id: {e}")
                row.pop("user_id", None)
                try:
                    _insert_license(row)
                except Exception as e2:
                    logger.error(f"Не удалось записать лицензию (заказ {token}): {e2}")
                    return None
            else:
                logger.error(f"Не удалось записать лицензию (заказ {token}): {e}")
                return None

        _supabase.table("web_orders").update({
            "license_key": key,
            "status": "succeeded",
        }).eq("token", token).execute()

        logger.info(f"[web] Ключ {key[:4]}-… выдан по заказу {token}, план {plan_key}")
        return {"key": key, "plan": plan_key,
                "expires_at": row.get("expires_at"), "status": "succeeded"}


# --------------------------------------------------- сверка заказов -------

# Насколько старые pending-заказы имеет смысл перепроверять
RECONCILE_MAX_AGE = timedelta(hours=48)


def _parse_created(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def reconcile_pending_orders(user_id: str | None = None, limit: int = 3) -> int:
    """
    Догоняет выдачу ключей: если покупатель оплатил и закрыл вкладку,
    ключ всё равно появится при следующем заходе в кабинет (или в админку).
    Проверяет только свежие pending-заказы и не более limit за раз.
    """
    try:
        query = (
            _supabase.table("web_orders")
            .select("token,payment_id,created_at,user_id")
            .eq("status", "pending")
            .order("created_at", desc=True)
        )
        if user_id:
            query = query.eq("user_id", user_id)
        rows = query.execute().data or []
    except Exception as e:
        logger.warning(f"[web] Сверка заказов: не удалось прочитать список: {e}")
        return 0

    now = datetime.now(timezone.utc)
    checked = 0
    issued = 0
    for row in rows:
        if checked >= limit:
            break
        created = _parse_created(row.get("created_at"))
        if created and now - created > RECONCILE_MAX_AGE:
            continue

        token = str(row.get("token") or "")
        if not token:
            continue
        checked += 1

        try:
            result = await issue_web_license(token)
        except Exception as e:
            logger.warning(f"[web] Сверка заказа {token[:6]}…: ошибка выдачи: {e}")
            continue
        if result:
            issued += 1
            continue

        # ключа нет: возможно, платёж отменён или истёк — закрываем заказ
        status, _ = await _yk_status(str(row.get("payment_id") or ""))
        if status == "canceled":
            try:
                _supabase.table("web_orders").update(
                    {"status": "canceled"}
                ).eq("token", token).execute()
            except Exception as e:
                logger.warning(f"[web] Не удалось пометить заказ {token[:6]}… отменённым: {e}")

    if issued:
        logger.info(f"[web] Сверка заказов: выдано ключей — {issued}")
    return issued


# ----------------------------------------------------------- эндпоинты ----

def _client_ip(request: web.Request) -> str:
    # Worker передаёт реальный IP клиента в X-Forwarded-For. При прямом
    # обращении к Render заголовок может быть подделан, но это лишь
    # эвристика для rate-limit, а не доверенная авторизация.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return (fwd.split(",")[0].strip() or "?")[:64]
    return (request.headers.get("CF-Connecting-IP", "") or request.remote or "?")[:64]


def _rate_limited(key: str, limit: int = CHECKOUT_USER_LIMIT) -> bool:
    now = datetime.now().timestamp()
    hits = [t for t in _checkout_hits.get(key, []) if now - t < CHECKOUT_WINDOW]
    if len(hits) >= limit:
        _checkout_hits[key] = hits
        return True
    hits.append(now)
    _checkout_hits[key] = hits
    if len(_checkout_hits) > 5000:
        for old_key in [k for k, v in _checkout_hits.items() if not v or now - v[-1] > 3600]:
            _checkout_hits.pop(old_key, None)
    return False


async def api_checkout(request: web.Request):
    if _rate_limited("ip:" + _client_ip(request), CHECKOUT_IP_LIMIT):
        return web.json_response(
            {"error": "too_many_requests"},
            status=429,
            headers={"Retry-After": str(CHECKOUT_WINDOW)},
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "bad_json"}, status=400)

    plan_key = body.get("plan")
    plan = _plans.get(plan_key)
    if not plan:
        return web.json_response({"error": "bad_plan"}, status=400)

    # Покупка доступна только авторизованным пользователям: ключ сразу
    # привязывается к аккаунту, а не теряется после оплаты.
    user = await account_api.get_user(request)
    if not user or not user.get("id"):
        return web.json_response({"error": "unauthorized"}, status=401)

    if _rate_limited("user:" + str(user["id"]), CHECKOUT_USER_LIMIT):
        return web.json_response(
            {"error": "too_many_requests"},
            status=429,
            headers={"Retry-After": str(CHECKOUT_WINDOW)},
        )

    token = secrets.token_urlsafe(24)  # ~192 бита — не подобрать перебором
    payment_id, url = await _yk_create(
        plan["price"], token, plan_key, f"VoidTweaks — {plan['name']}"
    )
    if not payment_id or not url:
        return web.json_response({"error": "payment_failed"}, status=502)

    order_row = {
        "token": token,
        "plan": plan_key,
        "payment_id": payment_id,
        "status": "pending",
        "user_id": user["id"],
    }

    try:
        _supabase.table("web_orders").insert(order_row).execute()
    except Exception as e:
        logger.error(f"Не удалось сохранить web_order: {e}")
        return web.json_response({"error": "db_failed"}, status=500)

    return web.json_response({"url": url, "token": token})


async def api_order(request: web.Request):
    if _rate_limited("order:" + _client_ip(request), ORDER_IP_LIMIT):
        return web.json_response(
            {"error": "too_many_requests"},
            status=429,
            headers={"Retry-After": str(CHECKOUT_WINDOW)},
        )

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
