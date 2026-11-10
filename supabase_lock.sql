-- Закрывает таблицы лицензий от публичного (publishable / anon) ключа.
-- Выполнить ОДИН раз в Supabase: Project → SQL Editor → New query → Run.
--
-- ПЕРЕД запуском:
--   1. На Render переменная SUPABASE_KEY должна быть СЕКРЕТНЫМ ключом
--      (Project Settings → API Keys: sb_secret_... или старый service_role).
--      Секретный ключ обходит эти ограничения, публичный — нет: с публичным
--      ключом после этого скрипта перестанут работать и оплата, и активация.
--   2. Сервер с /api/license/check уже задеплоен, а VoidTweaks 1.0.3 выпущен:
--      1.0.3 проверяет лицензию через сервер, а не через базу напрямую.

-- 1. Включаем Row Level Security: без разрешающих правил публичный ключ не видит ни строки.
alter table public.licenses   enable row level security;
alter table public.web_orders enable row level security;

-- 2. Удаляем все существующие правила (policies) на этих таблицах:
--    именно они сейчас открывают таблицы публичному ключу.
do $$
declare p record;
begin
  for p in
    select policyname, tablename from pg_policies
    where schemaname = 'public' and tablename in ('licenses', 'web_orders')
  loop
    execute format('drop policy %I on public.%I', p.policyname, p.tablename);
  end loop;
end $$;

-- 3. Забираем у публичных ролей сами права на таблицы.
--    ВАЖНО: public — это псевдороль, которая включает anon и authenticated.
--    Без этого revoke таблица может остаться читаемой, даже если RLS включён,
--    но в ней остались старые policy.
revoke all on table public.licenses, public.web_orders from anon, authenticated;
revoke all on table public.licenses, public.web_orders from public;

-- 4. Проверка: политик должно быть 0, RLS должен быть включён.
select relname, relrowsecurity, relforcerowsecurity
from pg_class
where relname in ('licenses', 'web_orders');

select grantee, privilege_type
from information_schema.role_table_grants
where table_name in ('licenses', 'web_orders')
  and grantee in ('anon', 'authenticated', 'PUBLIC');

select tablename, policyname from pg_policies
where schemaname = 'public' and tablename in ('licenses', 'web_orders');

-- 5. После выполнения проверь с публичным ключом:
--    GET /rest/v1/licenses?select=license_key&limit=1
--    Должно быть 401/403, а не массив с ключами.
--    Если backend использует service_role, он продолжит работать:
--    service_role обходит RLS.
