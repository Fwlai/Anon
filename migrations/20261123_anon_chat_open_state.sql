alter table public.anon_users
  add column if not exists is_open boolean not null default false;

create table if not exists public.anon_admin_sessions (
  sender_user_id bigint primary key references public.anon_users(telegram_user_id) on delete cascade,
  last_message_at timestamptz not null default now()
);

alter table public.anon_admin_sessions
  add column if not exists last_header_message_id bigint;

-- The active admin recipient is the single source of truth for open state.
update public.anon_users
set is_open = false;
