alter table public.anon_users
  add column if not exists is_open boolean not null default false;
