DO $$
BEGIN
  CREATE ROLE anon NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
  CREATE ROLE authenticated NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

DO $$
BEGIN
  CREATE ROLE service_role NOLOGIN BYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$$;

GRANT USAGE ON SCHEMA public TO service_role;

CREATE TABLE public.anon_users (
  telegram_user_id bigint PRIMARY KEY,
  alias text NOT NULL UNIQUE
);

CREATE TABLE public.anon_message_map (
  admin_message_id bigint PRIMARY KEY,
  sender_user_id bigint NOT NULL,
  user_message_id bigint
);

CREATE TABLE public.anon_outbound_map (
  user_message_id bigint NOT NULL,
  sender_user_id bigint NOT NULL,
  admin_message_id bigint NOT NULL UNIQUE,
  PRIMARY KEY (sender_user_id, user_message_id)
);

CREATE TABLE public.anon_processed_updates (
  update_id bigint PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);

GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
