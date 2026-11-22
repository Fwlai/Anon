-- Remove the obsolete unread queue and inbox-only activity metadata.
-- Message routing is now immediate; anon_admin_state is retained only for the
-- currently selected /i Alias recipient.
DROP TABLE IF EXISTS public.anon_inbox_items;
DROP TABLE IF EXISTS public.anon_chat_activity;

ALTER TABLE public.anon_admin_state
  DROP COLUMN IF EXISTS inbox_page;
