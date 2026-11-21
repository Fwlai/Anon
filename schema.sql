create table if not exists public.anon_users (
  telegram_user_id bigint primary key,
  alias text not null unique,
  created_at timestamptz not null default now(),
  blocked_admin boolean not null default false,
  blocked_by_admin boolean not null default false,
  last_session_at timestamptz
);

create table if not exists public.anon_message_map (
  admin_message_id bigint primary key,
  sender_user_id bigint not null references public.anon_users(telegram_user_id) on delete cascade,
  created_at timestamptz not null default now()
);

create table if not exists public.anon_admin_sessions (
  sender_user_id bigint primary key references public.anon_users(telegram_user_id) on delete cascade,
  last_message_at timestamptz not null default now()
);

alter table public.anon_users enable row level security;
alter table public.anon_message_map enable row level security;
alter table public.anon_admin_sessions enable row level security;

revoke all on public.anon_users from anon, authenticated;
revoke all on public.anon_message_map from anon, authenticated;
revoke all on public.anon_admin_sessions from anon, authenticated;

grant all on public.anon_users to service_role;
grant all on public.anon_message_map to service_role;
grant all on public.anon_admin_sessions to service_role;