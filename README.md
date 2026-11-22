# Anon

Anonymous Telegram relay with an admin-only inbox and chat switching.

## Features
- Permanent unique color aliases (40 colors × 100 numbers)
- Admin chat controls: `/i Alias` opens a conversation; `/c` closes it
- Technical logs are sent only to a registered group, each in a `Log` code block
- Register the log group with `/setlog` after adding the bot as an administrator
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
- `/setlog` — register the current group as the technical log destination (run as the configured admin; bot must be a group admin)
- `/c` — close current chat
- `/block Alias` and `/unblock Alias` — moderation

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_ID`
- `TELEGRAM_WEBHOOK_SECRET`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Technical log setup
1. Create a group and add the bot.
2. Promote the bot to administrator.
3. Send `/setlog` in the group from the configured admin account.
4. The bot stores the group's numeric Telegram chat ID in `anon_log_config`. Invite-link changes do not affect logging.

Logs contain event metadata (such as alias and message type) and error notices only. Private message contents are never included in technical logs.

## Deploy
Render web service:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

The webhook is registered on startup. The service exposes `/` and `/health`.
