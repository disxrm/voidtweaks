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

-- 3. И на всякий случай забираем у публичных ролей сами права на таблицы.
revoke all on public.licenses, public.web_orders from anon, authenticated;

-- Проверка: должно вернуть 0 строк.
select tablename, policyname from pg_policies
where schemaname = 'public' and tablename in ('licenses', 'web_orders');
