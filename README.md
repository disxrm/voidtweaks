# VOID TWEAKS переехал

Проект теперь живёт в организации **[voidtweaksapp](https://github.com/voidtweaksapp)**:

- Репозиторий и релизы: **https://github.com/voidtweaksapp/voidtweaks**
- Сайт: **https://voidtweaksapp.github.io/voidtweaks/**

Релизы в этом репозитории оставлены для совместимости со старыми версиями программы.

`bot.py` и `web_checkout.py` — сервер оплаты PRO для сайта (Render), он собирается из этого репозитория.
Telegram-бота больше нет: `bot.py` — только веб-сервер (имя оставлено ради команды запуска на Render).

Переменные окружения на Render: `SUPABASE_URL`, `SUPABASE_KEY`, `YUKASSA_SHOP_ID`, `YUKASSA_SECRET_KEY`,
`SITE_URL`, `RENDER_URL`, `ADMIN_EMAILS` (и по желанию `ALLOWED_ORIGINS`). `BOT_TOKEN` больше не нужен.

**Важно:** `SUPABASE_KEY` должен быть secret/service_role. Публичный
`sb_publishable_...` не имеет доступа к `licenses`/`web_orders` после
включения RLS. Перед выкладкой см. `SECURITY_CHECKLIST.md`; описание
защиты — `SECURITY.md`, модель угроз — `THREAT_MODEL.md`, отчёт аудита —
`SECURITY_AUDIT_2026-11.md`.
