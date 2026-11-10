# Чек-лист безопасности перед выкладкой

## 1. Supabase

- [x] RLS включён на `licenses` и `web_orders`.
- [x] Политики `anon`/`authenticated` удалены.
- [x] `revoke all` для `anon`, `authenticated`, `public`.
- [x] Проверка публичным ключом: `GET /rest/v1/licenses` и
      `/rest/v1/web_orders` возвращают **401**.
- [ ] В Render `SUPABASE_KEY` — secret/service_role (не `sb_publishable_`).
      Проверить: после RLS кабинет, оплата и активация работают.
- [ ] В Supabase Auth включено подтверждение email.
- [ ] Пароль минимум 8 символов (совпадает с формой регистрации).
- [ ] 2FA на аккаунте Supabase.

Проверка RLS в SQL Editor:

```sql
select relname, relrowsecurity
from pg_class
where relname in ('licenses', 'web_orders');

select schemaname, tablename, policyname, roles
from pg_policies
where tablename in ('licenses', 'web_orders');
```

Ожидается: `relrowsecurity = true`, список политик пуст.

## 2. Render / backend

- [x] `/api/checkout`, `/api/order`, `/api/account/*`, `/api/admin/*`
      проверяют Supabase access_token.
- [x] Покупка без входа → 401.
- [x] Сумма оплаты сверяется с планом.
- [x] Ключ привязывается к `user_id` покупателя.
- [x] Rate-limit: checkout 20/IP и 5/user в минуту; claim 15/user в минуту;
      license/check — по IP.
- [x] Security-заголовки и `Cache-Control: no-store` для `/api/*`.
- [x] Лимит тела запроса 64 КБ.
- [x] Полные лицензионные ключи не пишутся в логи.
- [x] Админ-действия логируются (email, действие, префикс ключа).
- [ ] `ADMIN_EMAILS` задан в Render и совпадает с владельцем.
- [ ] 2FA на Render.

## 3. Cloudflare Worker

- [x] Allowlist маршрутов API.
- [x] Allowlist Origin; чужой Origin → 403.
- [x] Только GET/HEAD/POST; остальные → 405.
- [x] Лимит тела 16 КБ (по `Content-Length` и фактическому размеру).
- [x] `X-Forwarded-For` перезаписывается реальным IP клиента.
- [x] Security-заголовки, `Cache-Control: no-store`.
- [ ] Задеплоить Worker и проверить: preflight с чужим Origin → 403,
      обычный запрос сайта → 200.
- [ ] 2FA на Cloudflare.

## 4. Сайт (GitHub Pages)

- [x] CSP и `Referrer-Policy` на `index.html` и `account.html`.
- [x] SRI (SHA-384) для `vendor/supabase.js`.
- [x] Email администратора удалён из фронтенда.
- [x] Данные админки не кэшируются в `sessionStorage`; кэш ключей
      очищается при выходе/истечении сессии.
- [x] Токен заказа удаляется из URL после сохранения.
- [x] Пароль минимум 8 символов.
- [x] Нет `target="_blank"` без `rel="noopener"`.
- [ ] Проверить страницы в браузере: нет ошибок CSP/SRI, вход и покупка
      работают.
- [ ] 2FA на GitHub-организации.

## 5. Windows-приложение

- [x] Лицензия проверяется по HTTPS на `voidtweaks.onrender.com`.
- [x] В коде нет Supabase service_role и других секретов.
- [x] Обновление: allowlist GitHub-релиза, обязательный SHA-256, лимит
      размера, повторная проверка хэша перед запуском.
- [ ] Подписать установщик сертификатом code signing (вне кода).
- [ ] Проверить, что в релизе опубликован `digest` (SHA-256) для
      `VOIDTWEAKSInstaller.exe`; без него обновление блокируется.

## 6. Перед деплоем

- [x] `python -m unittest discover -s tests -v` — 20/20.
- [x] `python -m py_compile bot.py web_checkout.py account_api.py admin_api.py license_api.py`.
- [x] `node --check` для Worker (ES module).
- [x] Секреты не добавлены в git; `.env` не коммитится.
- [ ] Просмотреть diff и убедиться, что нет отладочных обходов.
- [ ] Выкладка только после подтверждения владельца.

## 7. После деплоя (smoke)

1. Открыть сайт и кабинет, проверить консоль браузера (CSP/SRI).
2. Войти тестовым аккаунтом, открыть «Мои ключи».
3. Создать тестовый платёж и проверить возврат/статус (без реальной оплаты).
4. Проверить `/api/license/check` тестовым ключом.
5. Проверить, что гостевой checkout → 401.
6. Проверить, что чужой Origin на Worker → 403.
7. Убедиться, что в логах Render нет полных ключей.
