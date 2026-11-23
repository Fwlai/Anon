SET ROLE service_role;

DO $$
DECLARE
  v_count integer;
  v_claimed integer;
  v_status text;
  v_claimed_at timestamptz;
BEGIN
  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, last_seen_at
  ) VALUES (
    123, -1004323044570, 123, 'user_to_admin', 'recovery-expired', 501,
    now() - interval '10 minutes'
  );

  SELECT count(*) INTO v_claimed
  FROM public.anon_claim_album(
    123, -1004323044570, 'recovery-expired', 1
  );
  IF v_claimed <> 1 THEN
    RAISE EXCEPTION 'stale-recovery album was not initially claimed';
  END IF;

  UPDATE public.anon_album_buffer
  SET claimed_at = now() - interval '10 minutes'
  WHERE source_chat_id = 123
    AND destination_chat_id = -1004323044570
    AND media_group_id = 'recovery-expired';

  SELECT public.anon_expire_stale_albums(30) INTO v_count;
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'expected one stale album item to expire, got %', v_count;
  END IF;

  SELECT status INTO v_status
  FROM public.anon_album_buffer
  WHERE media_group_id = 'recovery-expired';
  IF v_status <> 'unknown' THEN
    RAISE EXCEPTION 'expired album was not marked unknown';
  END IF;

  SELECT count(*) INTO v_claimed
  FROM public.anon_claim_album(
    123, -1004323044570, 'recovery-expired', 1
  );
  IF v_claimed <> 0 THEN
    RAISE EXCEPTION 'unknown album was incorrectly claimed again';
  END IF;

  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, last_seen_at
  ) VALUES (
    123, -1004323044570, 123, 'user_to_admin', 'recovery-retry', 601,
    now() - interval '10 minutes'
  );

  SELECT count(*) INTO v_claimed
  FROM public.anon_claim_album(
    123, -1004323044570, 'recovery-retry', 1
  );
  IF v_claimed <> 1 THEN
    RAISE EXCEPTION 'retry album was not claimed';
  END IF;

  SELECT claimed_at INTO v_claimed_at
  FROM public.anon_album_buffer
  WHERE media_group_id = 'recovery-retry';
  IF v_claimed_at IS NULL THEN
    RAISE EXCEPTION 'claimed album lease timestamp is missing';
  END IF;

  PERFORM public.anon_finish_album(
    123, -1004323044570, 'recovery-retry', NULL, 'pending'
  );

  SELECT status, claimed_at
    INTO v_status, v_claimed_at
  FROM public.anon_album_buffer
  WHERE media_group_id = 'recovery-retry';
  IF v_status <> 'pending' OR v_claimed_at IS NOT NULL THEN
    RAISE EXCEPTION 'pending retry did not clear its processing lease';
  END IF;

  SELECT count(*) INTO v_claimed
  FROM public.anon_claim_album(
    123, -1004323044570, 'recovery-retry', 1
  );
  IF v_claimed <> 1 THEN
    RAISE EXCEPTION 'pending album could not be claimed again';
  END IF;

  PERFORM public.anon_finish_album(
    123, -1004323044570, 'recovery-retry', NULL, 'failed'
  );

  IF has_function_privilege(
    'anon', 'public.anon_expire_stale_albums(integer)', 'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'anon role can execute album recovery function';
  END IF;

  IF NOT has_function_privilege(
    'service_role', 'public.anon_expire_stale_albums(integer)', 'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'service_role cannot execute album recovery function';
  END IF;
END
$$;

RESET ROLE;
