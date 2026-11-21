import os
import random
import re
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("anon")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

COLORS = ["Red", "Blue", "Green", "Yellow", "Pink", "Purple", "Orange", "Brown", "Gray", "White", "Black", "Cyan", "Magenta", "Olive", "Teal", "Maroon"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
REST = f"{SUPABASE_URL}/rest/v1"
client = httpx.AsyncClient(timeout=25)

def db_headers(prefer="return=representation"):
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": prefer}

async def db_get(table, params):
    r = await client.get(f"{REST}/{table}", params=params, headers=db_headers())
    r.raise_for_status()
    return r.json()

async def db_post(table, payload, prefer="return=representation"):
    r = await client.post(f"{REST}/{table}", json=payload, headers=db_headers(prefer))
    r.raise_for_status()
    return r.json() if r.content else []

async def db_patch(table, params, payload):
    r = await client.patch(f"{REST}/{table}", params=params, json=payload, headers=db_headers())
    r.raise_for_status()
    return r.json() if r.content else []

async def tg(method, payload):
    r = await client.post(f"{API}/{method}", json=payload)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} rejected the request")
    return data.get("result")

async def get_user(uid):
    rows = await db_get("anon_users", {"telegram_user_id": f"eq.{uid}", "select": "*"})
    return rows[0] if rows else None

async def allocate_alias(uid):
    current = await get_user(uid)
    if current:
        return current, False
    for _ in range(1600):
        alias = f"{random.choice(COLORS)}-{random.randrange(100):02d}"
        try:
            rows = await db_post("anon_users", {"telegram_user_id": uid, "alias": alias})
            if rows:
                return rows[0], True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (409, 400):
                raise
            current = await get_user(uid)
            if current:
                return current, False
    raise RuntimeError("Alias pool exhausted")

async def set_block(uid, field, value):
    await db_patch("anon_users", {"telegram_user_id": f"eq.{uid}"}, {field: value})

async def send_user(uid, text):
    return await tg("sendMessage", {"chat_id": uid, "text": text})

