"""
Админ-панель VOIDTWEAKS.

Доступ только для email из ADMIN_EMAILS. Все данные читаются/меняются
через service-role ключ Supabase, поэтому каждый запрос сначала проверяет
access_token пользователя и его email.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

import account_api

logger = logging.getLogger(__name__)

_supabase = None
_supabase_url = ""
_supabase_key = ""
_plans = {}

PAGE_SIZE = 1000
MAX_PAGES = 10


def init(*, supabase, supabase_url, supabase_key, plans):
    global _supabase, _supabase_url, _supabase_key, _plans
    _supabase = supabase
    _supabase_url = (supabase_url or "").rstrip("/")
    _supabase_key = supabase_key or ""
    _plans = plans or {}


def _unauthorized():
    return web.json_response({"error": "unauthorized"}, status=401)


def _forbidden():
    return web.json_response({"error": "forbidden"}, status=403)


def _server_error():
    return web.json_response({"error": "server_error"}, status=500)


async def _admin_user(request: web.Request) -> dict | None:
    user = await account_api.get_user(request)
    if not user:
        return None
    if not account_api.is_admin(user):
        return None
    return user


async def _is_admin_request(request: web.Request) -> tuple[bool, dict | None]:
    user = await account_api.get_user(request)
    if not user:
        return False, None
    if not account_api.is_admin(user):
        return False, user
    return True, user


def _parse_dt(value):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_current_license(row: dict) -> bool:
    if row.get("is_active") is False:
        return False
    expires = _parse_dt(row.get("expires_at"))
    return not expires or expires > datetime.now(timezone.utc)


def _sort_rows(rows: list[dict]) -> list[dict]:
    def key(row):
        return str(row.get("created_at") or "")

    try:
        return sorted(rows, key=key, reverse=True)
    except Exception:
        return rows


async def _fetch_auth_users() -> list[dict]:
    if not _supabase_url or not _supabase_key:
        return []

    users = []
    headers = {
        "apikey": _supabase_key,
        "Authorization": f"Bearer {_supabase_key}",
    }
    async with aiohttp.ClientSession() as session:
        for page in range(1, MAX_PAGES + 1):
            url = (
                f"{_supabase_url}/auth/v1/admin/users"
                f"?page={page}&per_page={PAGE_SIZE}"
            )
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"auth admin HTTP {response.status}")
                data = await response.json()
            batch = data.get("users", []) if isinstance(data, dict) else []
            users.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < PAGE_SIZE:
                break
    return users


def _user_view(row: dict, license_count: int, order_count: int, spent: int) -> dict:
    return {
        "id": row.get("id"),
        "email": row.get("email"),
        "created_at": row.get("created_at"),
        "last_sign_in_at": row.get("last_sign_in_at"),
        "license_count": license_count,
        "order_count": order_count,
        "spent": spent,
    }


def _license_view(row: dict, email: str = "") -> dict:
    return {
        "license_key": row.get("license_key"),
        "plan": row.get("plan"),
        "expires_at": row.get("expires_at"),
        "is_active": row.get("is_active"),
        "hwid": row.get("hwid"),
        "created_at": row.get("created_at"),
        "user_id": row.get("user_id"),
        "email": email,
    }


def _order_view(row: dict, email: str = "") -> dict:
    return {
        "plan": row.get("plan"),
        "status": row.get("status"),
        "license_key": row.get("license_key"),
        "created_at": row.get("created_at"),
        "user_id": row.get("user_id"),
        "email": email,
    }


def _plan_price(plan: str | None) -> int:
    try:
        return int((_plans.get(plan) or {}).get("price") or 0)
    except Exception:
        return 0


def _stats(licenses: list[dict], orders: list[dict], users: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    active = sum(1 for row in licenses if _is_current_license(row))
    claimed = sum(1 for row in licenses if (row.get("user_id") or "").strip())
    revenue = 0
    revenue_month = 0
    for order in orders:
        if order.get("status") != "succeeded":
            continue
        price = _plan_price(order.get("plan"))
        revenue += price
        created = _parse_dt(order.get("created_at"))
        if created and created >= month_start:
            revenue_month += price

    return {
        "users": len(users),
        "licenses": len(licenses),
        "active_licenses": active,
        "claimed_licenses": claimed,
        "orders": len(orders),
        "pending_orders": sum(1 for row in orders if row.get("status") == "pending"),
        "revenue": revenue,
        "revenue_month": revenue_month,
    }


async def api_admin_data(request: web.Request):
    ok, user = await _is_admin_request(request)
    if not user:
        return _unauthorized()
    if not ok:
        return _forbidden()

    users_task = asyncio.create_task(_fetch_auth_users())
    try:
        licenses, orders = await asyncio.gather(
            asyncio.to_thread(
                lambda: _sort_rows(
                    _supabase.table("licenses").select("*").execute().data or []
                )
            ),
            asyncio.to_thread(
                lambda: _sort_rows(
                    _supabase.table("web_orders").select("*").execute().data or []
                )
            ),
        )
    except Exception as exc:
        users_task.cancel()
        logger.error(f"[admin] Ошибка чтения данных: {exc}")
        return _server_error()

    users_error = False
    try:
        auth_users = await users_task
    except Exception as exc:
        logger.warning(f"[admin] Не удалось получить пользователей: {exc}")
        auth_users = []
        users_error = True

    by_id = {str(row.get("id")): row for row in auth_users if row.get("id")}
    email_by_id = {
        uid: (row.get("email") or "") for uid, row in by_id.items()
    }

    license_counts = {}
    order_counts = {}
    spent = {}
    for row in licenses:
        uid = str(row.get("user_id") or "")
        if uid:
            license_counts[uid] = license_counts.get(uid, 0) + 1
    for row in orders:
        uid = str(row.get("user_id") or "")
        if uid:
            order_counts[uid] = order_counts.get(uid, 0) + 1
            if row.get("status") == "succeeded":
                spent[uid] = spent.get(uid, 0) + _plan_price(row.get("plan"))

    users = [
        _user_view(
            row,
            license_counts.get(str(row.get("id")), 0),
            order_counts.get(str(row.get("id")), 0),
            spent.get(str(row.get("id")), 0),
        )
        for row in auth_users
    ]
    users.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)

    return web.json_response({
        "ok": True,
        "stats": _stats(licenses, orders, auth_users),
        "licenses": [
            _license_view(row, email_by_id.get(str(row.get("user_id")), ""))
            for row in licenses
        ],
        "orders": [
            _order_view(row, email_by_id.get(str(row.get("user_id")), ""))
            for row in orders
        ],
        "users": users,
        "users_error": users_error,
    })


async def api_admin_license(request: web.Request):
    ok, user = await _is_admin_request(request)
    if not user:
        return _unauthorized()
    if not ok:
        return _forbidden()

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "bad_json"}, status=400)

    key = str(body.get("license_key") or "").strip().upper()
    action = str(body.get("action") or "").strip()
    if not account_api.KEY_RE.match(key):
        return web.json_response({"error": "bad_key"}, status=400)
    if action not in {"toggle", "extend", "unbind"}:
        return web.json_response({"error": "bad_action"}, status=400)

    try:
        result = _supabase.table("licenses").select("*").eq(
            "license_key", key
        ).execute()
    except Exception as exc:
        logger.error(f"[admin] Ошибка поиска ключа: {exc}")
        return _server_error()

    if not result.data:
        return web.json_response({"error": "not_found"}, status=404)

    row = result.data[0]
    update = {}
    if action == "toggle":
        update["is_active"] = not bool(row.get("is_active"))
    elif action == "unbind":
        update["hwid"] = None
    else:
        expires = _parse_dt(row.get("expires_at"))
        if not expires:
            return web.json_response({"error": "forever"}, status=400)
        base = max(expires, datetime.now(timezone.utc))
        update["expires_at"] = (base + timedelta(days=30)).isoformat()
        update["is_active"] = True

    try:
        _supabase.table("licenses").update(update).eq(
            "license_key", key
        ).execute()
    except Exception as exc:
        logger.error(f"[admin] Ошибка обновления ключа: {exc}")
        return _server_error()

    admin_email = str(user.get("email") or "?").replace("\n", " ").replace("\r", " ")[:200]
    logger.info(
        f"[admin] {admin_email} -> {action} "
        f"{key[:4]}-… (is_active={update.get('is_active', row.get('is_active'))})"
    )

    row.update(update)
    email = ""
    try:
        uid = str(row.get("user_id") or "")
        if uid:
            users = await _fetch_auth_users()
            by_id = {str(item.get("id")): item for item in users if item.get("id")}
            email = (by_id.get(uid) or {}).get("email") or ""
    except Exception:
        pass

    return web.json_response({
        "ok": True,
        "license": _license_view(row, email),
    })


def register(app: web.Application):
    app.router.add_get("/api/admin/data", api_admin_data)
    app.router.add_post("/api/admin/license", api_admin_license)
