# Album delivery and recovery

Album parts are persisted in `anon_album_buffer` before the webhook is acknowledged. The Render process uses a background worker for delivery, while a periodic recovery loop scans the database for quiet groups still marked `pending`. This makes queued albums survive a process restart without depending on an in-memory task or cached media object. The worker waits for an eight-second quiet window after the most recently received part before claiming a group, reducing premature closure when Telegram webhook updates arrive with a delay. Album delivery now consistently uses Telegram's album-aware copyMessages method, preserving the source grouping and message order without relying on an in-memory media cache, including after Render restarts.

When a worker claims a group, PostgreSQL records `claimed_at` and changes all its parts to `processing` under the same advisory lock used by insertion and completion. The migration also adds covering indexes for the album and message-map user foreign keys to keep deletion cascades efficient. A normal Telegram 429 response or a database failure detected before any Telegram send releases the group back to `pending`, allowing the worker or recovery loop to retry it. Successful delivery stores all message mappings atomically. If the first admin album part has no reply target but a later part has one, the route reservation fills the missing reply target without changing the original recipient or replacing a reply target already stored.

## Ambiguous outcomes

Telegram does not provide an idempotency key for copy/send operations. If a process dies while a group is `processing`, the database cannot know whether Telegram accepted the outgoing album. After the five-minute processing lease expires, the recovery function changes the group to `unknown`. Unknown groups are deliberately not replayed automatically because that could send duplicate albums. The `/health` report flags this state for manual review.

A permanently rejected Telegram request is recorded as `failed`. A request with an uncertain network outcome is recorded as `delivered_unmapped` while the worker is alive, or `unknown` if the process disappears before it can persist the result.

## Deployment order

For a fresh database, apply the four `20261122_*.sql` migrations in filename order, then apply `20261123_anon_album_recovery.sql`.

For the current production database, a read-only inspection on 2026-11-23 confirmed that the album tables, album RPCs, webhook lease columns, and update-lease RPCs from the first four reliability migrations already exist. Their exact filenames are not present in the Supabase migration-history list, so the history is incomplete relative to the live schema. The fifth migration's `claimed_at` column and `anon_expire_stale_albums` function are not present yet. Reconcile that history deliberately; do not assume the ledger alone describes the live schema.

The earlier migrations are repeatable in CI, but they replace functions and triggers. Do not rerun them against live traffic without a controlled maintenance window. Apply the recovery migration while album workers are drained or webhook intake is paused, then deploy the application code containing the recovery loop. The migration does not contact Telegram. It changes legacy `processing` album rows without a claim timestamp to `unknown` so they cannot be replayed blindly.
