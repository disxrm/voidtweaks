"""
Личный кабинет: привязка лицензий к аккаунтам Supabase Auth.

Пользователь регистрируется/входит на сайте через Supabase Auth (email + пароль).
Сайт присылает access_token в заголовке Authorization: Bearer <token>.
Сервер проверяет токен через GoTrue (/auth/v1/user) и работает с таблицами
licenses / web_orders своим секретным ключом.

  GET  /api/account/keys   -> {"ok": true, "email": ..., "licenses": [...], "orders": [...]}
  POST /api/account/claim  -> {"ok": true, "license": {...}}
       body: {"token": "<токен заказа>"} или {"key": "XXXX-XXXX-XXXX-XXXX"}

Привязка идемпотентна: повторный claim того же заказа/ключа не меняет данные.
Чужой ключ/заказ привязать нельзя.
"""

import asyncio
import logging
import re

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

_supabase = None
_supabase_url = ""
_supabase_key = ""
_admin_emails = {"vit9aso2@gmail.com"}

KEY_RE = re.compile(r"^[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$")


def init(*, supabase, supabase_url, supabase_key, admin_emails=None):
    global _supabase, _supabase_url, _supabase_key, _admin_emails
    _supabase = supabase
    _supabase_url = (supabase_url or "").rstrip("/")
    _supabase_key = supabase_key or ""
    if admin_emails:
        emails = {str(email).strip().lower() for email in admin_emails if str(email).strip()}
        if emails:
            _admin_emails = emails


def is_admin(user: dict | None) -> bool:
    email = (user or {}).get("email") or ""
    return email.strip().lower() in _admin_emails


async def get_user(request: web.Request) -> dict | None:
    """Проверяет access_token Supabase и возвращает user или None."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token or not _supabase_url or not _supabase_key:
        return None

    url = _supabase_url + "/auth/v1/user"
    headers = {"apikey": _supabase_key, "Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status != 200:
                    logger.info(f"[account] Токен отклонён: HTTP {response.status}")
                    return None
                data = await response.json()
                return data if isinstance(data, dict) and data.get("id") else None
    except Exception as e:
        logger.warning(f"[account] Не удалось проверить токен: {e}")
        return None


def _unauthorized():
    return web.json_response({"error": "unauthorized"}, status=401)


def _server_error():
    return web.json_response({"error": "server_error"}, status=500)


def _safe_license(row: dict) -> dict:
    return {
        "license_key": row.get("license_key"),
        "plan": row.get("plan"),
        "expires_at": row.get("expires_at"),
        "is_active": row.get("is_active"),
        "hwid": row.get("hwid"),
        "created_at": row.get("created_at"),
    }


def _safe_order(row: dict) -> dict:
    return {
        "plan": row.get("plan"),
        "status": row.get("status"),
        "license_key": row.get("license_key"),
        "created_at": row.get("created_at"),
    }


def _sort_rows(rows: list[dict]) -> list[dict]:
    def key(row):
        value = row.get("created_at") or ""
        return str(value)
    try:
        return sorted(rows, key=key, reverse=True)
    except Exception:
        return rows


async def api_account_keys(request: web.Request):
    user = await get_user(request)
    if not user:
        return _unauthorized()

    uid = user["id"]
    try:
        licenses, orders = await asyncio.gather(
            asyncio.to_thread(
                lambda: _supabase.table("licenses").select("*").eq("user_id", uid).execute()
            ),
            asyncio.to_thread(
                lambda: _supabase.table("web_orders").select("*").eq("user_id", uid).execute()
            ),
        )
    except Exception as e:
        logger.error(f"[account] Ошибка чтения данных: {e}")
        return _server_error()

    lic_rows = _sort_rows(licenses.data or [])
    order_rows = _sort_rows(orders.data or [])

    return web.json_response({
        "ok": True,
        "email": user.get("email"),
        "is_admin": is_admin(user),
        "licenses": [_safe_license(row) for row in lic_rows],
        "orders": [_safe_order(row) for row in order_rows],
    })


async def _claim_token(uid: str, token: str):
    if len(token) > 100:
        return web.json_response({"error": "not_found"}, status=404)

    try:
        result = _supabase.table("web_orders").select("*").eq("token", token).execute()
    except Exception as e:
        logger.error(f"[account] Ошибка поиска заказа: {e}")
        return _server_error()

    if not result.data:
        return web.json_response({"error": "not_found"}, status=404)

    order = result.data[0]
    owner = (order.get("user_id") or "").strip()
    if owner and owner != uid:
        return web.json_response({"error": "already_claimed"}, status=409)

    license_row = None
    license_key = order.get("license_key")

    if license_key:
        try:
            lic = _supabase.table("licenses").select("*").eq(
                "license_key", license_key
            ).execute()
            if lic.data:
                license_row = lic.data[0]
        except Exception as e:
            logger.error(f"[account] Ошибка поиска лицензии: {e}")
            return _server_error()

        if license_row:
            lic_owner = (license_row.get("user_id") or "").strip()
            if lic_owner and lic_owner != uid:
                return web.json_response({"error": "already_claimed"}, status=409)

    # Привязываем заказ. Если лицензия уже есть — привязываем и её.
    try:
        if not owner:
            _supabase.table("web_orders").update({"user_id": uid}).eq(
                "token", token
            ).execute()
        if license_row and not (license_row.get("user_id") or "").strip():
            _supabase.table("licenses").update({"user_id": uid}).eq(
                "license_key", license_row["license_key"]
            ).execute()
            license_row["user_id"] = uid
    except Exception as e:
        logger.error(f"[account] Ошибка привязки заказа: {e}")
        return _server_error()

    return web.json_response({
        "ok": True,
        "pending": not bool(license_row),
        "license": _safe_license(license_row) if license_row else None,
    })


async def _claim_key(uid: str, key: str):
    if not KEY_RE.match(key):
        return web.json_response({"error": "bad_key"}, status=400)

    try:
        result = _supabase.table("licenses").select("*").eq(
            "license_key", key
        ).execute()
    except Exception as e:
        logger.error(f"[account] Ошибка поиска ключа: {e}")
        return _server_error()

    if not result.data:
        return web.json_response({"error": "not_found"}, status=404)

    row = result.data[0]
    owner = (row.get("user_id") or "").strip()
    if owner and owner != uid:
        return web.json_response({"error": "already_claimed"}, status=409)

    try:
        if not owner:
            _supabase.table("licenses").update({"user_id": uid}).eq(
                "license_key", key
            ).execute()
            row["user_id"] = uid

        # Если по этому ключу есть заказ — привязываем и его, чтобы он был
        # в истории покупок. Ошибка здесь не критична.
        try:
            linked = _supabase.table("web_orders").select(
                "token,user_id"
            ).eq("license_key", key).execute()
            for order in linked.data or []:
                if not (order.get("user_id") or "").strip():
                    _supabase.table("web_orders").update(
                        {"user_id": uid}
                    ).eq("token", order["token"]).execute()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[account] Ошибка привязки ключа: {e}")
        return _server_error()

    return web.json_response({"ok": True, "license": _safe_license(row)})


async def api_account_claim(request: web.Request):
    user = await get_user(request)
    if not user:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "bad_json"}, status=400)

    token = str(body.get("token") or "").strip()
    key = str(body.get("key") or "").strip().upper()

    if token:
        return await _claim_token(user["id"], token)
    if key:
        return await _claim_key(user["id"], key)
    return web.json_response({"error": "bad_request"}, status=400)


def register(app: web.Application):
    app.router.add_get("/api/account/keys", api_account_keys)
    app.router.add_post("/api/account/claim", api_account_claim)
