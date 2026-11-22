# Anon

Anonymous Telegram relay with an admin-only inbox and chat switching.

## Features
- Permanent unique color aliases (40 colors × 100 numbers)
- Admin chat controls: `/i Alias` opens a conversation; `/c` closes it
- Technical logs are sent only to a registered group, each in a `Log` code block
- Technical logs are restricted to the pre-registered group; the bot leaves all other groups automatically
- Open a conversation with `/i Alias`; close the selected chat with `/c`
- New-message notifications hide text previews behind Telegram spoilers and use media labels (image, album, video, voice, GIF, file, etc.)
- Incoming messages are copied into the admin chat only when the conversation is open; otherwise they remain unread until opened
- Reply mapping is preserved for copies of both inbound and outbound messages, allowing replies to target the exact counterpart message
- Switching chats does not erase unread messages or previous message mappings
- No positive delivery reactions; only a dislike is used when a message cannot be transferred or a requested alias is invalid
- User messages are not archived as text in the database; only Telegram message IDs, routing metadata, unread state, aliases, and activity are stored
- Admin can block/unblock aliases; users do not have block/unblock commands
- Severe burst throttling, webhook secret validation, and update deduplication
- HTTP request logging suppressed to avoid exposing bot-token URLs

## Commands (admin only)
- `/i Alias` — open/switch chat and deliver unread messages
- `/c` — close current chat
- `/block Alias` and `/unblock Alias` — moderation

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_ID`
- `TELEGRAM_WEBHOOK_SECRET`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Technical log group
The technical log destination is already registered in `anon_log_config`. The bot is intended to remain only in that group and automatically leaves any other group it is added to. The group ID is preserved in the database.

Logs contain event metadata (such as alias and message type) and error notices only. Private message contents are never included in technical logs.

## Deploy
Render web service:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

The webhook is registered on startup. The service exposes `/` and `/health`.
