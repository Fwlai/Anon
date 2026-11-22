import os
import random
import re
import logging
import html
import json
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException

class JSONLogFormatter(logging.Formatter):
    def format(self, record):
        if isinstance(record.msg, dict):
            payload = dict(record.msg)
        else:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "service": "anon",
                "environment": "render" if os.getenv("RENDER") == "true" else "local",
                "event": "application.log",
                "message": record.getMessage(),
            }
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler()], force=True)
for handler in logging.getLogger().handlers:
    handler.setFormatter(JSONLogFormatter())

log = logging.getLogger("anon")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
REQUEST_ID = ContextVar("request_id", default=None)
UPDATE_ID = ContextVar("update_id", default=None)

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


class IntegrationError(RuntimeError):
    def __init__(self, provider, operation, **details):
        self.provider = provider
        self.operation = operation
        self.details = details
        super().__init__(f"{provider} {operation} failed")

    def log_fields(self):
        return {"provider": self.provider, "operation": self.operation, **self.details}


def safe_error_message(value):
    message = str(value or "")
    for secret in (BOT_TOKEN, SUPABASE_KEY):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    if SUPABASE_URL:
        message = message.replace(SUPABASE_URL, "[SUPABASE_URL]")
    return message[:300]


def exception_fields(exc):
    if isinstance(exc, IntegrationError):
        return {"exception_type": type(exc).__name__, **exc.log_fields()}
    fields = {"exception_type": type(exc).__name__}
    if isinstance(exc, httpx.HTTPStatusError):
        fields["http_status"] = exc.response.status_code
    elif isinstance(exc, httpx.HTTPError):
        fields["transport_error"] = type(exc).__name__
    else:
        message = safe_error_message(exc)
        if message:
            fields["message"] = message
    return fields


def render_metadata():
    keys = {
        "service_id": "RENDER_SERVICE_ID",
        "service_name": "RENDER_SERVICE_NAME",
        "instance_id": "RENDER_INSTANCE_ID",
        "service_type": "RENDER_SERVICE_TYPE",
        "git_commit": "RENDER_GIT_COMMIT",
        "git_branch": "RENDER_GIT_BRANCH",
        "external_url": "RENDER_EXTERNAL_URL",
        "cpu_count": "RENDER_CPU_COUNT",
    }
    return {name: os.getenv(env) for name, env in keys.items() if os.getenv(env)}


def make_log_record(event, level="INFO", **details):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "service": "anon",
        "environment": "render" if os.getenv("RENDER") == "true" else "local",
        "event": event,
    }
    request_id = REQUEST_ID.get()
    if request_id:
        record["request_id"] = request_id
    update_id = UPDATE_ID.get()
    if update_id is not None:
        record["telegram_update_id"] = update_id
    metadata = render_metadata()
    if metadata:
        record["render"] = metadata
    if details:
        record["details"] = details
    return record


def write_log_record(record):
    level = getattr(logging, str(record.get("level", "INFO")).upper(), logging.INFO)
    log.log(level, record)


async def db_get(table, params):
    try:
        r = await client.get(f"{REST}/{table}", params=params, headers=db_headers())
    except httpx.HTTPError as exc:
        raise IntegrationError("supabase", "GET", resource=table, transport_error=type(exc).__name__) from None
    if r.status_code >= 400:
        try:
            error = r.json()
        except ValueError:
            error = {}
        raise IntegrationError("supabase", "GET", resource=table, http_status=r.status_code,
                               error_code=error.get("code"), error=safe_error_message(error.get("message")))
    try:
        return r.json()
    except ValueError:
        raise IntegrationError("supabase", "GET", resource=table, http_status=r.status_code,
                               error="invalid_json_response") from None


async def db_post(table, payload, prefer="return=representation", params=None):
    try:
        r = await client.post(f"{REST}/{table}", params=params, json=payload, headers=db_headers(prefer))
    except httpx.HTTPError as exc:
        raise IntegrationError("supabase", "POST", resource=table, transport_error=type(exc).__name__) from None
    if r.status_code >= 400:
        try:
            error = r.json()
        except ValueError:
            error = {}
        raise IntegrationError("supabase", "POST", resource=table, http_status=r.status_code,
                               error_code=error.get("code"), error=safe_error_message(error.get("message")))
    try:
        return r.json() if r.content else []
    except ValueError:
        raise IntegrationError("supabase", "POST", resource=table, http_status=r.status_code,
                               error="invalid_json_response") from None


