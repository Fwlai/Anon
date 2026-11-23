ALTER TABLE public.anon_album_buffer
  ADD COLUMN IF NOT EXISTS claimed_at timestamptz;

ALTER TABLE public.anon_album_buffer
  DROP CONSTRAINT IF EXISTS anon_album_buffer_status_check;

UPDATE public.anon_album_buffer
SET status = 'unknown'
WHERE status = 'processing'
  AND claimed_at IS NULL;

ALTER TABLE public.anon_album_buffer
  ADD CONSTRAINT anon_album_buffer_status_check
  CHECK (status IN (
    'pending', 'processing', 'delivered', 'delivered_unmapped', 'failed', 'unknown'
  ));

CREATE INDEX IF NOT EXISTS anon_album_buffer_processing_lease_idx
  ON public.anon_album_buffer (claimed_at)
  WHERE status = 'processing';

-- Keep user-deletion cascades efficient as these relay tables grow.
CREATE INDEX IF NOT EXISTS anon_album_buffer_sender_user_id_idx
  ON public.anon_album_buffer (sender_user_id);

CREATE INDEX IF NOT EXISTS anon_album_routes_recipient_user_id_idx
  ON public.anon_album_routes (recipient_user_id);

CREATE INDEX IF NOT EXISTS anon_message_map_sender_user_id_idx
  ON public.anon_message_map (sender_user_id);

