ALTER TABLE public.anon_processed_updates
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'processed';

ALTER TABLE public.anon_processed_updates
  ADD COLUMN IF NOT EXISTS lease_until timestamptz;

DO $$
BEGIN
  ALTER TABLE public.anon_processed_updates
    ADD CONSTRAINT anon_processed_updates_status_check
    CHECK (status IN ('processing', 'processed'));
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

CREATE OR REPLACE FUNCTION public.anon_claim_update(
  p_update_id bigint,
  p_lease_seconds integer DEFAULT 300
)
RETURNS TABLE (
  claimed boolean,
  state text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
  v_claimed_id bigint;
  v_state text;
BEGIN
  INSERT INTO public.anon_processed_updates AS update_record (
    update_id,
    status,
    lease_until
  )
  VALUES (
    p_update_id,
    'processing',
    now() + make_interval(secs => GREATEST(p_lease_seconds, 30))
  )
  ON CONFLICT (update_id)
  DO UPDATE SET
    status = 'processing',
    lease_until = EXCLUDED.lease_until,
    created_at = now()
  WHERE update_record.status = 'processing'
    AND (
      update_record.lease_until IS NULL
      OR update_record.lease_until <= now()
    )
  RETURNING update_record.update_id INTO v_claimed_id;

  IF FOUND THEN
    RETURN QUERY SELECT true, 'claimed'::text;
    RETURN;
  END IF;

  SELECT update_record.status
    INTO v_state
  FROM public.anon_processed_updates AS update_record
  WHERE update_record.update_id = p_update_id;

  IF NOT FOUND THEN
    RETURN QUERY SELECT false, 'missing'::text;
  ELSE
    RETURN QUERY SELECT false, v_state;
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.anon_complete_update(
  p_update_id bigint
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
BEGIN
  UPDATE public.anon_processed_updates
  SET status = 'processed',
      lease_until = NULL
  WHERE update_id = p_update_id
    AND status = 'processing';

  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION public.anon_claim_update(bigint, integer)
  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.anon_complete_update(bigint)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.anon_claim_update(bigint, integer)
  TO service_role;
GRANT EXECUTE ON FUNCTION public.anon_complete_update(bigint)
  TO service_role;
