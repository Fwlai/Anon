SET ROLE service_role;

DO $$
DECLARE
  v_expired integer;
  v_claimed integer;
BEGIN
  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, status, last_seen_at, claimed_at
  ) VALUES
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-stale', 801,
     'pending', now() - interval '10 minutes', NULL),
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-stale', 802,
     'pending', now() - interval '10 minutes', NULL),
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-fresh', 803,
     'processing', now(), now()),
    (123, -1004323044570, 123, 'user_to_admin', 'recovery-pending', 804,
     'pending', now() - interval '10 minutes', NULL);

  SELECT count(*) INTO v_claimed
  FROM public.anon_claim_album(
    123, -1004323044570, 'recovery-stale', 1
  );

  IF v_claimed <> 2 THEN
    RAISE EXCEPTION 'expected two album parts to be claimed, got %', v_claimed;
  END IF;

  UPDATE public.anon_album_buffer
  SET claimed_at = now() - interval '10 minutes'
  WHERE source_chat_id = 123
    AND destination_chat_id = -1004323044570
    AND media_group_id = 'recovery-stale'
    AND status = 'processing';

  SELECT public.anon_expire_stale_albums(300)
    INTO v_expired;

  IF v_expired <> 2 THEN
    RAISE EXCEPTION 'expected 2 stale claimed album parts to expire, got %', v_expired;
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.anon_album_buffer
    WHERE media_group_id = 'recovery-stale'
      AND status <> 'delivered_unmapped'
  ) THEN
    RAISE EXCEPTION 'stale claimed album was not marked delivered_unmapped';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.anon_album_buffer
    WHERE media_group_id = 'recovery-legacy-migration'
      AND status <> 'delivered_unmapped'
  ) THEN
    RAISE EXCEPTION 'legacy processing album was not migrated to delivered_unmapped';
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
