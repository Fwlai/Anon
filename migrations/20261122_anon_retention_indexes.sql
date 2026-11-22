CREATE INDEX IF NOT EXISTS anon_processed_updates_created_at_idx
  ON public.anon_processed_updates (created_at);

CREATE INDEX IF NOT EXISTS anon_album_buffer_created_at_idx
  ON public.anon_album_buffer (created_at);