async def react(chat_id, message_id, emoji):
    try:
        await tg("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]})
    except Exception:
        log.info("Reaction request failed")

def is_command(text):
    return isinstance(text, str) and text.startswith("/")

def command_name(text):
    if not isinstance(text, str):
        return ""
    return text.split(maxsplit=1)[0].split("@")[0].lower()

async def handle_start(message):
    uid = message["from"]["id"]
    user, created = await allocate_alias(uid)
    if created:
        await react(uid, message["message_id"], "❤️")
    return

async def admin_command(message):
    text = message.get("text", "")
    match = re.fullmatch(r"/block(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*", text, re.I)
    if match:
        alias = match.group(1).lower()
        rows = await db_get("anon_users", {"alias": f"ilike.{alias}", "select": "*"})
        if not rows:
            await send_user(ADMIN_ID, "Alias not found.")
            return True
        target = rows[0]
        await set_block(target["telegram_user_id"], "blocked_by_admin", True)
        try:
            await send_user(target["telegram_user_id"], "You blocked by Fwlai")
        except Exception:
            log.info("Blocked notice could not be delivered")
        await send_user(ADMIN_ID, f"You blocked: {target['alias']}")
        return True
    match = re.fullmatch(r"/unblock(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*", text, re.I)
    if match:
        alias = match.group(1).lower()
        rows = await db_get("anon_users", {"alias": f"ilike.{alias}", "select": "*"})
        if rows:
            await set_block(rows[0]["telegram_user_id"], "blocked_by_admin", False)
            await send_user(ADMIN_ID, f"Unblocked: {rows[0]['alias']}")
        else:
            await send_user(ADMIN_ID, "Alias not found.")
        return True
    return False

async def user_command(message, user):
    uid = message["from"]["id"]
    text = message.get("text", "")
    if text == "/block":
        await set_block(uid, "blocked_admin", True)
        await send_user(ADMIN_ID, f"You blocked by: {user['alias']}")
        await send_user(uid, "You blocked Fwlai use /unblock to unblock")
        return True
    if text == "/unblock":
        if not user.get("blocked_admin"):
            await react(uid, message["message_id"], "👎")
        else:
            await set_block(uid, "blocked_admin", False)
            await send_user(uid, "You unblocked Fwlai")
        return True
    return False

async def mark_update(update_id):
    rows = await db_post("anon_processed_updates", {"update_id": update_id}, "resolution=ignore-duplicates,return=representation")
    return bool(rows)

async def rate_ok(uid):
    r = await client.post(f"{REST}/rpc/anon_rate_check", json={"p_user_id": uid, "p_limit": 50, "p_window_seconds": 10}, headers=db_headers("return=minimal"))
    r.raise_for_status()
    return r.json() is True

async def map_admin_message(msg_id, sender_id):
    await db_post("anon_message_map", {"admin_message_id": msg_id, "sender_user_id": sender_id}, "resolution=ignore-duplicates,return=minimal")

async def relay_to_admin(message, user):
    uid = message["from"]["id"]
    now = datetime.now(timezone.utc)
    sessions = await db_get("anon_admin_sessions", {"sender_user_id": f"eq.{uid}", "select": "*"})
    new_session = not sessions
    if sessions:
        stamp = sessions[0]["last_message_at"].replace("Z", "+00:00")
        last = datetime.fromisoformat(stamp)
        new_session = (now - last).total_seconds() >= 600
    if new_session:
        await tg("sendMessage", {"chat_id": ADMIN_ID, "text": f"Alias: <code>{user['alias']}</code>", "parse_mode": "HTML"})
    copied = await tg("copyMessage", {"chat_id": ADMIN_ID, "from_chat_id": uid, "message_id": message["message_id"]})
    await map_admin_message(copied["message_id"], uid)
    await db_post("anon_admin_sessions", {"sender_user_id": uid, "last_message_at": now.isoformat()}, "resolution=merge-duplicates,return=minimal")
    await react(uid, message["message_id"], "👍")

@app.get("/")
async def root():
    return {"service": "Anon", "status": "running"}

@app.get("/health")
async def health():
    if not all([BOT_TOKEN, ADMIN_ID, WEBHOOK_SECRET, SUPABASE_URL, SUPABASE_KEY]):
        return {"status": "configuration_pending"}
    try:
        await db_get("anon_users", {"select": "telegram_user_id", "limit": "1"})
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")

@app.post("/telegram/webhook")
async def webhook(request: Request):
    if not WEBHOOK_SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    update = await request.json()
    update_id = update.get("update_id")
    if update_id is not None:
        try:
            if not await mark_update(update_id):
                return {"ok": True, "duplicate": True}
        except Exception:
            log.exception("Update deduplication failed")
            raise HTTPException(status_code=503, detail="temporary failure")
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}
    sender = message.get("from") or {}
    uid = sender.get("id")
    if not uid or sender.get("is_bot"):
        return {"ok": True}

    if uid == ADMIN_ID:
        text = message.get("text", "")
        if is_command(text):
            handled = await admin_command(message)
            if handled:
                return {"ok": True}
            return {"ok": True}
        reply = message.get("reply_to_message")
        if reply:
            rows = await db_get("anon_message_map", {"admin_message_id": f"eq.{reply['message_id']}", "select": "*"})
            if rows:
                try:
                    await tg("copyMessage", {"chat_id": rows[0]["sender_user_id"], "from_chat_id": ADMIN_ID, "message_id": message["message_id"]})
                except Exception:
                    log.warning("Admin reply delivery failed")
        return {"ok": True}

    text = message.get("text", "")
    if text == "/start":
        existing = await get_user(uid)
        if existing:
            return {"ok": True}
        user, created = await allocate_alias(uid)
        if created:
            await react(uid, message["message_id"], "❤️")
        return {"ok": True}

    user = await get_user(uid)
    if not user:
        user, created = await allocate_alias(uid)
    if await user_command(message, user):
        return {"ok": True}
    if is_command(text):
        return {"ok": True}
    if user.get("blocked_admin") or user.get("blocked_by_admin"):
        return {"ok": True}
    try:
        if not await rate_ok(uid):
            return {"ok": True}
    except Exception:
        log.exception("Rate-limit check failed")
        return {"ok": True}
    try:
        await relay_to_admin(message, user)
    except Exception:
        log.exception("Message relay failed")
        raise HTTPException(status_code=503, detail="relay failed")
    return {"ok": True}

@app.on_event("startup")
async def startup():
    if all([BOT_TOKEN, ADMIN_ID, WEBHOOK_SECRET, SUPABASE_URL, SUPABASE_KEY]):
        try:
            await db_get("anon_users", {"select": "telegram_user_id", "limit": "1"})
            await send_user(ADMIN_ID, "Anon started.")
            log.info("Startup check passed")
        except Exception:
            log.exception("Startup check failed")
    else:
        log.warning("Environment variables are not configured yet")

@app.on_event("shutdown")
async def shutdown():
    try:
        await send_user(ADMIN_ID, "Anon stopped.")
    except Exception:
        pass
    await client.aclose()
