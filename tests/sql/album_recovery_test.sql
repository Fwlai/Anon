SET ROLE service_role;

DO $$
DECLARE
  v_expired integer;
BEGIN
  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, status, last_seen_at
  ) VALUES
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-stale', 801,
     'processing', now() - interval '10 minutes'),
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-stale', 802,
     'processing', now() - interval '10 minutes'),
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-fresh', 803,
     'processing', now()),
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-pending', 804,
     'pending', now() - interval '10 minutes');

  SELECT public.anon_expire_stale_albums(300)
    INTO v_expired;

  IF v_expired <> 2 THEN
    RAISE EXCEPTION 'expected 2 stale album parts to expire, got %', v_expired;
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.anon_album_buffer
    WHERE media_group_id = 'recovery-stale'
      AND status <> 'delivered_unmapped'
  ) THEN
    RAISE EXCEPTION 'stale processing album was not marked delivered_unmapped';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.anon_album_buffer
    WHERE media_group_id = 'recovery-fresh'
      AND status <> 'processing'
  ) THEN
    RAISE EXCEPTION 'fresh processing album was incorrectly expired';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.anon_album_buffer
    WHERE media_group_id = 'recovery-pending'
      AND status <> 'pending'
  ) THEN
    RAISE EXCEPTION 'pending album was modified by stale recovery';
  END IF;
END
$$;
