ALTER TABLE public.anon_processed_updates
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'processed';

ALTER TABLE public.anon_processed_updates
  ADD COLUMN IF NOT EXISTS lease_until timestamptz;

ALTER TABLE public.anon_processed_updates
  ADD COLUMN IF NOT EXISTS lease_token uuid;

DO $$
BEGIN
  ALTER TABLE public.anon_processed_updates
    ADD CONSTRAINT anon_processed_updates_status_check
    CHECK (status IN ('processing', 'processed', 'unknown'));
EXCEPTION WHEN duplicate_object THEN
  NULL;
END
$$;

CREATE INDEX IF NOT EXISTS anon_processed_updates_processing_lease_idx
  ON public.anon_processed_updates (lease_until)
  WHERE status = 'processing';

ALTER TABLE public.anon_processed_updates ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.anon_processed_updates FROM anon, authenticated;
GRANT ALL ON public.anon_processed_updates TO service_role;

DROP FUNCTION IF EXISTS public.anon_claim_update(bigint, integer);

CREATE FUNCTION public.anon_claim_update(
  p_update_id bigint,
  p_lease_seconds integer DEFAULT 300
)
RETURNS TABLE (
  claimed boolean,
  state text,
  lease_token uuid
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  v_claimed_id bigint;
  v_state text;
  v_lease_token uuid := gen_random_uuid();
BEGIN
  INSERT INTO public.anon_processed_updates (
    update_id,
    status,
    lease_until,
    lease_token
  )
  VALUES (
    p_update_id,
    'processing',
    now() + make_interval(secs => GREATEST(p_lease_seconds, 30)),
    v_lease_token
  )
  ON CONFLICT (update_id) DO NOTHING
  RETURNING update_id INTO v_claimed_id;

  IF FOUND THEN
    RETURN QUERY SELECT true, 'claimed'::text, v_lease_token;
    RETURN;
  END IF;

  UPDATE public.anon_processed_updates
  SET status = 'unknown',
      lease_until = NULL,
      lease_token = NULL,
      created_at = now()
  WHERE update_id = p_update_id
    AND status = 'processing'
    AND (
      lease_until IS NULL
      OR lease_until <= now()
    )
  RETURNING update_id INTO v_claimed_id;

  IF FOUND THEN
    RETURN QUERY SELECT false, 'unknown'::text, NULL::uuid;
    RETURN;
  END IF;

  SELECT update_record.status, update_record.lease_token
    INTO v_state, v_lease_token
  FROM public.anon_processed_updates AS update_record
  WHERE update_record.update_id = p_update_id;

  IF NOT FOUND THEN
    RETURN QUERY SELECT false, 'missing'::text, NULL::uuid;
  ELSE
    RETURN QUERY SELECT false, v_state, v_lease_token;
  END IF;
END;
$$;

DROP FUNCTION IF EXISTS public.anon_complete_update(bigint);
DROP FUNCTION IF EXISTS public.anon_complete_update(bigint, uuid);

CREATE FUNCTION public.anon_complete_update(
  p_update_id bigint,
  p_lease_token uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
  UPDATE public.anon_processed_updates
  SET status = 'processed',
      lease_until = NULL,
      lease_token = NULL
  WHERE update_id = p_update_id
    AND status = 'processing'
    AND lease_token = p_lease_token;

  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION public.anon_claim_update(bigint, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.anon_complete_update(bigint, uuid)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_claim_update(bigint, integer)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.anon_complete_update(bigint, uuid)
  TO service_role;
