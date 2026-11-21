ALTER TABLE public.anon_message_map ADD COLUMN IF NOT EXISTS user_message_id bigint;

CREATE TABLE IF NOT EXISTS public.anon_inbox_items (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL,
  user_message_id bigint NOT NULL,
  media_group_id text,
  message_kind text NOT NULL DEFAULT 'text',
  unread boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, user_message_id)
);
CREATE INDEX IF NOT EXISTS anon_inbox_unread_recent_idx ON public.anon_inbox_items (unread, created_at DESC);
CREATE INDEX IF NOT EXISTS anon_inbox_user_unread_idx ON public.anon_inbox_items (user_id, unread, created_at ASC);
CREATE INDEX IF NOT EXISTS anon_inbox_media_group_idx ON public.anon_inbox_items (user_id, media_group_id);
CREATE TABLE IF NOT EXISTS public.anon_outbound_map (
  user_message_id bigint NOT NULL,
  sender_user_id bigint NOT NULL,
  admin_message_id bigint NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sender_user_id, user_message_id)
);
CREATE INDEX IF NOT EXISTS anon_outbound_sender_idx ON public.anon_outbound_map (sender_user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS public.anon_chat_activity (
  user_id bigint PRIMARY KEY,
  last_activity timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.anon_admin_state (
  admin_user_id bigint PRIMARY KEY,
  active_user_id bigint NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT ALL ON public.anon_inbox_items, public.anon_outbound_map, public.anon_chat_activity, public.anon_admin_state TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.anon_inbox_items_id_seq TO service_role;
