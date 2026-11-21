import os
import random
import re
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("anon")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_ID"])
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
COLORS = ["Red", "Blue", "Green", "Yellow", "Pink", "Purple", "Orange", "Brown", "Gray", "White", "Black", "Cyan", "Magenta", "Olive", "Teal", "Maroon"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
REST = f"{SUPABASE_URL}/rest/v1"
app = FastAPI()
client = httpx.AsyncClient(timeout=20)

def db_headers(prefer="return=representation"):
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": prefer}

async def db_get(table, params):
    r = await client.get(f"{REST}/{table}", params=params, headers=db_headers())
    r.raise_for_status()
    return r.json()

async def db_post(table, payload, prefer="return=representation"):
    r = await client.post(f"{REST}/{table}", json=payload, headers=db_headers(prefer))
    r.raise_for_status()
    return r.json()

async def db_patch(table, params, payload):
    r = await client.patch(f"{REST}/{table}", params=params, json=payload, headers=db_headers())
    r.raise_for_status()
    return r.json()

async def tg(method, payload):
    r = await client.post(f"{API}/{method}", json=payload)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed")
    return data.get("result")

async def get_user(uid):
    rows = await db_get("anon_users", {"telegram_user_id": f"eq.{uid}", "select": "*"})
    return rows[0] if rows else None

async def allocate_alias(uid):
    existing = await get_user(uid)
    if existing:
        return existing
    for _ in range(1600):
        alias = f"{random.choice(COLORS)}-{random.randrange(100):02d}"
        try:
            rows = await db_post("anon_users", {"telegram_user_id": uid, "alias": alias})
            if rows:
                return rows[0]
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (409, 400):
                raise
            existing = await get_user(uid)
            if existing:
                return existing
    raise RuntimeError("Alias pool exhausted")

async def set_block(uid, column, value):
    await db_patch("anon_users", {"telegram_user_id": f"eq.{uid}"}, {column: value})

async def send_user(uid, text):
    return await tg("sendMessage", {"chat_id": uid, "text": text})

async def react(chat_id, message_id, emoji):
    try:
        await tg("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]})
    except Exception:
        log.info("Reaction not supported or failed")

def is_command(text):
    return isinstance(text, str) and text.startswith("/")

async def handle_start(message):
    uid = message["from"]["id"]
    user = await allocate_alias(uid)
    if user.get("created_at") and user.get("last_session_at") is None:
        await react(uid, message["message_id"], "❤️")
    # start is intentionally silent for returning users
    return

async def admin_command(message):
    text = message.get("text", "")
    match = re.match(r"^/block(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*$", text, re.I)
    if match:
        alias = match.group(1)
        rows = await db_get("anon_users", {"alias": f"eq.{alias}", "select": "*"})
        if not rows:
            await send_user(ADMIN_ID, "Alias not found.")
            return
        target = rows[0]
        await set_block(target["telegram_user_id"], "blocked_by_admin", True)
        await send_user(target["telegram_user_id"], "You blocked by Fwlai")
        await send_user(ADMIN_ID, f"You blocked: {target['alias']}")
        return
    if re.match(r"^/unblock(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*$", text, re.I):
        alias = re.match(r"^/unblock(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*$", text, re.I).group(1)
        rows = await db_get("anon_users", {"alias": f"eq.{alias}", "select": "*"})
        if rows:
            await set_block(rows[0]["telegram_user_id"], "blocked_by_admin", False)
            await send_user(ADMIN_ID, f"Unblocked: {rows[0]['alias']}")
        else:
            await send_user(ADMIN_ID, "Alias not found.")
        return

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

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/telegram/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    update = await request.json()
    message = update.get("message")
    if not message:
        return {"ok": True}
    uid = message.get("from", {}).get("id")
    if not uid:
        return {"ok": True}
    if uid == ADMIN_ID:
        await admin_command(message)
        # Reply forwarding: map admin's replied-to message ID to anonymous sender.
        reply = message.get("reply_to_message")
        if reply and not is_command(message.get("text", "")):
            mapped = await db_get("anon_message_map", {"admin_message_id": f"eq.{reply['message_id']}", "select": "*"})
            if mapped:
                try:
                    await tg("copyMessage", {"chat_id": mapped[0]["sender_user_id"], "from_chat_id": ADMIN_ID, "message_id": message["message_id"]})
                except Exception:
                    log.warning("Reply delivery failed")
        return {"ok": True}
    if message.get("text") == "/start":
        user = await allocate_alias(uid)
        # Add ❤️ only for the first start; the creation timestamp is stable but
        # this is refined in a subsequent schema migration to avoid race ambiguity.
        if user.get("created_at"):
            await react(uid, message["message_id"], "❤️")
        return {"ok": True}
    user = await get_user(uid)
    if not user:
        user = await allocate_alias(uid)
        await react(uid, message["message_id"], "❤️")
    if await user_command(message, user):
        return {"ok": True}
    if user.get("blocked_admin") or user.get("blocked_by_admin"):
        return {"ok": True}
    text = message.get("text", "")
    if is_command(text):
        return {"ok": True}
    sessions = await db_get("anon_admin_sessions", {"sender_user_id": f"eq.{uid}", "select": "*"})
    now = datetime.now(timezone.utc)
    if not sessions or (now - datetime.fromisoformat(sessions[0]["last_message_at"].replace("Z", "+00:00"))).total_seconds() >= 600:
        await tg("sendMessage", {"chat_id": ADMIN_ID, "text": f"Alias: <code>{user['alias']}</code>", "parse_mode": "HTML"})
    # Copy the user's message so Telegram-native media/message types remain intact.
    copied = await tg("copyMessage", {"chat_id": ADMIN_ID, "from_chat_id": uid, "message_id": message["message_id"]})
    await db_post("anon_message_map", {"admin_message_id": copied["message_id"], "sender_user_id": uid})
    await db_post("anon_admin_sessions", {"sender_user_id": uid, "last_message_at": now.isoformat()}, prefer="resolution=merge-duplicates,return=representation")
    await react(uid, message["message_id"], "👍")
    return {"ok": True}

@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
