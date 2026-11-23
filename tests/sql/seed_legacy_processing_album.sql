INSERT INTO public.anon_users (telegram_user_id, alias)
VALUES (789, 'Amber-89');

INSERT INTO public.anon_album_buffer (
  source_chat_id,
  destination_chat_id,
  sender_user_id,
  direction,
  media_group_id,
  message_id,
  status,
  last_seen_at
)
VALUES (
  789,
  -1004323044570,
  789,
  'user_to_admin',
  'recovery-legacy-migration',
  800,
  'processing',
  now() - interval '10 minutes'
);
