"""
Проверка и активация лицензий для программы VoidTweaks.

Раньше программа ходила в Supabase напрямую публичным ключом, а он открывал
всю таблицу licenses (чтение всех ключей и правка любой записи). Теперь
программа знает только этот эндпоинт, а в базу ходит сервер своим ключом.

  POST /api/license/check
       {"key": "XXXX-XXXX-XXXX-XXXX", "hwid": "...", "compatible": ["...", ...]}
    -> 200 {"ok": true, "plan": ..., "expires_at": ...}
    -> 200 {"ok": false, "error": "not_found" | "inactive" | "expired" | "other_device"}
    -> 400 bad_request, 429 too_many_requests, 500 server_error

Один и тот же вызов и активирует ключ (привязывает к ПК), и периодически
перепроверяет уже активированный.
"""

import logging
import re
from datetime import datetime, timezone

from aiohttp import web

logger = logging.getLogger(__name__)

_supabase = None

KEY_RE = re.compile(r"^[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$")
MAX_HWID_LEN = 200
MAX_COMPATIBLE = 16

# Перебор ключей: не больше CHECK_LIMIT запросов с одного IP за CHECK_WINDOW секунд
_check_hits: dict[str, list[float]] = {}
CHECK_LIMIT = 10
CHECK_WINDOW = 60


def init(*, supabase):
    global _supabase
    _supabase = supabase


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote) or "?"


def _rate_limited(ip: str) -> bool:
    now = datetime.now().timestamp()
    hits = [t for t in _check_hits.get(ip, []) if now - t < CHECK_WINDOW]
    limited = len(hits) >= CHECK_LIMIT
    if not limited:
        hits.append(now)
    _check_hits[ip] = hits
    return limited


def _parse_expires(value) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def api_license_check(request: web.Request):
    if _rate_limited(_client_ip(request)):
        return web.json_response({"ok": False, "error": "too_many_requests"}, status=429)

    try:
        body = await request.json()
        key = str(body.get("key", "")).strip().upper()
        hwid = str(body.get("hwid", "")).strip()
        compatible = body.get("compatible") or []
        if not isinstance(compatible, list):
            raise ValueError("compatible must be a list")
        compatible = {str(h).strip().lower() for h in compatible[:MAX_COMPATIBLE] if h}
    except Exception:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    if not KEY_RE.match(key) or not hwid or len(hwid) > MAX_HWID_LEN:
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    try:
        res = _supabase.table("licenses").select(
            "license_key, plan, expires_at, is_active, hwid"
        ).eq("license_key", key).execute()
    except Exception as e:
        logger.error(f"[license] Ошибка чтения лицензии: {e}")
        return web.json_response({"ok": False, "error": "server_error"}, status=500)

    if not res.data:
        return web.json_response({"ok": False, "error": "not_found"})
    lic = res.data[0]

    if not lic.get("is_active"):
        return web.json_response({"ok": False, "error": "inactive"})

    expires = _parse_expires(lic.get("expires_at"))
    if expires and expires < datetime.now(timezone.utc):
        return web.json_response({"ok": False, "error": "expired"})

    # Привязка к ПК: пустая — привязываем; совпадает с текущим или с одним из
    # «совместимых» HWID этого же ПК (старые форматы) — перепривязываем на текущий.
    bound = (lic.get("hwid") or "").strip()
    if bound and bound.lower() != hwid.lower() and bound.lower() not in compatible:
        return web.json_response({"ok": False, "error": "other_device"})

    if bound.lower() != hwid.lower():
        try:
            _supabase.table("licenses").update({"hwid": hwid}).eq("license_key", key).execute()
            logger.info(f"[license] Ключ {key[:4]}-… привязан к устройству")
        except Exception as e:
            logger.error(f"[license] Не удалось привязать HWID: {e}")
            return web.json_response({"ok": False, "error": "server_error"}, status=500)

    return web.json_response({
        "ok": True,
        "plan": lic.get("plan"),
        "expires_at": expires.isoformat() if expires else None,
    })


def register(app: web.Application):
    app.router.add_post("/api/license/check", api_license_check)
