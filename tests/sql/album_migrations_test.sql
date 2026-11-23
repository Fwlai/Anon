INSERT INTO public.anon_users (telegram_user_id, alias)
VALUES (123, 'Blue-01'), (456, 'Red-02');

SET ROLE service_role;

DO $$
DECLARE
  v_count integer;
  v_recipient bigint;
  v_reply bigint;
  v_claimed boolean;
  v_state text;
  v_token uuid;
  v_next_token uuid;
  v_lease_until timestamptz;
BEGIN
  IF has_table_privilege('anon', 'public.anon_processed_updates', 'SELECT') THEN
    RAISE EXCEPTION 'anon role can read processed update leases';
  END IF;

  IF has_table_privilege('anon', 'public.anon_album_buffer', 'SELECT')
     OR has_table_privilege('anon', 'public.anon_album_routes', 'SELECT') THEN
    RAISE EXCEPTION 'anon role can read private album routing tables';
  END IF;

  IF has_function_privilege(
    'anon', 'public.anon_claim_update(bigint,integer)', 'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'anon role can execute the update claim function';
  END IF;

  IF NOT has_table_privilege(
    'service_role', 'public.anon_processed_updates', 'SELECT'
  ) THEN
    RAISE EXCEPTION 'service_role cannot access processed update leases';
  END IF;

  INSERT INTO public.anon_processed_updates (update_id) VALUES (6003);

  SELECT status, lease_until, lease_token
    INTO v_state, v_lease_until, v_token
  FROM public.anon_processed_updates
  WHERE update_id = 6003;
  IF v_state <> 'processing' OR v_lease_until <= now() OR v_token IS NOT NULL THEN
    RAISE EXCEPTION 'legacy application insert was not fenced during deployment';
  END IF;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_token
  FROM public.anon_claim_update(6003, 30);
  IF v_claimed OR v_state <> 'processing' OR v_token IS NOT NULL THEN
    RAISE EXCEPTION 'new application did not wait for legacy update lease';
  END IF;

  UPDATE public.anon_processed_updates
  SET lease_until = now() - interval '1 second'
  WHERE update_id = 6003;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_token
  FROM public.anon_claim_update(6003, 30);
  IF v_claimed OR v_state <> 'unknown' OR v_token IS NOT NULL THEN
    RAISE EXCEPTION 'expired legacy update was not marked unknown';
  END IF;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_token
  FROM public.anon_claim_update(6000, 30);
  IF v_claimed OR v_state <> 'processed' OR v_token IS NOT NULL THEN
    RAISE EXCEPTION 'legacy processed update was not preserved';
  END IF;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_token
  FROM public.anon_claim_update(6001, 30);
  IF NOT v_claimed OR v_state <> 'claimed' OR v_token IS NULL THEN
    RAISE EXCEPTION 'new update lease was not claimed';
  END IF;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_next_token
  FROM public.anon_claim_update(6001, 30);
  IF v_claimed OR v_state <> 'processing' OR v_next_token <> v_token THEN
    RAISE EXCEPTION 'active update lease did not block duplicate processing';
  END IF;

  IF NOT public.anon_complete_update(6001, v_token) THEN
    RAISE EXCEPTION 'update lease could not be completed';
  END IF;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_next_token
  FROM public.anon_claim_update(6001, 30);
  IF v_claimed OR v_state <> 'processed' OR v_next_token IS NOT NULL THEN
    RAISE EXCEPTION 'completed update was not deduplicated';
  END IF;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_token
  FROM public.anon_claim_update(6002, 30);
  IF NOT v_claimed OR v_token IS NULL THEN
    RAISE EXCEPTION 'recoverable update lease was not initially claimed';
  END IF;

  UPDATE public.anon_processed_updates
  SET lease_until = now() - interval '1 second'
  WHERE update_id = 6002;

  SELECT claimed, state, lease_token
    INTO v_claimed, v_state, v_next_token
  FROM public.anon_claim_update(6002, 30);
  IF v_claimed OR v_state <> 'unknown' OR v_next_token IS NOT NULL THEN
    RAISE EXCEPTION 'expired update lease was not marked as ambiguous';
  END IF;

  IF public.anon_complete_update(6002, v_token) THEN
    RAISE EXCEPTION 'expired lease token completed an unknown update';
  END IF;

  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, reply_to_message_id, last_seen_at
  ) VALUES
    (123, -1004323044570, 123, 'user_to_admin', 'user-album-1', 101, 700,
     now() - interval '3 seconds'),
    (123, -1004323044570, 123, 'user_to_admin', 'user-album-1', 102, 700,
     now() - interval '3 seconds');

  SELECT count(*) INTO v_count
  FROM public.anon_claim_album(123, -1004323044570, 'user-album-1', 1);
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'expected two claimed user album items, got %', v_count;
  END IF;

  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, last_seen_at
  ) VALUES (
    123, -1004323044570, 123, 'user_to_admin', 'user-album-1', 103, now()
  );

  SELECT count(*) INTO v_count
  FROM public.anon_album_buffer
  WHERE source_chat_id = 123
    AND destination_chat_id = -1004323044570
    AND media_group_id = 'user-album-1';
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'late part created a second album row';
  END IF;

  PERFORM public.anon_finish_album(
    123,
    -1004323044570,
    'user-album-1',
    '[{"source_message_id":101,"destination_message_id":901},
      {"source_message_id":102,"destination_message_id":902}]'::jsonb,
    'delivered'
  );

  SELECT count(*) INTO v_count
  FROM public.anon_message_map
  WHERE sender_user_id = 123;
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'user album mappings were not committed atomically';
  END IF;

  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, last_seen_at
  ) VALUES
    (-1004323044570, 123, 123, 'admin_to_user', 'admin-album-1', 201,
     now() - interval '3 seconds'),
    (-1004323044570, 123, 123, 'admin_to_user', 'admin-album-1', 202,
     now() - interval '3 seconds');

  SELECT count(*) INTO v_count
  FROM public.anon_claim_album(-1004323044570, 123, 'admin-album-1', 1);
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'expected two claimed admin album items, got %', v_count;
  END IF;

  PERFORM public.anon_finish_album(
    -1004323044570,
    123,
    'admin-album-1',
    '[{"source_message_id":201,"destination_message_id":301},
      {"source_message_id":202,"destination_message_id":302}]'::jsonb,
    'delivered'
  );

  SELECT count(*) INTO v_count
  FROM public.anon_outbound_map
  WHERE sender_user_id = 123;
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'admin album outbound mappings were not committed';
  END IF;

  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-race', 123, 55
  );
  IF v_recipient <> 123 OR v_reply <> 55 THEN
    RAISE EXCEPTION 'first album route reservation is incorrect';
  END IF;

  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-race', 456, 66
  );
  IF v_recipient <> 123 OR v_reply <> 55 THEN
    RAISE EXCEPTION 'album route reservation changed recipient or reply target';
  END IF;
  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-reply-fill', 123, NULL
  );
  IF v_recipient <> 123 OR v_reply IS NOT NULL THEN
    RAISE EXCEPTION 'reply-less route reservation is incorrect';
  END IF;

  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-reply-fill', 456, 77
  );
  IF v_recipient <> 123 OR v_reply <> 77 THEN
    RAISE EXCEPTION 'later album part did not fill missing reply target';
  END IF;

  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-reply-fill', 456, 88
  );
  IF v_recipient <> 123 OR v_reply <> 77 THEN
    RAISE EXCEPTION 'existing recipient or reply target was overwritten';
  END IF;
  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-reply-fill', 123, NULL
  );
  IF v_recipient <> 123 OR v_reply IS NOT NULL THEN
    RAISE EXCEPTION 'reply-less route reservation is incorrect';
  END IF;

  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-reply-fill', 456, 77
  );
  IF v_recipient <> 123 OR v_reply <> 77 THEN
    RAISE EXCEPTION 'later album part did not fill missing reply target';
  END IF;

  SELECT recipient_user_id, reply_to_message_id
    INTO v_recipient, v_reply
  FROM public.anon_reserve_admin_album_route(
    -1004323044570, 'route-reply-fill', 456, 88
  );
  IF v_recipient <> 123 OR v_reply <> 77 THEN
    RAISE EXCEPTION 'existing recipient or reply target was overwritten';
  END IF;

  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, last_seen_at
  ) VALUES (
    123, -1004323044570, 123, 'user_to_admin', 'invalid-map', 401,
    now() - interval '3 seconds'
  );

  SELECT count(*) INTO v_count
  FROM public.anon_claim_album(123, -1004323044570, 'invalid-map', 1);
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'invalid mapping test album was not claimed';
  END IF;

  BEGIN
    PERFORM public.anon_finish_album(
      123, -1004323044570, 'invalid-map',
      '[]'::jsonb, 'delivered'
    );
    RAISE EXCEPTION 'expected invalid mapping count to fail';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM = 'expected invalid mapping count to fail' THEN
      RAISE;
    END IF;
  END;

  SELECT count(*) INTO v_count
  FROM public.anon_album_buffer
  WHERE source_chat_id = 123
    AND destination_chat_id = -1004323044570
    AND media_group_id = 'invalid-map'
    AND status = 'processing';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'failed mapping changed album processing state';
  END IF;

  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, last_seen_at
  ) VALUES (
    123, -1004323044570, 123, 'user_to_admin', 'wrong-source-map', 411,
    now() - interval '3 seconds'
  );

  SELECT count(*) INTO v_count
  FROM public.anon_claim_album(123, -1004323044570, 'wrong-source-map', 1);
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'wrong source mapping test album was not claimed';
  END IF;

  BEGIN
    PERFORM public.anon_finish_album(
      123, -1004323044570, 'wrong-source-map',
      '[{"source_message_id":999,"destination_message_id":911}]'::jsonb,
      'delivered'
    );
    RAISE EXCEPTION 'expected unclaimed source ID to fail';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM = 'expected unclaimed source ID to fail' THEN
      RAISE;
    END IF;
  END;

  INSERT INTO public.anon_album_buffer (
    source_chat_id, destination_chat_id, sender_user_id, direction,
    media_group_id, message_id, last_seen_at
  ) VALUES
    (123, -1004323044570, 123, 'user_to_admin', 'duplicate-destination-map', 421,
     now() - interval '3 seconds'),
    (123, -1004323044570, 123, 'user_to_admin', 'duplicate-destination-map', 422,
     now() - interval '3 seconds');

  SELECT count(*) INTO v_count
  FROM public.anon_claim_album(
    123, -1004323044570, 'duplicate-destination-map', 1
  );
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'duplicate destination mapping test album was not claimed';
  END IF;

  BEGIN
    PERFORM public.anon_finish_album(
      123, -1004323044570, 'duplicate-destination-map',
      '[{"source_message_id":421,"destination_message_id":912},
        {"source_message_id":422,"destination_message_id":912}]'::jsonb,
      'delivered'
    );
    RAISE EXCEPTION 'expected duplicate destination IDs to fail';
  EXCEPTION WHEN OTHERS THEN
    IF SQLERRM = 'expected duplicate destination IDs to fail' THEN
      RAISE;
    END IF;
  END;

  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public'
      AND indexname = 'anon_album_buffer_created_at_idx'
  ) THEN
    RAISE EXCEPTION 'album retention index is missing';
  END IF;
END
$$;
