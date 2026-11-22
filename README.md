# Anon

One-way anonymous Telegram relay: user messages are delivered to the admin immediately, and only the admin can reply to users.

## Features
- Permanent unique color aliases (40 colors × 100 numbers)
- Every incoming user message is relayed to the admin immediately, whether or not a destination is selected with `/i`
- Reply to any relayed user message to answer that message's author
- `/i Alias` selects a user so the admin can send messages without using Reply
- `/c` clears the selected destination
- Text, photos, videos, documents, audio, voice messages, stickers, animations, locations, contacts, polls, and other Telegram message types are copied through Telegram's Bot API
- Reply mappings are retained for both incoming messages and admin-to-user copies
- No inbox, unread state, queued messages, or delayed inbox retrieval
- Only a dislike reaction is used when an admin message has no destination or a transfer fails
- Technical logs are sent only to a registered group
- Technical logs contain metadata only; private message contents are never logged
- Users can be blocked/unblocked by the admin
- Webhook secret validation, update deduplication, and burst throttling

## Commands (admin only)
- `/i Alias` — select a recipient for messages sent without Reply
- `/c` — clear the selected recipient
- `/block Alias` and `/unblock Alias` — moderation

## Message transfer
Incoming messages are copied to the admin chat as soon as their update is handled. Replying to a copied incoming message sends the admin's message to that original sender. This works independently of the currently selected `/i Alias`.

When no message is being replied to, the selected `/i Alias` is used as the recipient. If there is no selected alias, the bot reacts with a dislike instead of guessing a recipient.

Transfers use Telegram's `copyMessage` method, so the original message content and supported media are preserved without downloading files to the bot server. Telegram itself excludes certain special/service message types from copying; normal text, photos, videos, files, music/audio, voice messages, stickers, animations, and other standard message types are supported.

## Required environment variables
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_ID`
- `TELEGRAM_WEBHOOK_SECRET`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Technical log group
The technical log destination is already registered in `anon_log_config`. The bot is intended to remain only in that group and automatically leaves any other group it is added to.

## Deploy
Render web service:
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

The service exposes `/` and `/health`.
