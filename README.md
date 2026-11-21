# Anon

Minimal anonymous Telegram message relay.

## Features
- Permanent unique aliases in the `Color-00` to `Color-99` style, drawn from 16 colors (1600 total)
- First-time `/start` reaction only; returning `/start` is silent
- User-to-admin anonymous message delivery using Telegram copyMessage
- Reply mapping so admin replies to copied messages reach the original sender
- User/admin block and unblock commands
- Admin alias headers separated from messages, repeated after 10 minutes of inactivity
- Severe burst throttling
- Webhook secret validation and duplicate-update tracking
- No message body archive; only alias/user state, message ID mappings, and minimal operational metadata

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_ID`
- `TELEGRAM_WEBHOOK_SECRET`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Deploy
Render Web Service, Python runtime:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Set Telegram webhook URL to `/telegram/webhook` and use the same secret as `TELEGRAM_WEBHOOK_SECRET`. The service provides `/` and `/health`.

Note: Telegram Bot API cannot provide reliable reaction-update mirroring from the admin's reaction on the copied message, so that feature is not implemented.