async def db_patch(table, params, payload):
    try:
        r = await client.patch(f"{REST}/{table}", params=params, json=payload, headers=db_headers())
    except httpx.HTTPError as exc:
        raise IntegrationError("supabase", "PATCH", resource=table, transport_error=type(exc).__name__) from None
    if r.status_code >= 400:
        try:
            error = r.json()
        except ValueError:
            error = {}
        raise IntegrationError("supabase", "PATCH", resource=table, http_status=r.status_code,
                               error_code=error.get("code"), error=safe_error_message(error.get("message")))
    try:
        return r.json() if r.content else []
    except ValueError:
        raise IntegrationError("supabase", "PATCH", resource=table, http_status=r.status_code,
                               error="invalid_json_response") from None


async def tg(method, payload):
    try:
        r = await client.post(f"{API}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise IntegrationError("telegram", method, transport_error=type(exc).__name__) from None
    try:
        data = r.json()
    except ValueError:
        raise IntegrationError("telegram", method, http_status=r.status_code,
                               error="invalid_json_response") from None
    if r.status_code >= 400 or not data.get("ok"):
        raise IntegrationError("telegram", method, http_status=r.status_code,
                               error_code=data.get("error_code"),
                               error=safe_error_message(data.get("description")))
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
            except IntegrationError as exc:
                if exc.details.get("http_status") != 409:
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
        except Exception as exc:
            await technical("telegram.keyboard_notice_cleanup_failed", "WARNING",
                            notify_group=False, **exception_fields(exc))
    except Exception as exc:
        await technical("telegram.reply_keyboard_removal_failed", "WARNING", **exception_fields(exc))

async def react(chat_id, message_id, emoji):
    try:
        await tg("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]})
        return True
    except Exception as exc:
        await technical("telegram.reaction_failed", "WARNING", notify_group=False,
                        reaction=emoji, **exception_fields(exc))
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

def utf16_length(value):
    return len(value.encode("utf-16-le")) // 2


async def technical(event, level="INFO", notify_group=True, **details):
    record = make_log_record(event, level, **details)
    write_log_record(record)
    if not notify_group:
        return record
    try:
        chat_id = await log_chat_id()
        if not chat_id:
            return record
        text = json.dumps(record, ensure_ascii=False, indent=2)
        if utf16_length(text) > 3900:
            record["details"] = {"truncated": True, "available_fields": list(details.keys())}
            text = json.dumps(record, ensure_ascii=False, indent=2)
        await tg("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "entities": [{"type": "pre", "offset": 0, "length": utf16_length(text), "language": "json"}],
            "disable_web_page_preview": True,
        })
    except Exception as exc:
        failure = make_log_record(
            "logging.telegram_delivery_failed",
            "ERROR",
            source_event=event,
            **exception_fields(exc),
        )
        write_log_record(failure)
    return record

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
        await technical("system.log_group_registered", group_type=chat.get("type"))
    except Exception as exc:
        await technical("system.log_group_registration_failed", "ERROR",
                        group_type=chat.get("type"), **exception_fields(exc))
        try:
            await tg("sendMessage", {"chat_id": chat["id"], "text": "Registration failed. Check permissions and database connection."})
        except Exception as exc:
            await technical("telegram.log_group_registration_notice_failed", "WARNING",
                            notify_group=False, **exception_fields(exc))
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
        await technical("message.incoming_edit_relay_succeeded", direction="user_to_admin",
                        admin_message_id=admin_mid, user_message_id=m["message_id"])
    except Exception as exc:
        await react(ADMIN_ID,admin_mid,"👎")
        user = await get_user(uid)
        await technical("message.edit_relay_failed", "ERROR", direction="user_to_admin",
                        alias=user.get("alias") if user else None, **exception_fields(exc))

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
    await technical("message.forwarded_to_admin", alias=user["alias"], message_type=kind_of(m),
                    source_message_id=m["message_id"], admin_message_id=cp["message_id"])

async def handle_user(m,user):
    uid=m["from"]["id"]
    if user.get("blocked_by_admin"):
        await technical("message.rejected_blocked_user", "WARNING", alias=user["alias"],
                        message_type=kind_of(m))
        await react_status(uid,m["message_id"],False)
        return
    if not await rate_ok(uid):
        await technical("message.rate_limited", "WARNING", alias=user["alias"])
        return
    kind=kind_of(m); await store_inbox(uid,m,kind); await activity(uid)
    await technical("message.received", alias=user["alias"], message_type=kind,
                    message_id=m["message_id"])
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
        except Exception as exc:
            await react_status(uid,m["message_id"],False)
            await technical("message.live_relay_failed", "ERROR", alias=user["alias"],
                            message_type=kind, **exception_fields(exc))
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
        await technical("message.admin_edit_relay_succeeded", recipient_message_id=user_mid,
                        admin_message_id=m["message_id"])
        return True
    except Exception as exc:
        await react(ADMIN_ID,m["message_id"],"👎")
        await technical("message.admin_edit_relay_failed", "ERROR",
                        admin_message_id=m["message_id"], **exception_fields(exc))
        return False

async def send_to_user(m,uid,reply_to=None):
    p={"chat_id":uid,"from_chat_id":ADMIN_ID,"message_id":m["message_id"]}
    if reply_to: p["reply_parameters"]={"message_id":reply_to}
    cp=await tg("copyMessage",p)
    await map_outbound(cp["message_id"],uid,m["message_id"])
    await activity(uid)
    user = await get_user(uid)
    await technical("message.admin_to_user_sent", alias=user.get("alias") if user else None,
                    message_type=kind_of(m), admin_message_id=m["message_id"],
                    user_message_id=cp["message_id"])

async def open_alias(alias,cmdid):
    u=await get_alias(alias)
    if not u or u.get("blocked_by_admin"):
        await react(ADMIN_ID,cmdid,"👎"); return
    uid=u["telegram_user_id"]; await set_active(uid); await activity(uid)
    await remove_admin_keyboard()
    await technical("admin.chat_opened", alias=u["alias"])
    pending=await db_get("anon_inbox_items",{"user_id":f"eq.{uid}","unread":"eq.true","select":"*","order":"created_at.asc","limit":"1000"})
    ok=fail=0
    for item in pending:
        try:
            cp=await tg("copyMessage",{"chat_id":ADMIN_ID,"from_chat_id":uid,"message_id":item["user_message_id"]})
            await map_admin_message(cp["message_id"],uid,item["user_message_id"])
            await db_patch("anon_inbox_items",{"id":f"eq.{item['id']}"},{"unread":False}); ok+=1
        except Exception:
            fail+=1; await react(uid,item["user_message_id"],"👎")
    if ok or fail:
        await technical("admin.inbox_retrieval_completed", alias=u["alias"],
                        retrieved=ok, unavailable=fail)

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
    await technical("admin.inbox_overview", unread_total=sum(counts.values()),
                    chats_total=len(ids), page=page, total_pages=pages)

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
                try:
                    await send_to_user(m,mapping["sender_user_id"],mapping.get("user_message_id"))
                except Exception as exc:
                    await technical("message.admin_reply_delivery_failed", "ERROR",
                                    admin_message_id=m["message_id"], **exception_fields(exc))
                    await react(ADMIN_ID,m["message_id"],"👎")
            else: await react(ADMIN_ID,m["message_id"],"👎")
            return
        rows=await db_get("anon_outbound_map",{"admin_message_id":f"eq.{reply['message_id']}","select":"*"})
        if rows:
            mapping=rows[0]
            if active != mapping["sender_user_id"]:
                await react(ADMIN_ID,m["message_id"],"👎"); return
            try:
                await send_to_user(m,mapping["sender_user_id"],mapping["user_message_id"])
            except Exception as exc:
                await technical("message.admin_reply_delivery_failed", "ERROR",
                                admin_message_id=m["message_id"], **exception_fields(exc))
                await react(ADMIN_ID,m["message_id"],"👎")
            return
        await react(ADMIN_ID,m["message_id"],"👎"); return
    state=await admin_state(); uid=state.get("active_user_id")
    if uid:
        u=await get_user(uid)
        if u and not u.get("blocked_by_admin"):
            try:
                await send_to_user(m,uid)
            except Exception as exc:
                await technical("message.admin_delivery_failed", "ERROR",
                                admin_message_id=m["message_id"], **exception_fields(exc))
                await react(ADMIN_ID,m["message_id"],"👎")
            return
    await technical("admin.message_ignored_no_active_chat")

async def admin_command(message):
    text = message.get("text", "")
    match=re.fullmatch(r"/i(?:@\w+)?(?:\s+(.+))?\s*",text,re.I)
    if match:
        arg=(match.group(1) or "").strip()
        if not arg: await technical("admin.inbox_list_disabled", hint="Use /i Alias to open a conversation")
        elif re.fullmatch(r"[A-Za-z]+-\d{2}",arg): await open_alias(arg,message["message_id"])
        else: await react(ADMIN_ID,message["message_id"],"👎")
        return True
    if re.fullmatch(r"/c(?:@\w+)?\s*",text,re.I):
        await set_active(None); await technical("admin.chat_closed")
        return True
    match = re.fullmatch(r"/block(?:@\w+)?\s+([A-Za-z]+-\d{2})\s*", text, re.I)
    if match:
        alias = match.group(1).lower()
        rows = await db_get("anon_users", {"alias": f"ilike.{alias}", "select": "*"})
        if not rows:
            await technical("admin.alias_not_found", command="block")
            return True
        target = rows[0]
        await set_block(target["telegram_user_id"], "blocked_by_admin", True)
        try:
            await send_user(target["telegram_user_id"], "You blocked by Fwlai")
        except Exception as exc:
            await technical("telegram.block_notice_failed", "WARNING",
                            alias=target["alias"], notify_group=False, **exception_fields(exc))
        await technical("admin.user_blocked", alias=target["alias"])
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
            except Exception as exc:
                await technical("telegram.unblock_notice_failed", "WARNING",
                                alias=target["alias"], notify_group=False, **exception_fields(exc))
            await technical("admin.user_unblocked", alias=target["alias"])
        else:
            await technical("admin.alias_not_found", command="unblock")
        return True
    return False

async def mark_update(update_id):
    rows = await db_post("anon_processed_updates", {"update_id": update_id}, "resolution=ignore-duplicates,return=representation")
    return bool(rows)

async def unmark_update(update_id):
    try:
        r = await client.delete(f"{REST}/anon_processed_updates", params={"update_id": f"eq.{update_id}"}, headers=db_headers("return=minimal"))
    except httpx.HTTPError as exc:
        raise IntegrationError("supabase", "DELETE", resource="anon_processed_updates",
                               transport_error=type(exc).__name__) from None
    if r.status_code >= 400:
        raise IntegrationError("supabase", "DELETE", resource="anon_processed_updates",
                               http_status=r.status_code)

async def rate_ok(uid):
    try:
        r = await client.post(f"{REST}/rpc/anon_rate_check",
                              json={"p_user_id": uid, "p_limit": 50, "p_window_seconds": 10},
                              headers=db_headers("return=representation"))
    except httpx.HTTPError as exc:
        raise IntegrationError("supabase", "RPC anon_rate_check",
                               transport_error=type(exc).__name__) from None
    if r.status_code >= 400:
        try:
            error = r.json()
        except ValueError:
            error = {}
        raise IntegrationError("supabase", "RPC anon_rate_check", http_status=r.status_code,
                               error_code=error.get("code"),
                               error=safe_error_message(error.get("message")))
    try:
        return r.json() is True
    except ValueError:
        raise IntegrationError("supabase", "RPC anon_rate_check",
                               http_status=r.status_code, error="invalid_json_response") from None

async def map_admin_message(msg_id, sender_id, user_message_id=None):
    payload = {"admin_message_id": msg_id, "sender_user_id": sender_id}
    if user_message_id is not None:
        payload["user_message_id"] = user_message_id
    await db_post("anon_message_map", payload, "resolution=ignore-duplicates,return=minimal")

app = FastAPI()


@app.middleware("http")
async def request_observability(request: Request, call_next):
    incoming_id = request.headers.get("Rndr-Id")
    request_id = incoming_id or uuid.uuid4().hex
    request_token = REQUEST_ID.set(request_id)
    update_token = UPDATE_ID.set(None)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        status = response.status_code
        level = "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO"
        await technical(
            "http.request_completed",
            level,
            notify_group=(status >= 500 or duration_ms >= 1500),
            method=request.method,
            path=request.url.path,
            status_code=status,
            duration_ms=duration_ms,
            render_request_id=incoming_id,
        )
        response.headers["Rndr-Id"] = request_id
        return response
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        await technical(
            "http.request_exception",
            "ERROR",
            method=request.method,
            path=request.url.path,
            duration_ms=duration_ms,
            **exception_fields(exc),
        )
        raise
    finally:
        UPDATE_ID.reset(update_token)
        REQUEST_ID.reset(request_token)


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
    except Exception as exc:
        await technical("system.health_check_failed", "ERROR", **exception_fields(exc))
        raise HTTPException(status_code=503, detail="database unavailable")

@app.post("/telegram/webhook")
async def webhook(request: Request):
    if not WEBHOOK_SECRET or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    update = await request.json()
    update_id = update.get("update_id")
    UPDATE_ID.set(update_id)
    update_type = next((key for key in ("message", "edited_message", "my_chat_member")
                        if key in update), "unknown")
    await technical("telegram.update_received", notify_group=False,
                    update_type=update_type)
    if update_id is not None:
        try:
            if not await mark_update(update_id):
                return {"ok": True, "duplicate": True}
        except Exception as exc:
            await technical("telegram.update_deduplication_failed", "ERROR",
                            **exception_fields(exc))
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
                    await technical("telegram.unregistered_group_left", notify_group=False,
                                    group_type=group.get("type"), trigger="membership_update")
                except Exception as exc:
                    await technical("telegram.unregistered_group_leave_failed", "ERROR",
                                    notify_group=False, **exception_fields(exc))
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
                await technical("telegram.unregistered_group_left", notify_group=False,
                                group_type=chat.get("type"), trigger="group_message")
            except Exception as exc:
                await technical("telegram.unregistered_group_leave_failed", "ERROR",
                                notify_group=False, **exception_fields(exc))
            return {"ok": True}
        if not registered and isinstance(message.get("text"), str) and re.match(r"^/log(?:@\w+)?\s*$", message["text"], re.I):
            await register_log_group(message)
        return {"ok": True}

    if uid == ADMIN_ID:
        if edited:
            try:
                await edit_admin_outbound_copy(message)
            except Exception as exc:
                await technical("message.admin_edit_relay_failed", "ERROR",
                                admin_message_id=message.get("message_id"),
                                **exception_fields(exc))
            return {"ok": True}
        try:
            await route_admin(message)
        except Exception as exc:
            await technical("admin.routing_failed", "ERROR", **exception_fields(exc))
            if update_id is not None: await unmark_update(update_id)
            raise HTTPException(status_code=503, detail="admin routing failed")
        return {"ok": True}

    if edited:
        try:
            await edit_incoming_copy(message)
        except Exception as exc:
            await technical("message.incoming_edit_handler_failed", "ERROR", **exception_fields(exc))
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
                await technical("user.registered", alias=user["alias"], registration_source="start")
            heart_added = await react(uid, message["message_id"], "❤️")
            if not heart_added:
                await react(uid, message["message_id"], "👎")
            await technical("telegram.start_processed", alias=user["alias"], user_created=created,
                            success=True, heart_reaction=heart_added)
        except Exception as exc:
            await technical("telegram.start_failed", "ERROR", **exception_fields(exc))
            await react(uid, message["message_id"], "👎")
            if update_id is not None:
                await unmark_update(update_id)
            raise HTTPException(status_code=503, detail="start failed")
        return {"ok": True}

    user = await get_user(uid)
    if not user:
        try:
            user, created = await allocate_alias(uid)
        except Exception as exc:
            await technical("user.alias_allocation_failed", "ERROR", **exception_fields(exc))
            if update_id is not None:
                await unmark_update(update_id)
            raise HTTPException(status_code=503, detail="alias allocation failed")
        if created:
            await technical("user.registered", alias=user["alias"], registration_source="first_message")
    if is_command(text):
        return {"ok": True}
    try:
        await handle_user(message, user)
    except Exception as exc:
        await technical("message.webhook_handler_failed", "ERROR", alias=user["alias"],
                        message_type=kind_of(message), **exception_fields(exc))
        if update_id is not None:
            await unmark_update(update_id)
        raise HTTPException(status_code=503, detail="relay failed")
    return {"ok": True}

@app.on_event("startup")
async def startup():
    await technical("system.startup_started", notify_group=False)
    if all([BOT_TOKEN, ADMIN_ID, WEBHOOK_SECRET, SUPABASE_URL, SUPABASE_KEY]):
        try:
            await db_get("anon_users", {"select": "telegram_user_id", "limit": "1"})
            base_url = os.getenv("RENDER_EXTERNAL_URL", "https://anon-mr5s.onrender.com").rstrip("/")
            await tg("setWebhook", {
                "url": f"{base_url}/telegram/webhook",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": ["message", "edited_message", "my_chat_member"]
            })
            await technical("system.startup_completed", database="connected",
                            telegram_webhook="configured",
                            allowed_updates=["message", "edited_message", "my_chat_member"])
        except Exception as exc:
            await technical("system.startup_failed", "ERROR", **exception_fields(exc))
    else:
        missing = [name for name, value in {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ADMIN_ID": ADMIN_ID,
            "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "SUPABASE_URL": SUPABASE_URL,
            "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_KEY,
        }.items() if not value]
        await technical("system.configuration_incomplete", "ERROR", missing_variables=missing)

@app.on_event("shutdown")
async def shutdown():
    if BOT_TOKEN and ADMIN_ID:
        try:
            await technical("system.shutdown_started")
        except Exception as exc:
            log.error(make_log_record("system.shutdown_log_failed", "ERROR",
                                      **exception_fields(exc)))
    await client.aclose()
