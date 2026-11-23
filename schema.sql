create table if not exists public.anon_users (
  telegram_user_id bigint primary key,
  alias text not null unique,
  is_open boolean not null default false,
  created_at timestamptz not null default now(),
  blocked_admin boolean not null default false,
  blocked_by_admin boolean not null default false,
  last_session_at timestamptz,
  start_reacted boolean not null default false
);
alter table public.anon_users add column if not exists start_reacted boolean not null default false;
alter table public.anon_users add column if not exists is_open boolean not null default false;

create table if not exists public.anon_message_map (
  admin_message_id bigint primary key,
  sender_user_id bigint not null references public.anon_users(telegram_user_id) on delete cascade,
  created_at timestamptz not null default now()
);

create table if not exists public.anon_admin_sessions (
  sender_user_id bigint primary key references public.anon_users(telegram_user_id) on delete cascade,
  last_message_at timestamptz not null default now(),
  last_header_message_id bigint
);
alter table public.anon_admin_sessions
  add column if not exists last_header_message_id bigint;

create table if not exists public.anon_processed_updates (
  update_id bigint primary key,
  created_at timestamptz not null default now()
);

create table if not exists public.anon_rate_limits (
  telegram_user_id bigint primary key references public.anon_users(telegram_user_id) on delete cascade,
  window_started_at timestamptz not null default now(),
  message_count integer not null default 0,
  blocked_until timestamptz
);

create or replace function public.anon_rate_check(p_user_id bigint, p_limit integer default 50, p_window_seconds integer default 10)
returns boolean
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_now timestamptz := now();
  v_count integer;
  v_started timestamptz;
  v_blocked timestamptz;
begin
  insert into anon_rate_limits(telegram_user_id, window_started_at, message_count, blocked_until)
  values (p_user_id, v_now, 1, null)
  on conflict (telegram_user_id) do nothing;

  select message_count, window_started_at, blocked_until
    into v_count, v_started, v_blocked
    from anon_rate_limits
    where telegram_user_id = p_user_id
    for update;

  if v_blocked is not null and v_blocked > v_now then
    return false;
  end if;

  if v_started + make_interval(secs => p_window_seconds) <= v_now then
    update anon_rate_limits
      set window_started_at = v_now, message_count = 1, blocked_until = null
      where telegram_user_id = p_user_id;
    return true;
  end if;

  if v_count >= p_limit then
    update anon_rate_limits
      set blocked_until = v_now + interval '60 seconds'
      where telegram_user_id = p_user_id;
    return false;
  end if;

  update anon_rate_limits
    set message_count = message_count + 1
    where telegram_user_id = p_user_id;
  return true;
end;
$$;

alter table public.anon_users enable row level security;
alter table public.anon_message_map enable row level security;
alter table public.anon_admin_sessions enable row level security;
alter table public.anon_processed_updates enable row level security;
alter table public.anon_rate_limits enable row level security;

revoke all on public.anon_users from anon, authenticated;
revoke all on public.anon_message_map from anon, authenticated;
revoke all on public.anon_admin_sessions from anon, authenticated;
revoke all on public.anon_processed_updates from anon, authenticated;
revoke all on public.anon_rate_limits from anon, authenticated;
revoke all on function public.anon_rate_check(bigint, integer, integer) from public, anon, authenticated;

grant all on public.anon_users to service_role;
grant all on public.anon_message_map to service_role;
grant all on public.anon_admin_sessions to service_role;
grant all on public.anon_processed_updates to service_role;
grant all on public.anon_rate_limits to service_role;
grant execute on function public.anon_rate_check(bigint, integer, integer) to service_role;

create table if not exists public.anon_log_config (
  config_key text primary key check (config_key = 'log_chat'),
  chat_id bigint not null,
  registered_by bigint not null,
  registered_at timestamptz not null default now()
);
alter table public.anon_log_config enable row level security;
revoke all on public.anon_log_config from anon, authenticated;
grant all on public.anon_log_config to service_role;
