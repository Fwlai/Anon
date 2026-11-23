ALTER TABLE public.anon_album_buffer
  ADD COLUMN IF NOT EXISTS claimed_at timestamptz;

UPDATE public.anon_album_buffer
SET status = 'delivered_unmapped'
WHERE status = 'processing'
  AND claimed_at IS NULL;

CREATE INDEX IF NOT EXISTS anon_album_buffer_sender_user_id_idx
  ON public.anon_album_buffer (sender_user_id);

CREATE INDEX IF NOT EXISTS anon_album_routes_recipient_user_id_idx
  ON public.anon_album_routes (recipient_user_id);

CREATE INDEX IF NOT EXISTS anon_message_map_sender_user_id_idx
  ON public.anon_message_map (sender_user_id);

CREATE INDEX IF NOT EXISTS anon_album_buffer_processing_claim_idx
  ON public.anon_album_buffer (claimed_at)
  WHERE status = 'processing';

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
      AND last_seen_at > now() - make_interval(
        secs => GREATEST(p_quiet_seconds, 1)
      )
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

CREATE OR REPLACE FUNCTION public.anon_expire_stale_albums(
  p_lease_seconds integer DEFAULT 300
)
RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  v_album record;
  v_updated integer;
  v_total integer := 0;
  v_cutoff timestamptz := now() - make_interval(
    secs => GREATEST(p_lease_seconds, 30)
  );
BEGIN
  FOR v_album IN
    SELECT
      source_chat_id,
      destination_chat_id,
      media_group_id
    FROM public.anon_album_buffer
    WHERE status = 'processing'
    GROUP BY source_chat_id, destination_chat_id, media_group_id
    HAVING count(*) FILTER (WHERE claimed_at IS NULL) > 0
        OR max(claimed_at) < v_cutoff
    ORDER BY source_chat_id, destination_chat_id, media_group_id
  LOOP
    PERFORM pg_advisory_xact_lock(
      hashtextextended(
        v_album.source_chat_id::text || ':' ||
        v_album.destination_chat_id::text || ':' ||
        v_album.media_group_id,
        0
      )
    );

    UPDATE public.anon_album_buffer
    SET status = 'delivered_unmapped'
    WHERE source_chat_id = v_album.source_chat_id
      AND destination_chat_id = v_album.destination_chat_id
      AND media_group_id = v_album.media_group_id
      AND status = 'processing'
      AND (claimed_at IS NULL OR claimed_at < v_cutoff);

    GET DIAGNOSTICS v_updated = ROW_COUNT;
    v_total := v_total + v_updated;
  END LOOP;

  RETURN v_total;
END;
$$;

REVOKE ALL ON FUNCTION public.anon_claim_album(bigint, bigint, text, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.anon_expire_stale_albums(integer)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_claim_album(bigint, bigint, text, integer)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.anon_expire_stale_albums(integer)
  TO service_role;

NOTIFY pgrst, 'reload schema';
