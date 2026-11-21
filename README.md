# Anon

Minimal anonymous Telegram message relay.

## Features
- Permanent unique Color-XX aliases
- User-to-admin anonymous message delivery
- Reply mapping (admin replies to copied messages)
- User/admin block and unblock commands
- Session header after 10-minute inactivity
- No message body archive
- Webhook secret validation

## Required environment variables
- TELEGRAM_BOT_TOKEN
- TELEGRAM_ADMIN_ID
- TELEGRAM_WEBHOOK_SECRET
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY

## Deploy
Render Web Service, Python runtime:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Set a webhook URL ending in `/telegram/webhook` and use the same secret as `TELEGRAM_WEBHOOK_SECRET`. The service provides `/health`.

Note: message contents are not archived. Minimal mapping rows are stored only to support replying. Bot API limitations mean admin reaction syncing (Seen) is not implemented.
