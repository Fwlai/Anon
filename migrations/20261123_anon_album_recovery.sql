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
    HAVING max(last_seen_at) < v_cutoff
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
      AND last_seen_at < v_cutoff;

    GET DIAGNOSTICS v_updated = ROW_COUNT;
    v_total := v_total + v_updated;
  END LOOP;

  RETURN v_total;
END;
$$;

REVOKE ALL ON FUNCTION public.anon_expire_stale_albums(integer)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_expire_stale_albums(integer)
  TO service_role;

NOTIFY pgrst, 'reload schema';