CREATE OR REPLACE FUNCTION public.anon_reserve_admin_album_route(
  p_source_chat_id bigint,
  p_media_group_id text,
  p_candidate_user_id bigint,
  p_reply_to_message_id bigint DEFAULT NULL
)
RETURNS TABLE (
  recipient_user_id bigint,
  reply_to_message_id bigint
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $
BEGIN
  INSERT INTO public.anon_album_routes AS route (
    source_chat_id,
    media_group_id,
    recipient_user_id,
    reply_to_message_id
  )
  VALUES (
    p_source_chat_id,
    p_media_group_id,
    p_candidate_user_id,
    p_reply_to_message_id
  )
  ON CONFLICT (source_chat_id, media_group_id)
  DO UPDATE SET
    recipient_user_id = route.recipient_user_id,
    reply_to_message_id = COALESCE(
      route.reply_to_message_id, EXCLUDED.reply_to_message_id
    )
  RETURNING route.recipient_user_id, route.reply_to_message_id
    INTO recipient_user_id, reply_to_message_id;

  RETURN NEXT;
END;
$;

REVOKE ALL ON FUNCTION public.anon_reserve_admin_album_route(bigint, text, bigint, bigint)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_reserve_admin_album_route(bigint, text, bigint, bigint)
  TO service_role;


CREATE OR REPLACE FUNCTION public.anon_claim_album(
  p_source_chat_id bigint,
  p_destination_chat_id bigint,
  p_media_group_id text,
  p_quiet_seconds integer DEFAULT 1
)
RETURNS TABLE (
  sender_user_id bigint,
  direction text,
  message_id bigint,
  reply_to_message_id bigint
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(
    hashtextextended(
      p_source_chat_id::text || ':' ||
      p_destination_chat_id::text || ':' ||
      p_media_group_id,
      0
    )
  );

  IF NOT EXISTS (
    SELECT 1
    FROM public.anon_album_buffer
    WHERE source_chat_id = p_source_chat_id
      AND destination_chat_id = p_destination_chat_id
      AND media_group_id = p_media_group_id
      AND status = 'pending'
  ) THEN
    RETURN;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.anon_album_buffer
    WHERE source_chat_id = p_source_chat_id
      AND destination_chat_id = p_destination_chat_id
      AND media_group_id = p_media_group_id
      AND status = 'pending'
      AND last_seen_at > now() - make_interval(secs => GREATEST(p_quiet_seconds, 1))
  ) THEN
    RETURN;
  END IF;

  RETURN QUERY
  WITH claimed AS (
    UPDATE public.anon_album_buffer
    SET status = 'processing',
        claimed_at = now()
    WHERE source_chat_id = p_source_chat_id
      AND destination_chat_id = p_destination_chat_id
      AND media_group_id = p_media_group_id
      AND status = 'pending'
    RETURNING
      anon_album_buffer.sender_user_id,
      anon_album_buffer.direction,
      anon_album_buffer.message_id,
      anon_album_buffer.reply_to_message_id
  )
  SELECT claimed.sender_user_id,
         claimed.direction,
         claimed.message_id,
         claimed.reply_to_message_id
  FROM claimed
  ORDER BY claimed.message_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.anon_finish_album(
  p_source_chat_id bigint,
  p_destination_chat_id bigint,
  p_media_group_id text,
  p_mappings jsonb DEFAULT NULL,
  p_status text DEFAULT 'delivered'
)
RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  v_direction text;
  v_sender_user_id bigint;
  v_expected integer;
  v_updated integer;
  v_inserted integer;
BEGIN
  PERFORM pg_advisory_xact_lock(
    hashtextextended(
      p_source_chat_id::text || ':' ||
      p_destination_chat_id::text || ':' ||
      p_media_group_id,
      0
    )
  );

  IF p_status IS NULL OR p_status NOT IN ('pending', 'delivered', 'delivered_unmapped', 'failed') THEN
    RAISE EXCEPTION 'Invalid album status';
  END IF;

  SELECT album.direction, album.sender_user_id
    INTO v_direction, v_sender_user_id
  FROM public.anon_album_buffer AS album
  WHERE album.source_chat_id = p_source_chat_id
    AND album.destination_chat_id = p_destination_chat_id
    AND album.media_group_id = p_media_group_id
    AND album.status = 'processing'
  ORDER BY album.message_id
  LIMIT 1;

  IF NOT FOUND THEN
    RETURN 0;
  END IF;

  SELECT count(*)
    INTO v_expected
  FROM public.anon_album_buffer
  WHERE source_chat_id = p_source_chat_id
    AND destination_chat_id = p_destination_chat_id
    AND media_group_id = p_media_group_id
    AND status = 'processing';

  IF p_status = 'delivered' THEN
    IF jsonb_typeof(p_mappings) IS DISTINCT FROM 'array' THEN
      RAISE EXCEPTION 'Album mappings must be a JSON array';
    END IF;

    IF jsonb_array_length(p_mappings) <> v_expected THEN
      RAISE EXCEPTION 'Album mapping count does not match claimed messages';
    END IF;

    IF (
      SELECT count(DISTINCT item.source_message_id)
      FROM jsonb_to_recordset(p_mappings) AS item(
        source_message_id bigint,
        destination_message_id bigint
      )
    ) <> v_expected THEN
      RAISE EXCEPTION 'Album source message IDs must be unique and complete';
    END IF;

    IF EXISTS (
      SELECT 1
      FROM jsonb_to_recordset(p_mappings) AS item(
        source_message_id bigint,
        destination_message_id bigint
      )
      LEFT JOIN public.anon_album_buffer AS album
        ON album.source_chat_id = p_source_chat_id
       AND album.destination_chat_id = p_destination_chat_id
       AND album.media_group_id = p_media_group_id
       AND album.message_id = item.source_message_id
       AND album.status = 'processing'
      WHERE album.message_id IS NULL
    ) THEN
      RAISE EXCEPTION 'Album mapping contains an unclaimed source message';
    END IF;

    IF (
      SELECT count(DISTINCT item.destination_message_id)
      FROM jsonb_to_recordset(p_mappings) AS item(
        source_message_id bigint,
        destination_message_id bigint
      )
    ) <> v_expected THEN
      RAISE EXCEPTION 'Album destination message IDs must be unique and complete';
    END IF;

    IF v_direction = 'user_to_admin' THEN
      INSERT INTO public.anon_message_map (
        admin_message_id, sender_user_id, user_message_id
      )
      SELECT
        item.destination_message_id,
        v_sender_user_id,
        item.source_message_id
      FROM jsonb_to_recordset(p_mappings) AS item(
        source_message_id bigint,
        destination_message_id bigint
      )
      ON CONFLICT (admin_message_id) DO NOTHING;

      GET DIAGNOSTICS v_inserted = ROW_COUNT;
      IF v_inserted <> v_expected THEN
        RAISE EXCEPTION 'Not all incoming album mappings were inserted';
      END IF;
    ELSE
      INSERT INTO public.anon_outbound_map (
        user_message_id, sender_user_id, admin_message_id
      )
      SELECT
        item.destination_message_id,
        v_sender_user_id,
        item.source_message_id
      FROM jsonb_to_recordset(p_mappings) AS item(
        source_message_id bigint,
        destination_message_id bigint
      )
      ON CONFLICT (sender_user_id, user_message_id) DO NOTHING;

      GET DIAGNOSTICS v_inserted = ROW_COUNT;
      IF v_inserted <> v_expected THEN
        RAISE EXCEPTION 'Not all outgoing album mappings were inserted';
      END IF;
    END IF;
  END IF;

  UPDATE public.anon_album_buffer
  SET status = p_status,
      claimed_at = NULL
  WHERE source_chat_id = p_source_chat_id
    AND destination_chat_id = p_destination_chat_id
    AND media_group_id = p_media_group_id
    AND status = 'processing';

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated;
END;
$$;

CREATE OR REPLACE FUNCTION public.anon_expire_stale_albums(
  p_lease_seconds integer DEFAULT 300
)
RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  v_group record;
  v_expired integer := 0;
  v_updated integer;
  v_cutoff timestamptz := now() - make_interval(secs => GREATEST(p_lease_seconds, 30));
BEGIN
  FOR v_group IN
    SELECT DISTINCT source_chat_id, destination_chat_id, media_group_id
    FROM public.anon_album_buffer
    WHERE status = 'processing'
      AND claimed_at IS NOT NULL
      AND claimed_at <= v_cutoff
  LOOP
    PERFORM pg_advisory_xact_lock(
      hashtextextended(
        v_group.source_chat_id::text || ':' ||
        v_group.destination_chat_id::text || ':' ||
        v_group.media_group_id,
        0
      )
    );

    UPDATE public.anon_album_buffer
    SET status = 'unknown',
        claimed_at = NULL
    WHERE source_chat_id = v_group.source_chat_id
      AND destination_chat_id = v_group.destination_chat_id
      AND media_group_id = v_group.media_group_id
      AND status = 'processing'
      AND claimed_at <= v_cutoff;

    GET DIAGNOSTICS v_updated = ROW_COUNT;
    v_expired := v_expired + v_updated;
  END LOOP;

  RETURN v_expired;
END;
$$;

REVOKE ALL ON FUNCTION public.anon_expire_stale_albums(integer)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_expire_stale_albums(integer)
  TO service_role;

NOTIFY pgrst, 'reload schema';
