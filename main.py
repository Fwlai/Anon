import os
import random
import re
import logging
import html
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("anon")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

COLORS = ["Red", "Blue", "Green", "Yellow", "Pink", "Purple", "Orange", "Brown", "Gray", "White", "Black", "Cyan", "Magenta", "Olive", "Teal", "Maroon", "Coral", "Crimson", "Gold", "Silver", "Indigo", "Violet", "Lavender", "Mint", "Lime", "Peach", "Amber", "Azure", "Navy", "Turquoise", "Rose", "Ruby", "Emerald", "Sapphire", "Lilac", "Ivory", "Charcoal", "Sky", "Plum", "Copper"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
REST = f"{SUPABASE_URL}/rest/v1"
client = httpx.AsyncClient(timeout=25)

def db_headers(prefer="return=representation"):
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": prefer}

async def db_get(table, params):
    r = await client.get(f"{REST}/{table}", params=params, headers=db_headers())
    r.raise_for_status()
    return r.json()

async def db_post(table, payload, prefer="return=representation", params=None):
    r = await client.post(f"{REST}/{table}", params=params, json=payload, headers=db_headers(prefer))
    r.raise_for_status()
    return r.json() if r.content else []

async def db_patch(table, params, payload):
    r = await client.patch(f"{REST}/{table}", params=params, json=payload, headers=db_headers())
    r.raise_for_status()
    return r.json() if r.content else []

async def tg(method, payload):
    try:
        r = await client.post(f"{API}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Telegram API {method} transport failed ({type(exc).__name__})") from None
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram API {method} returned HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram API {method} returned an invalid response") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} returned an error")
    return data.get("result")

async def get_user(uid):
    rows = await db_get("anon_users", {"telegram_user_id": f"eq.{uid}", "select": "*"})
    return rows[0] if rows else None

async def allocate_alias(uid):
    current = await get_user(uid)
    if current:
        return current, False
    for refresh in range(4):
        occupied = set()
        offset = 0
        while True:
            page = await db_get("anon_users", {"select": "alias", "order": "alias.asc", "limit": "1000", "offset": str(offset)})
            occupied.update(row["alias"].lower() for row in page)
            if len(page) < 1000:
                break
            offset += 1000
        candidates = [
            f"{color}-{number:02d}"
            for color in COLORS
            for number in range(100)
            if f"{color}-{number:02d}".lower() not in occupied
        ]
        random.shuffle(candidates)
        for alias in candidates:
            try:
                rows = await db_post("anon_users", {"telegram_user_id": uid, "alias": alias})
                if rows:
                    return rows[0], True
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                current = await get_user(uid)
                if current:
                    return current, False
                break
    raise RuntimeError("Alias pool exhausted")

async def set_block(uid, field, value):
    await db_patch("anon_users", {"telegram_user_id": f"eq.{uid}"}, {field: value})

