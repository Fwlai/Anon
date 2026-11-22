# Logging

Anon writes structured JSON records to Render stdout and forwards technical events to the registered Telegram log group.

## Telegram Rich Messages

Telegram log notifications use the official Bot API `sendRichMessage` method with an `InputRichMessage.markdown` payload.

Each notification contains:
- A compact inline-code event name as its heading
- The complete structured event record in a fenced `json` code block
- Syntax-language metadata (`json`) so supported Telegram clients can render the record as formatted code

The JSON is serialized with UTF-8 characters preserved and indentation for readability. If a record exceeds the conservative Telegram display budget, its details are replaced with a truncation marker and the names of the available fields before sending.

The bot does not fall back to a regular `sendMessage` if rich-message delivery fails. This is intentional: a timeout or lost response can mean Telegram accepted the first request, and retrying through another method could create duplicate log notifications. Delivery failures are recorded in Render stdout instead.

Telegram Rich Messages were introduced in Bot API 10.1. The implementation calls the Bot API directly over HTTPS, so it does not depend on a framework wrapper supporting the new method.

## Log content and privacy

The record includes:
- UTC timestamp, severity, service, environment, and event name
- Request correlation ID and Telegram update ID when applicable
- Render service, instance, Git commit, branch, and external URL metadata when Render provides them
- Structured event details
- Provider, operation, HTTP status, error code, and a redacted error message for Telegram and Supabase API failures

HTTP requests are logged with method, path, response status, and duration. Successful routine requests go to Render stdout; failed or unusually slow requests are also forwarded to the Telegram log group.

Private Telegram message text, captions, and media contents are never included in technical logs. Render webhook details are sanitized before being recorded.

## Render platform events

The app exposes `POST /render/webhook` for signed Render service-event notifications.

1. In Render, open **Environment** for the `anon` service and add `RENDER_WEBHOOK_SECRET` using the signing secret from the Render webhook settings. Keep the secret private.
2. Optionally add `RENDER_API_KEY` with a Render API key. When present, Anon calls the Render **Retrieve event** endpoint to attach the event's expanded `details` object.
3. In the Render workspace, open **Integrations → Webhooks → Create Webhook**. Set the destination URL to `https://anon-mr5s.onrender.com/render/webhook`.
4. Select events such as `build_started`, `build_ended`, `deploy_started`, `deploy_ended`, `server_failed`, `server_available`, `server_restarted`, `service_suspended`, and `service_resumed`.

Render webhook notifications use Standard Webhooks signatures. Anon verifies the HMAC signature against the raw request body and rejects timestamps outside the five-minute window. Events for a different service are ignored when `RENDER_SERVICE_ID` is available.

Render workspace webhooks require a Pro plan or higher. If **Integrations → Webhooks** is unavailable, the workspace plan must be upgraded before platform events can be pushed to Anon.

These webhooks provide platform lifecycle events and event metadata, not a live copy of every raw build-log line. Full build and deploy output remains available in the Render dashboard. Application stdout logs are structured JSON and are searchable in Render Logs.
