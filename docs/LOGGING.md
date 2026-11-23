# Logging

Anon keeps structured application records in Render stdout for troubleshooting, with private identifiers and platform metadata redacted.

## Telegram log group

The group receives only startup/shutdown and user-registration events, errors, and a small allowlist of actionable warnings. Routine message events and ordinary forwarding successes are not sent to the group. Repeated non-critical events are suppressed for five minutes per event type.

Notifications are plain, concise text rather than expanded JSON or Rich Messages. They contain the event name and timestamp, with only short safe diagnostic fields such as provider, operation, HTTP status, error code, or a sanitized error summary when relevant.

Aliases, numeric Telegram IDs, message IDs, project/service/deployment/instance metadata, repository/branch/commit details, and Render metadata are removed from log records and group notifications. Service/deployment information is available only through the admin-only `/health` command in the configured log group.

## Health command

The admin can use `/health` in the registered log group to check:
- Database connectivity
- Telegram webhook status and pending update count
- Album recovery queue state
- Error/warning counters since process start
- Uptime and deployment/service metadata

The command is ignored in private chats and never relayed to anonymous users.

## Render platform events

The app exposes `POST /render/webhook` for signed Render service-event notifications.

1. In Render, open **Environment** for the `anon` service and add `RENDER_WEBHOOK_SECRET` using the signing secret from the Render webhook settings. Keep the secret private.
2. Optionally add `RENDER_API_KEY` with a Render API key. When present, Anon calls the Render **Retrieve event** endpoint to attach the event's expanded `details` object.
3. In the Render workspace, open **Integrations → Webhooks → Create Webhook**. Set the destination URL to `https://anon-mr5s.onrender.com/render/webhook`.
4. Select lifecycle events such as build/deploy start and end, server failure/availability/restart, and service suspension/resumption.

Render webhook notifications use Standard Webhooks signatures. Anon verifies the HMAC signature against the raw request body and rejects timestamps outside the five-minute window. Events for a different service are ignored when `RENDER_SERVICE_ID` is available.

Render workspace webhooks require a Pro plan or higher. Full build/deploy output remains in the Render dashboard; application stdout logs are structured JSON and searchable in Render Logs.