async def send_user(uid, text, parse_mode=None):
    payload = {"chat_id": uid, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return await tg("sendMessage", payload)

async def remove_admin_keyboard():
    try:
        message = await tg("sendMessage", {"chat_id": ADMIN_ID, "text": "Keyboard removed.", "reply_markup": {"remove_keyboard": True}})
        try:
            await tg("deleteMessage", {"chat_id": ADMIN_ID, "message_id": message["message_id"]})
        except Exception:
            pass
    except Exception:
        log.info("Reply keyboard removal failed")

async def react(chat_id, message_id, emoji):
    try:
        await tg("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]})
        return True
    except Exception:
        log.info("Reaction request failed")
        return False

async def react_status(chat_id, message_id, success):
    if message_id is not None:
        await react(chat_id, message_id, "👍" if success else "👎")

def is_command(text):
    return isinstance(text, str) and text.startswith("/")


async def get_alias(alias):
    rows=await db_get("anon_users",{"alias":f"ilike.{alias}","select":"*"})
    return rows[0] if rows else None

async def admin_state():
    rows=await db_get("anon_admin_state",{"admin_user_id":f"eq.{ADMIN_ID}","select":"*"})
    return rows[0] if rows else {"active_user_id":None}

async def set_active(uid):
    await db_post("anon_admin_state",{"admin_user_id":ADMIN_ID,"active_user_id":uid},"resolution=merge-duplicates,return=minimal")

async def set_inbox_page(page):
    await db_post("anon_admin_state",{"admin_user_id":ADMIN_ID,"inbox_page":page},"resolution=merge-duplicates,return=minimal")

async def activity(uid):
    await db_post("anon_chat_activity",{"user_id":uid,"last_activity":datetime.now(timezone.utc).isoformat()},"resolution=merge-duplicates,return=minimal")

def kind_of(m):
    if m.get("media_group_id"): return "album"
    if m.get("animation"): return "GIF"
    if m.get("photo"): return "image"
    if m.get("video"): return "video"
    if m.get("voice"): return "voice"
    if m.get("audio"): return "audio"
    if m.get("document"): return "file"
    if m.get("sticker"): return "sticker"
    if m.get("video_note"): return "video message"
    if m.get("location") or m.get("venue"): return "location"
    if m.get("contact"): return "contact"
    if m.get("poll"): return "poll"
    return "text"

async def store_inbox(uid,m,kind):
    p={"user_id":uid,"user_message_id":m["message_id"],"message_kind":kind,"unread":True}
    if m.get("media_group_id"): p["media_group_id"]=str(m["media_group_id"])
    await db_post("anon_inbox_items",p,"resolution=ignore-duplicates,return=minimal",{"on_conflict":"user_id,user_message_id"})

async def map_outbound(user_mid,uid,admin_mid):
    await db_post("anon_outbound_map",{"user_message_id":user_mid,"sender_user_id":uid,"admin_message_id":admin_mid},"resolution=ignore-duplicates,return=minimal")

async def log_chat_id():
    rows = await db_get("anon_log_config", {"config_key": "eq.log_chat", "select": "chat_id", "limit": "1"})
    return int(rows[0]["chat_id"]) if rows else None

async def technical(text, reply_markup=None):
    try:
        chat_id = await log_chat_id()
        if not chat_id:
            log.info("TECH_LOG (group not registered): %s", text)
            return
        fence = chr(96) * 3
        await tg("sendMessage", {"chat_id": chat_id, "text": fence + "\nLog\n" + str(text) + "\n" + fence})
    except Exception as exc:
        log.warning("Technical log delivery failed (%s)", type(exc).__name__)

async def register_log_group(message):
    chat = message.get("chat", {})
    sender = message.get("from", {})
    if chat.get("type") not in ("group", "supergroup") or sender.get("id") != ADMIN_ID:
        return False
    try:
        if await log_chat_id():
            return True
        me = await tg("getMe", {})
        member = await tg("getChatMember", {"chat_id": chat["id"], "user_id": me["id"]})
        if member.get("status") not in ("administrator", "creator"):
            await tg("sendMessage", {"chat_id": chat["id"], "text": "I need administrator status before registration."})
            return True
        await db_post("anon_log_config", {"config_key": "log_chat", "chat_id": chat["id"], "registered_by": ADMIN_ID, "registered_at": datetime.now(timezone.utc).isoformat()}, "resolution=ignore-duplicates,return=minimal")
        await tg("sendMessage", {"chat_id": chat["id"], "text": "Log group registered."})
        await technical("Log group registered and ready.")
    except Exception as exc:
        log.warning("Log group registration failed (%s)", type(exc).__name__)
        try:
            await tg("sendMessage", {"chat_id": chat["id"], "text": "Registration failed. Check permissions and database connection."})
        except Exception:
            pass
    return True

async def edit_incoming_copy(m):
    uid=m["from"]["id"]
    rows=await db_get("anon_message_map",{"sender_user_id":f"eq.{uid}","user_message_id":f"eq.{m['message_id']}","select":"*"})
    if not rows:
        await react(uid,m["message_id"],"👎")
        return
    admin_mid=rows[0]["admin_message_id"]
    try:
        if "text" in m:
            await tg("editMessageText",{"chat_id":ADMIN_ID,"message_id":admin_mid,"text":m.get("text","")})
        else:
            await tg("editMessageCaption",{"chat_id":ADMIN_ID,"message_id":admin_mid,"caption":m.get("caption","")})
    except Exception:
        await react(ADMIN_ID,admin_mid,"👎")
        await technical("ERROR: Incoming edit relay failed")

async def relay_live(m,user,reply_to=None):
    uid=m["from"]["id"]; now=datetime.now(timezone.utc)
    sessions=await db_get("anon_admin_sessions",{"sender_user_id":f"eq.{uid}","select":"*"})
    header=not sessions
    if sessions:
        last=datetime.fromisoformat(sessions[0]["last_message_at"].replace("Z","+00:00"))
        header=(now-last).total_seconds()>=600
    if header:
        h=await tg("sendMessage",{"chat_id":ADMIN_ID,"text":f'<blockquote>Chat: <code>{html.escape(user["alias"])}</code></blockquote>',"parse_mode":"HTML"})
        await map_admin_message(h["message_id"],uid)
    p={"chat_id":ADMIN_ID,"from_chat_id":uid,"message_id":m["message_id"]}
    if reply_to: p["reply_parameters"]={"message_id":reply_to}
    cp=await tg("copyMessage",p)
    await map_admin_message(cp["message_id"],uid,m["message_id"])
    await db_post("anon_admin_sessions",{"sender_user_id":uid,"last_message_at":now.isoformat()},"resolution=merge-duplicates,return=minimal")
    await activity(uid)

async def handle_user(m,user):
    uid=m["from"]["id"]
    if user.get("blocked_by_admin"):
        await react_status(uid,m["message_id"],False); return
    if not await rate_ok(uid): return
    kind=kind_of(m); await store_inbox(uid,m,kind); await activity(uid)
    await technical("Incoming message: " + user["alias"] + " | " + kind)
    mapped=None; reply=m.get("reply_to_message")
    if reply:
        rows=await db_get("anon_outbound_map",{"user_message_id":f"eq.{reply['message_id']}","sender_user_id":f"eq.{uid}","select":"*"})
        if rows: mapped=rows[0]["admin_message_id"]
        else:
            rows=await db_get("anon_message_map",{"user_message_id":f"eq.{reply['message_id']}","sender_user_id":f"eq.{uid}","select":"*"})
            if rows: mapped=rows[0]["admin_message_id"]
    state=await admin_state()
    if reply and mapped is None:
        await react_status(uid,m["message_id"],False)
        await db_patch("anon_inbox_items",{"user_id":f"eq.{uid}","user_message_id":f"eq.{m['message_id']}"},{"unread":False})
        return
    if state.get("active_user_id")==uid or mapped:
        try:
            await relay_live(m,user,mapped)
            await db_patch("anon_inbox_items",{"user_id":f"eq.{uid}","user_message_id":f"eq.{m['message_id']}"},{"unread":False})
        except Exception:
            await react_status(uid,m["message_id"],False)
            await technical("ERROR: Live relay failed for " + user["alias"])
            raise
        return
    group=[]
    if m.get("media_group_id"):
        group=await db_get("anon_inbox_items",{"user_id":f"eq.{uid}","media_group_id":f"eq.{m['media_group_id']}","select":"id","limit":"10"})
    # inbox retrieval is manual via /i Alias

async def edit_admin_outbound_copy(m):
    rows=await db_get("anon_outbound_map",{"admin_message_id":f"eq.{m['message_id']}","select":"*"})
    if not rows:
        return False
    mapping=rows[0]
    uid=mapping["sender_user_id"]
    user_mid=mapping["user_message_id"]
    try:
        if "text" in m:
            await tg("editMessageText",{"chat_id":uid,"message_id":user_mid,"text":m.get("text","")})
        else:
            await tg("editMessageCaption",{"chat_id":uid,"message_id":user_mid,"caption":m.get("caption","")})
        return True
    except Exception:
        await react(ADMIN_ID,m["message_id"],"👎")
        await technical("ERROR: Admin edit relay failed")
        return False

async def send_to_user(m,uid,reply_to=None):
    p={"chat_id":uid,"from_chat_id":ADMIN_ID,"message_id":m["message_id"]}
    if reply_to: p["reply_parameters"]={"message_id":reply_to}
    cp=await tg("copyMessage",p)
    await map_outbound(cp["message_id"],uid,m["message_id"]); await activity(uid)

async def open_alias(alias,cmdid):
    u=await get_alias(alias)
    if not u or u.get("blocked_by_admin"):
        await react(ADMIN_ID,cmdid,"👎"); return
    uid=u["telegram_user_id"]; await set_active(uid); await activity(uid)
    await remove_admin_keyboard()
    await technical(f"Chat: <code>{html.escape(u['alias'])}</code>",{ "remove_keyboard": True})
    pending=await db_get("anon_inbox_items",{"user_id":f"eq.{uid}","unread":"eq.true","select":"*","order":"created_at.asc","limit":"1000"})
    ok=fail=0
    for item in pending:
        try:
            cp=await tg("copyMessage",{"chat_id":ADMIN_ID,"from_chat_id":uid,"message_id":item["user_message_id"]})
            await map_admin_message(cp["message_id"],uid,item["user_message_id"])
            await db_patch("anon_inbox_items",{"id":f"eq.{item['id']}"},{"unread":False}); ok+=1
        except Exception:
            fail+=1; await react(uid,item["user_message_id"],"👎")
    msg=f"Retrieved {ok} unread message(s)."
    if fail: msg+=f" {fail} message(s) unavailable."
    if ok or fail: await technical(msg)

async def inbox(page,cmdid):
    if page<1: await react(ADMIN_ID,cmdid,"👎"); return
    unread=await db_get("anon_inbox_items",{"unread":"eq.true","select":"user_id,created_at","order":"created_at.desc","limit":"1000"})
    counts={}; latest={}
    for x in unread:
        uid=x["user_id"]; counts[uid]=counts.get(uid,0)+1; latest.setdefault(uid,x["created_at"])
    activity_rows=await db_get("anon_chat_activity",{"select":"user_id,last_activity","order":"last_activity.desc","limit":"1000"})
    legacy=await db_get("anon_admin_sessions",{"select":"sender_user_id,last_message_at","order":"last_message_at.desc","limit":"1000"})
    ids=list(dict.fromkeys(sorted(counts,key=lambda uid:latest.get(uid,""),reverse=True)+[x["sender_user_id"] for x in legacy]+[x["user_id"] for x in activity_rows]))
    pages=max(1,(len(ids)+9)//10); page=min(page,pages); await set_inbox_page(page); start=(page-1)*10; selected=ids[start:start+10]
    lines=["<b>INBOX</b>",f"Unread: <b>{sum(counts.values())}</b> · Chats: {len(ids)} · Page {page}/{pages}",""]
    if not selected: lines.append("Inbox is empty.")
    for n,uid in enumerate(selected,start=start+1):
        u=await get_user(uid)
        if u:
            count=counts.get(uid,0); badge=f" · <b>{count} new</b>" if count else ""
            lines.append(f"{n}. <code>{html.escape(u['alias'])}</code>{badge}")
    lines.append("\nOpen: <code>/i Alias</code> · Close: <code>/c</code>")
    await technical("\n".join(lines),{"remove_keyboard":True})

async def route_admin(m):
    text=m.get("text","")
    state=await admin_state()
    if text.isdigit() and int(text)==int(state.get("inbox_page") or 1): return
    if text.startswith("/"):
        if await admin_command(m): return
        return
    state=await admin_state()
    active=state.get("active_user_id")
    reply=m.get("reply_to_message")
    if reply:
        rows=await db_get("anon_message_map",{"admin_message_id":f"eq.{reply['message_id']}","select":"*"})
        if rows:
            mapping=rows[0]
            if active != mapping["sender_user_id"]:
                await react(ADMIN_ID,m["message_id"],"👎"); return
            u=await get_user(mapping["sender_user_id"])
            if u and not u.get("blocked_by_admin"):
                try: await send_to_user(m,mapping["sender_user_id"],mapping.get("user_message_id"))
                except Exception: await react(ADMIN_ID,m["message_id"],"👎")
            else: await react(ADMIN_ID,m["message_id"],"👎")
            return
        rows=await db_get("anon_outbound_map",{"admin_message_id":f"eq.{reply['message_id']}","select":"*"})
        if rows:
            mapping=rows[0]
            if active != mapping["sender_user_id"]:
                await react(ADMIN_ID,m["message_id"],"👎"); return
            try: await send_to_user(m,mapping["sender_user_id"],mapping["user_message_id"])
            except Exception: await react(ADMIN_ID,m["message_id"],"👎")
            return
        await react(ADMIN_ID,m["message_id"],"👎"); return
    state=await admin_state(); uid=state.get("active_user_id")
    if uid:
        u=await get_user(uid)
        if u and not u.get("blocked_by_admin"):
            try: await send_to_user(m,uid)
            except Exception: await react(ADMIN_ID,m["message_id"],"👎")
            return
    await technical("No chat open. Use /i Alias.")

async def admin_command(message):
    text = message.get("text", "")
    match=re.fullmatch(r"/i(?:@\w+)?(?:\s+(.+))?\s*",text,re.I)
    if match:
        arg=(match.group(1) or "").strip()
        if not arg: await technical("Inbox list disabled. Use /i Alias to open a conversation.")
        elif re.fullmatch(r"[A-Za-z]+-\d{2}",arg): await open_alias(arg,message["message_id"])
        else: await react(ADMIN_ID,message["message_id"],"👎")
        return True
    if re.fullmatch(r"/c(?:@\w+)?\s*",text,re.I):
        await set_active(None); await technical("Chat closed.")
        return True
    match = re.fullmatch(r"/block(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*", text, re.I)
    if match:
        alias = match.group(1).lower()
        rows = await db_get("anon_users", {"alias": f"ilike.{alias}", "select": "*"})
        if not rows:
            await technical("Alias not found.")
            return True
        target = rows[0]
        await set_block(target["telegram_user_id"], "blocked_by_admin", True)
        try:
            await send_user(target["telegram_user_id"], "You blocked by Fwlai")
        except Exception:
            log.info("Blocked notice could not be delivered")
        await technical(f"Blocked: <code>{html.escape(target['alias'])}</code>")
        return True
    match = re.fullmatch(r"/unblock(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*", text, re.I)
    if match:
        alias = match.group(1).lower()
        rows = await db_get("anon_users", {"alias": f"ilike.{alias}", "select": "*"})
        if rows:
            target = rows[0]
            await set_block(target["telegram_user_id"], "blocked_by_admin", False)
            try:
                await send_user(target["telegram_user_id"], "You are unblocked by Fwlai")
            except Exception:
                log.info("Unblock notice could not be delivered")
            await technical(f"Unblocked: <code>{html.escape(target['alias'])}</code>")
        else:
            await technical("Alias not found.")
        return True
    return False

async def mark_update(update_id):
    rows = await db_post("anon_processed_updates", {"update_id": update_id}, "resolution=ignore-duplicates,return=representation")
    return bool(rows)

async def unmark_update(update_id):
    r = await client.delete(f"{REST}/anon_processed_updates", params={"update_id": f"eq.{update_id}"}, headers=db_headers("return=minimal"))
    r.raise_for_status()

async def rate_ok(uid):
    r = await client.post(f"{REST}/rpc/anon_rate_check", json={"p_user_id": uid, "p_limit": 50, "p_window_seconds": 10}, headers=db_headers("return=representation"))
    r.raise_for_status()
    return r.json() is True

async def map_admin_message(msg_id, sender_id, user_message_id=None):
    payload = {"admin_message_id": msg_id, "sender_user_id": sender_id}
    if user_message_id is not None:
        payload["user_message_id"] = user_message_id
    await db_post("anon_message_map", payload, "resolution=ignore-duplicates,return=minimal")

app = FastAPI()

@app.api_route("/", methods=["GET", "HEAD"])
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
    membership = update.get("my_chat_member")
    if membership:
        group = membership.get("chat", {})
        new_status = (membership.get("new_chat_member") or {}).get("status")
        if group.get("type") in ("group", "supergroup") and new_status in ("member", "administrator"):
            registered = await log_chat_id()
            if registered and int(group.get("id", 0)) != registered:
                try:
                    await tg("leaveChat", {"chat_id": group["id"]})
                except Exception:
                    log.info("Could not silently leave unregistered group")
        return {"ok": True}
    edited = update.get("edited_message")
    message = update.get("message") or edited
    if not message:
        return {"ok": True}
    sender = message.get("from") or {}
    uid = sender.get("id")
    if not uid or sender.get("is_bot"):
        return {"ok": True}

    chat = message.get("chat", {})
    if chat.get("type") in ("group", "supergroup"):
        registered = await log_chat_id()
        if registered and int(chat.get("id", 0)) != registered:
            try:
                await tg("leaveChat", {"chat_id": chat["id"]})
            except Exception:
                log.info("Could not silently leave unregistered group")
            return {"ok": True}
        if not registered and isinstance(message.get("text"), str) and re.match(r"^/log(?:@\w+)?\s*$", message["text"], re.I):
            await register_log_group(message)
        return {"ok": True}

    if uid == ADMIN_ID:
        if edited:
            try:
                await edit_admin_outbound_copy(message)
            except Exception:
                log.exception("Admin edit relay failed")
                await technical("ERROR: Admin edit relay failed")
            return {"ok": True}
        try:
            await route_admin(message)
        except Exception:
            log.exception("Admin routing failed")
            await technical("ERROR: Admin routing failed")
            if update_id is not None: await unmark_update(update_id)
            raise HTTPException(status_code=503, detail="admin routing failed")
        return {"ok": True}

    if edited:
        try:
            await edit_incoming_copy(message)
        except Exception:
            log.exception("Incoming edit relay failed")
        return {"ok": True}
    text = message.get("text", "")
    if text == "/start":
        try:
            existing = await get_user(uid)
            if existing:
                user, created = existing, False
            else:
                user, created = await allocate_alias(uid)
            if created:
                await technical("New user registered: " + user["alias"])
            if not await react(uid, message["message_id"], "❤️"):
                await react(uid, message["message_id"], "👎")
        except Exception:
            log.exception("Start flow failed")
            await react(uid, message["message_id"], "👎")
            if update_id is not None:
                await unmark_update(update_id)
            raise HTTPException(status_code=503, detail="start failed")
        return {"ok": True}

    user = await get_user(uid)
    if not user:
        try:
            user, created = await allocate_alias(uid)
        except Exception:
            log.exception("Alias allocation failed")
            await technical("ERROR: Alias allocation failed")
            if update_id is not None:
                await unmark_update(update_id)
            raise HTTPException(status_code=503, detail="alias allocation failed")
        if created:
            await technical("New user registered: " + user["alias"])
    if is_command(text):
        return {"ok": True}
    try:
        await handle_user(message, user)
    except Exception:
        log.exception("Message relay failed")
        if update_id is not None:
            await unmark_update(update_id)
        raise HTTPException(status_code=503, detail="relay failed")
    return {"ok": True}

@app.on_event("startup")
async def startup():
    if all([BOT_TOKEN, ADMIN_ID, WEBHOOK_SECRET, SUPABASE_URL, SUPABASE_KEY]):
        try:
            await db_get("anon_users", {"select": "telegram_user_id", "limit": "1"})
            base_url = os.getenv("RENDER_EXTERNAL_URL", "https://anon-mr5s.onrender.com").rstrip("/")
            await tg("setWebhook", {
                "url": f"{base_url}/telegram/webhook",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": ["message", "edited_message", "my_chat_member"]
            })
            await technical("Bot started. Webhook is configured.")
            log.info("Startup check and webhook setup passed")
        except Exception:
            log.error("Startup or webhook setup failed; details suppressed to protect bot token")
            try:
                await technical("ERROR: Startup or webhook setup failed. Check Render logs.")
            except Exception:
                pass
    else:
        log.warning("Environment variables are not configured yet")

@app.on_event("shutdown")
async def shutdown():
    if BOT_TOKEN and ADMIN_ID:
        try:
            await technical("Bot stopped.")
        except Exception:
            pass
    await client.aclose()
