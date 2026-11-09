-- Привязка ключей и заказов к аккаунтам сайта (Supabase Auth).
-- Выполнить один раз в Supabase SQL Editor ДО деплоя backend с account_api.
-- RLS уже включён в supabase_lock.sql: anon/authenticated не имеют доступа
-- к таблицам напрямую, все чтения/записи идут через backend с service key.

alter table public.web_orders
  add column if not exists user_id uuid references auth.users(id) on delete set null;

alter table public.licenses
  add column if not exists user_id uuid references auth.users(id) on delete set null;

create index if not exists web_orders_user_id_idx on public.web_orders (user_id);
create index if not exists licenses_user_id_idx on public.licenses (user_id);
