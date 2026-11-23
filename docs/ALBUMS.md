# Album delivery and recovery

Album parts are persisted in `anon_album_buffer` before the webhook is acknowledged. The Render process uses a background worker for delivery, while a periodic recovery loop scans the database for quiet groups still marked `pending`. This makes queued albums survive a process restart without depending on an in-memory task or cached media object. The worker waits for an eight-second quiet window after the most recently received part before claiming a group, reducing premature closure when Telegram webhook updates arrive with a delay.

When a worker claims a group, PostgreSQL records `claimed_at` and changes all its parts to `processing` under the same advisory lock used by insertion and completion. The migration also adds covering indexes for the album and message-map user foreign keys to keep deletion cascades efficient. A normal Telegram 429 response or a database failure detected before any Telegram send releases the group back to `pending`, allowing the worker or recovery loop to retry it. Successful delivery stores all message mappings atomically.

## Ambiguous outcomes

Telegram does not provide an idempotency key for copy/send operations. If a process dies while a group is `processing`, the database cannot know whether Telegram accepted the outgoing album. After the five-minute processing lease expires, the recovery function changes the group to `unknown`. Unknown groups are deliberately not replayed automatically because that could send duplicate albums. The `/health` report flags this state for manual review.

A permanently rejected Telegram request is recorded as `failed`. A request with an uncertain network outcome is recorded as `delivered_unmapped` while the worker is alive, or `unknown` if the process disappears before it can persist the result.

## Deployment order

Apply the existing four migrations in their documented order, then apply `20261123_anon_album_recovery.sql`. Only then deploy the application code containing the recovery loop. The new migration is additive and repeatable; it does not contact Telegram or modify the running Render service.
