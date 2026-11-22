CREATE TABLE IF NOT EXISTS public.anon_album_buffer (
  source_chat_id bigint NOT NULL,
  destination_chat_id bigint NOT NULL,
  sender_user_id bigint NOT NULL REFERENCES public.anon_users(telegram_user_id) ON DELETE CASCADE,
  direction text NOT NULL CHECK (direction IN ('user_to_admin', 'admin_to_user')),
  media_group_id text NOT NULL,
  message_id bigint NOT NULL,
  reply_to_message_id bigint,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'delivered', 'delivered_unmapped')),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_chat_id, destination_chat_id, media_group_id, message_id)
);

CREATE INDEX IF NOT EXISTS anon_album_buffer_pending_idx
  ON public.anon_album_buffer (source_chat_id, destination_chat_id, media_group_id, last_seen_at)
  WHERE status = 'pending';

ALTER TABLE public.anon_album_buffer ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.anon_album_buffer FROM anon, authenticated;
GRANT ALL ON public.anon_album_buffer TO service_role;

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
    SET status = 'processing'
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
BEGIN
  IF p_status NOT IN ('pending', 'delivered', 'delivered_unmapped') THEN
    RAISE EXCEPTION 'Invalid album status';
  END IF;

  SELECT direction, sender_user_id
    INTO v_direction, v_sender_user_id
  FROM public.anon_album_buffer
  WHERE source_chat_id = p_source_chat_id
    AND destination_chat_id = p_destination_chat_id
    AND media_group_id = p_media_group_id
    AND status = 'processing'
  ORDER BY message_id
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
    IF jsonb_typeof(p_mappings) IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_mappings) <> v_expected THEN
      RAISE EXCEPTION 'Album mapping count does not match claimed messages';
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
    END IF;
  END IF;

  UPDATE public.anon_album_buffer
  SET status = p_status
  WHERE source_chat_id = p_source_chat_id
    AND destination_chat_id = p_destination_chat_id
    AND media_group_id = p_media_group_id
    AND status = 'processing';

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated;
END;
$$;

REVOKE ALL ON FUNCTION public.anon_claim_album(bigint, bigint, text, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.anon_finish_album(bigint, bigint, text, jsonb, text)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_claim_album(bigint, bigint, text, integer)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.anon_finish_album(bigint, bigint, text, jsonb, text)
  TO service_role;
