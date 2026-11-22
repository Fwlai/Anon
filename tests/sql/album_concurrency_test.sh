#!/usr/bin/env bash
set -euo pipefail

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
SET ROLE service_role;
INSERT INTO public.anon_album_buffer (
  source_chat_id, destination_chat_id, sender_user_id, direction,
  media_group_id, message_id, last_seen_at
) VALUES (
  123, -1004323044570, 123, 'user_to_admin', 'concurrent-late-part', 501,
  now() - interval '3 seconds'
);
SQL

(
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
SET ROLE service_role;
DO $$
DECLARE
  v_count integer;
BEGIN
  SELECT count(*) INTO v_count
  FROM public.anon_claim_album(
    123, -1004323044570, 'concurrent-late-part', 1
  );
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'concurrent test could not claim original album row';
  END IF;
END
$$;
SELECT pg_sleep(2);
COMMIT;
SQL
) &
claim_pid=$!

sleep 0.2

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
SET ROLE service_role;
INSERT INTO public.anon_album_buffer (
  source_chat_id, destination_chat_id, sender_user_id, direction,
  media_group_id, message_id, last_seen_at
) VALUES (
  123, -1004323044570, 123, 'user_to_admin', 'concurrent-late-part', 502,
  now()
);
SQL

wait "$claim_pid"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
SET ROLE service_role;
DO $$
DECLARE
  v_count integer;
  v_status text;
BEGIN
  SELECT count(*), min(status) INTO v_count, v_status
  FROM public.anon_album_buffer
  WHERE source_chat_id = 123
    AND destination_chat_id = -1004323044570
    AND media_group_id = 'concurrent-late-part';

  IF v_count <> 1 OR v_status <> 'processing' THEN
    RAISE EXCEPTION
      'concurrent late insert was not suppressed: count %, status %',
      v_count, v_status;
  END IF;
END
$$;
SQL
