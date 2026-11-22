CREATE OR REPLACE FUNCTION public.anon_guard_album_buffer_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(
    hashtextextended(
      NEW.source_chat_id::text || ':' ||
      NEW.destination_chat_id::text || ':' ||
      NEW.media_group_id,
      0
    )
  );

  IF EXISTS (
    SELECT 1
    FROM public.anon_album_buffer AS album
    WHERE album.source_chat_id = NEW.source_chat_id
      AND album.destination_chat_id = NEW.destination_chat_id
      AND album.media_group_id = NEW.media_group_id
      AND album.status <> 'pending'
  ) THEN
    RETURN NULL;
  END IF;

  UPDATE public.anon_album_buffer
  SET last_seen_at = GREATEST(last_seen_at, NEW.last_seen_at)
  WHERE source_chat_id = NEW.source_chat_id
    AND destination_chat_id = NEW.destination_chat_id
    AND media_group_id = NEW.media_group_id
    AND status = 'pending';

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS anon_album_buffer_insert_guard
  ON public.anon_album_buffer;

CREATE TRIGGER anon_album_buffer_insert_guard
BEFORE INSERT ON public.anon_album_buffer
FOR EACH ROW
EXECUTE FUNCTION public.anon_guard_album_buffer_insert();

REVOKE ALL ON FUNCTION public.anon_guard_album_buffer_insert()
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_guard_album_buffer_insert()
  TO service_role;

NOTIFY pgrst, 'reload schema';
