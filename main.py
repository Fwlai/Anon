import os
import random
import re
import logging
import html
import json
import time
import uuid
import asyncio
import base64
import binascii
import hashlib
import hmac
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

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

for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    for handler in logging.getLogger(logger_name).handlers:
        handler.setFormatter(JSONLogFormatter())

log = logging.getLogger("anon")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").disabled = True
REQUEST_ID = ContextVar("request_id", default=None)
UPDATE_ID = ContextVar("update_id", default=None)
RETENTION_CLEANUP_INTERVAL = 6 * 60 * 60
_last_retention_cleanup = 0.0
_retention_cleanup_lock = asyncio.Lock()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
RENDER_WEBHOOK_SECRET = os.getenv("RENDER_WEBHOOK_SECRET", "")
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")

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
    for secret in (BOT_TOKEN, SUPABASE_KEY, RENDER_API_KEY, RENDER_WEBHOOK_SECRET):
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


def verify_render_webhook(headers, body):
    secret = RENDER_WEBHOOK_SECRET
    if not secret.startswith("whsec_"):
        return False
    webhook_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signature_header = headers.get("webhook-signature", "")
    if not webhook_id or not re.fullmatch(r"evt-[0-9a-z]{20}", webhook_id):
        return False
    if not timestamp.isdigit() or len(timestamp) > 12:
        return False
    if abs(time.time() - int(timestamp)) > 300:
        return False
    if not signature_header or len(signature_header) > 4096:
        return False
    try:
        encoded_secret = secret[6:]
        encoded_secret += "=" * (-len(encoded_secret) % 4)
        secret_bytes = base64.b64decode(encoded_secret, validate=True)
    except (ValueError, binascii.Error):
        return False
    signed_content = webhook_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
    for versioned_signature in signature_header.split():
        if not versioned_signature.startswith("v1,"):
            continue
        try:
            candidate = base64.b64decode(versioned_signature[3:], validate=True)
        except (ValueError, binascii.Error):
            continue
        if hmac.compare_digest(expected, candidate):
            return True
    return False


async def fetch_render_event_details(event_id):
    if not RENDER_API_KEY:
        return None
    try:
        response = await client.get(
            f"https://api.render.com/v1/events/{event_id}",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            timeout=4.0,
        )
    except httpx.HTTPError as exc:
        raise IntegrationError("render", "GET event", event_id=event_id,
                               transport_error=type(exc).__name__) from None
    if response.status_code >= 400:
        try:
            error = response.json()
        except ValueError:
            error = {}
        raise IntegrationError("render", "GET event", event_id=event_id,
                               http_status=response.status_code,
                               error=safe_error_message(error.get("message")))
    try:
        return response.json()
    except ValueError:
        raise IntegrationError("render", "GET event", event_id=event_id,
                               http_status=response.status_code,
                               error="invalid_json_response") from None


def sanitize_render_details(value, depth=0):
    if depth >= 5:
        return "[nested detail omitted]"
    if isinstance(value, dict):
        hidden = {"email", "user", "token", "authorization", "secret",
                  "api_key", "password", "access_key"}
        cleaned = {}
        for key, item in value.items():
            key_name = str(key)
            if key_name.lower() in hidden:
                continue
            sanitized = sanitize_render_details(item, depth + 1)
            if isinstance(item, (dict, list)) and item and not sanitized:
                continue
            cleaned[key_name] = sanitized
        return cleaned
    if isinstance(value, list):
        return [sanitize_render_details(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return safe_error_message(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe_error_message(value)


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
        raise IntegrationError("supabase", "POST", resource=table, http_status=r.status_code,                               error_code=error.get("code"), error=safe_error_message(error.get("message")))
    try:
        return r.json() if r.content else []
    except ValueError:
        raise IntegrationError("supabase", "POST", resource=table, http_status=r.status_code,
                               error="invalid_json_response") from None


async def db_rpc(function, payload):
    try:
        r = await client.post(
            f"{REST}/rpc/{function}",
            json=payload,
            headers=db_headers("return=representation"),
        )
    except httpx.HTTPError as exc:
        raise IntegrationError(
            "supabase", f"RPC {function}", transport_error=type(exc).__name__
        ) from None
    if r.status_code >= 400:
        try:
            error = r.json()
        except ValueError:
            error = {}
        raise IntegrationError(
            "supabase", f"RPC {function}", http_status=r.status_code,
            error_code=error.get("code"),
            error=safe_error_message(error.get("message")),
        )
    try:
        return r.json() if r.content else None
    except ValueError:
        raise IntegrationError(
            "supabase", f"RPC {function}", http_status=r.status_code,
            error="invalid_json_response",
        ) from None


async def db_delete(table, params):
    try:
        r = await client.delete(
            f"{REST}/{table}",
            params=params,
            headers=db_headers("return=minimal"),
        )
    except httpx.HTTPError as exc:
        raise IntegrationError(
            "supabase", "DELETE", resource=table,
            transport_error=type(exc).__name__,
        ) from None
    if r.status_code >= 400:
        try:
            error = r.json()
        except ValueError:
            error = {}
        raise IntegrationError(
            "supabase", "DELETE", resource=table,
            http_status=r.status_code,
            error_code=error.get("code"),
            error=safe_error_message(error.get("message")),
        )


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

async def get_alias(alias):
    rows=await db_get("anon_users",{"alias":f"ilike.{alias}","select":"*"})
    return rows[0] if rows else None

async def admin_state():
    rows=await db_get("anon_admin_state",{"admin_user_id":f"eq.{ADMIN_ID}","select":"*"})
    return rows[0] if rows else {"active_user_id":None}

async def set_active(uid):
    await db_post("anon_admin_state",{"admin_user_id":ADMIN_ID,"active_user_id":uid},"resolution=merge-duplicates,return=minimal")

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
    if m.get("live_photo"): return "live photo"
    if m.get("paid_media"): return "paid media"
    if m.get("location") or m.get("venue"): return "location"
    if m.get("contact"): return "contact"
    if m.get("poll"): return "poll"
    if m.get("dice"): return "dice"
    if m.get("game"): return "game"
    if m.get("invoice"): return "invoice"
    return "text"

async def map_outbound(user_mid,uid,admin_mid):
    await db_post("anon_outbound_map",{"user_message_id":user_mid,"sender_user_id":uid,"admin_message_id":admin_mid},"resolution=ignore-duplicates,return=minimal")

async def persist_after_delivery(operation, event, **details):
    last_error = None
    for attempt in range(2):
        try:
            await operation()
            return True
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.15)

    await technical(
        event,
        "ERROR",
        notify_group=False,
        delivery_confirmed=True,
        persistence_attempts=2,
        **details,
        **exception_fields(last_error),
    )
    return False

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
        if utf16_length(text) > 3000:
            record["details"] = {"truncated": True, "available_fields": list(details.keys())}
            text = json.dumps(record, ensure_ascii=False, indent=2)
        # Backtick-escape code fence markers inside JSON strings to preserve the Markdown block.
        safe_json = text.replace("```", "\\u0060\\u0060\\u0060")
        event_label = str(event).replace("`", "\\`")
        rich_markdown = (
            f"` {event_label} `\n\n"
            f"```json\n{safe_json}\n```"
        )
        try:
            await tg("sendRichMessage", {
                "chat_id": chat_id,
                "rich_message": {"markdown": rich_markdown},
                "disable_notification": level.upper() in ("INFO",),
            })
        except Exception as rich_exc:
            # Avoid duplicate delivery if Telegram accepted the request but the
            # response was lost or timed out. Rich logs are the only group format.
            write_log_record(make_log_record(
                "logging.rich_message_delivery_failed", "WARNING",
                source_event=event, **exception_fields(rich_exc),
            ))
    except Exception as exc:
        failure = make_log_record(
            "logging.telegram_delivery_failed",
            "ERROR",
            source_event=event,
            **exception_fields(exc),
        )
        write_log_record(failure)
    return record

def edit_request(m, chat_id, message_id):
    if "text" in m:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": m.get("text", ""),
        }
        for field in ("entities", "link_preview_options"):
            if field in m:
                payload[field] = m[field]
        return "editMessageText", payload

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": m.get("caption", ""),
    }
    for field in ("caption_entities", "show_caption_above_media"):
        if field in m:
            payload[field] = m[field]
    return "editMessageCaption", payload


async def edit_incoming_copy(m):
    uid=m["from"]["id"]
    rows=await db_get("anon_message_map",{"sender_user_id":f"eq.{uid}","user_message_id":f"eq.{m['message_id']}","select":"*"})
    if not rows:
        await react(uid,m["message_id"],"👎")
        return
    admin_mid=rows[0]["admin_message_id"]
    try:
        method, payload = edit_request(m, ADMIN_ID, admin_mid)
        await tg(method, payload)
        await technical("message.incoming_edit_relay_succeeded", direction="user_to_admin",
                        admin_message_id=admin_mid, user_message_id=m["message_id"])
    except Exception as exc:
        await react(ADMIN_ID,admin_mid,"👎")
        user = await get_user(uid)
        await technical("message.edit_relay_failed", "ERROR", direction="user_to_admin",
                        alias=user.get("alias") if user else None, **exception_fields(exc))

async def claim_album_messages(m, sender_uid, destination_uid, direction, reply_to=None):
    source_uid = ADMIN_ID if direction == "admin_to_user" else sender_uid
    group_id = str(m["media_group_id"])
    now = datetime.now(timezone.utc).isoformat()
    params = {
        "on_conflict": "source_chat_id,destination_chat_id,media_group_id,message_id"
    }
    await db_post(
        "anon_album_buffer",
        {
            "source_chat_id": source_uid,
            "destination_chat_id": destination_uid,
            "sender_user_id": sender_uid,
            "direction": direction,
            "media_group_id": group_id,
            "message_id": m["message_id"],
            "reply_to_message_id": reply_to,
            "last_seen_at": now,
        },
        "resolution=ignore-duplicates,return=minimal",
        params,
    )
    await db_patch(
        "anon_album_buffer",
        {
            "source_chat_id": f"eq.{source_uid}",
            "destination_chat_id": f"eq.{destination_uid}",
            "media_group_id": f"eq.{group_id}",
            "status": "eq.pending",
        },
        {"last_seen_at": now},
    )

    for delay in (1.2, 0.5, 0.5, 0.5):
        await asyncio.sleep(delay)
        rows = await db_rpc("anon_claim_album", {
            "p_source_chat_id": source_uid,
            "p_destination_chat_id": destination_uid,
            "p_media_group_id": group_id,
            "p_quiet_seconds": 1,
        })
        if rows:
            if any(
                row["sender_user_id"] != sender_uid or row["direction"] != direction
                for row in rows
            ):
                await db_rpc("anon_finish_album", {
                    "p_source_chat_id": source_uid,
                    "p_destination_chat_id": destination_uid,
                    "p_media_group_id": group_id,
                    "p_mappings": None,
                    "p_status": "pending",
                })
                raise RuntimeError("Album buffer contains inconsistent routing data")
            return source_uid, group_id, rows
    return source_uid, group_id, []


async def finish_album(source_uid, destination_uid, group_id, mappings=None, status="delivered"):
    return await db_rpc("anon_finish_album", {
        "p_source_chat_id": source_uid,
        "p_destination_chat_id": destination_uid,
        "p_media_group_id": group_id,
        "p_mappings": mappings,
        "p_status": status,
    })


async def relay_album_to_admin(m, user, reply_to=None):
    uid = m["from"]["id"]
    source_uid, group_id, rows = await claim_album_messages(
        m, uid, ADMIN_ID, "user_to_admin", reply_to
    )
    if not rows:
        return

    message_ids = sorted(row["message_id"] for row in rows)
    sessions = await db_get(
        "anon_admin_sessions",
        {"sender_user_id": f"eq.{uid}", "select": "*"},
    )
    now = datetime.now(timezone.utc)
    header = not sessions
    if sessions:
        last = datetime.fromisoformat(
            sessions[0]["last_message_at"].replace("Z", "+00:00")
        )
        header = (now - last).total_seconds() >= 600

    if header:
        sent_header = await tg("sendMessage", {
            "chat_id": ADMIN_ID,
            "text": f'<blockquote>Chat: <code>{html.escape(user["alias"])}</code></blockquote>',
            "parse_mode": "HTML",
        })
        try:
            await map_admin_message(sent_header["message_id"], uid)
        except Exception as exc:
            await technical(
                "message.album_header_mapping_failed", "ERROR",
                alias=user["alias"], message_delivered=True,
                **exception_fields(exc),
            )

    try:
        copied = await tg("copyMessages", {
            "chat_id": ADMIN_ID,
            "from_chat_id": source_uid,
            "message_ids": message_ids,
        })
    except Exception:
        try:
            await finish_album(source_uid, ADMIN_ID, group_id, status="pending")
        except Exception as persist_exc:
            await technical(
                "message.album_retry_state_failed", "ERROR",
                direction="user_to_admin", alias=user["alias"],
                **exception_fields(persist_exc),
            )
        raise

    if not isinstance(copied, list) or len(copied) != len(message_ids):
        try:
            await finish_album(source_uid, ADMIN_ID, group_id, status="delivered_unmapped")
        except Exception as exc:
            await technical(
                "message.album_unmapped_state_persistence_failed",
                "ERROR",
                direction="user_to_admin",
                alias=user["alias"],
                delivery_result_received=True,
                **exception_fields(exc),
            )
        await technical(
            "message.album_copy_incomplete", "ERROR",
            direction="user_to_admin", alias=user["alias"],
            source_message_count=len(message_ids),
            copied_message_count=len(copied) if isinstance(copied, list) else None,
            delivery_already_attempted=True,
        )
        return

    mappings = [
        {
            "source_message_id": source_id,
            "destination_message_id": copied_item["message_id"],
        }
        for source_id, copied_item in zip(message_ids, copied)
    ]
    try:
        await finish_album(source_uid, ADMIN_ID, group_id, mappings, "delivered")
    except Exception as exc:
        await technical(
            "message.album_mapping_persistence_failed", "ERROR",
            direction="user_to_admin", alias=user["alias"],
            message_count=len(message_ids), delivery_already_completed=True,
            **exception_fields(exc),
        )
        return

    try:
        await db_post(
            "anon_admin_sessions",
            {"sender_user_id": uid, "last_message_at": now.isoformat()},
            "resolution=merge-duplicates,return=minimal",
        )
    except Exception as exc:
        await technical(
            "message.album_session_update_failed", "WARNING",
            direction="user_to_admin", alias=user["alias"],
            message_count=len(message_ids), delivery_already_completed=True,
            **exception_fields(exc),
        )

    await technical(
        "message.album_forwarded_to_admin",
        alias=user["alias"],
        message_count=len(message_ids),
        source_message_ids=message_ids,
        reply_context_omitted=any(row.get("reply_to_message_id") for row in rows),
    )


async def relay_album_to_user(m, uid, reply_to=None):
    source_uid, group_id, rows = await claim_album_messages(
        m, uid, uid, "admin_to_user", reply_to
    )
    if not rows:
        return

    message_ids = sorted(row["message_id"] for row in rows)
    try:
        copied = await tg("copyMessages", {
            "chat_id": uid,
            "from_chat_id": source_uid,
            "message_ids": message_ids,
        })
    except Exception:
        try:
            await finish_album(source_uid, uid, group_id, status="pending")
        except Exception as persist_exc:
            await technical(
                "message.album_retry_state_failed", "ERROR",
                direction="admin_to_user", recipient_user_id=uid,
                **exception_fields(persist_exc),
            )
        raise

    if not isinstance(copied, list) or len(copied) != len(message_ids):
        try:
            await finish_album(source_uid, uid, group_id, status="delivered_unmapped")
        except Exception as exc:
            await technical(
                "message.album_unmapped_state_persistence_failed",
                "ERROR",
                direction="admin_to_user",
                recipient_user_id=uid,
                delivery_result_received=True,
                **exception_fields(exc),
            )
        await technical(
            "message.album_copy_incomplete", "ERROR",
            direction="admin_to_user", recipient_user_id=uid,
            source_message_count=len(message_ids),
            copied_message_count=len(copied) if isinstance(copied, list) else None,
            delivery_already_attempted=True,
        )
        return

    mappings = [
        {
            "source_message_id": source_id,
            "destination_message_id": copied_item["message_id"],
        }
        for source_id, copied_item in zip(message_ids, copied)
    ]
    try:
        await finish_album(source_uid, uid, group_id, mappings, "delivered")
    except Exception as exc:
        await technical(
            "message.album_mapping_persistence_failed", "ERROR",
            direction="admin_to_user", recipient_user_id=uid,
            message_count=len(message_ids), delivery_already_completed=True,
            **exception_fields(exc),
        )
        return

    user = await get_user(uid)
    await technical(
        "message.admin_album_to_user_sent",
        alias=user.get("alias") if user else None,
        message_count=len(message_ids),
        source_message_ids=message_ids,
        reply_context_omitted=any(row.get("reply_to_message_id") for row in rows),
    )


async def relay_live(m,user,reply_to=None):
    if m.get("media_group_id"):
        await relay_album_to_admin(m, user, reply_to)
        return

    uid=m["from"]["id"]; now=datetime.now(timezone.utc)
    sessions=await db_get("anon_admin_sessions",{"sender_user_id":f"eq.{uid}","select":"*"})
    header=not sessions
    if sessions:
        last=datetime.fromisoformat(sessions[0]["last_message_at"].replace("Z","+00:00"))
        header=(now-last).total_seconds()>=600
    if header:
        h=await tg("sendMessage",{"chat_id":ADMIN_ID,"text":f'<blockquote>Chat: <code>{html.escape(user["alias"])}</code></blockquote>',"parse_mode":"HTML"})
        await persist_after_delivery(
            lambda: map_admin_message(h["message_id"], uid),
            "message.header_mapping_persistence_failed",
            direction="user_to_admin",
            admin_message_id=h["message_id"],
            sender_user_id=uid,
        )
        await persist_after_delivery(
            lambda: db_post(
                "anon_admin_sessions",
                {"sender_user_id": uid, "last_message_at": now.isoformat()},
                "resolution=merge-duplicates,return=minimal",
            ),
            "message.session_persistence_failed",
            direction="user_to_admin",
            sender_user_id=uid,
        )
    p={"chat_id":ADMIN_ID,"from_chat_id":uid,"message_id":m["message_id"]}
    if reply_to: p["reply_parameters"]={"message_id":reply_to}
    cp=await tg("copyMessage",p)
    await persist_after_delivery(
        lambda: map_admin_message(cp["message_id"], uid, m["message_id"]),
        "message.delivery_mapping_persistence_failed",
        direction="user_to_admin",
        source_message_id=m["message_id"],
        destination_message_id=cp["message_id"],
        sender_user_id=uid,
    )
    if not header:
        await persist_after_delivery(
            lambda: db_post(
                "anon_admin_sessions",
                {"sender_user_id": uid, "last_message_at": now.isoformat()},
                "resolution=merge-duplicates,return=minimal",
            ),
            "message.session_persistence_failed",
            direction="user_to_admin",
            sender_user_id=uid,
        )
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

    kind=kind_of(m)
    await technical("message.received", alias=user["alias"], message_type=kind,
                    message_id=m["message_id"])

    mapped=None
    reply=m.get("reply_to_message")
    if reply:
        rows=await db_get("anon_outbound_map",{
            "user_message_id":f"eq.{reply['message_id']}",
            "sender_user_id":f"eq.{uid}",
            "select":"*"
        })
        if rows:
            mapped=rows[0]["admin_message_id"]
        else:
            rows=await db_get("anon_message_map",{
                "user_message_id":f"eq.{reply['message_id']}",
                "sender_user_id":f"eq.{uid}",
                "select":"*"
            })
            if rows:
                mapped=rows[0]["admin_message_id"]

    try:
        await relay_live(m,user,mapped)
    except Exception as exc:
        await react_status(uid,m["message_id"],False)
        await technical("message.live_relay_failed", "ERROR", alias=user["alias"],
                        message_type=kind, **exception_fields(exc))
        raise

async def edit_admin_outbound_copy(m):
    rows=await db_get("anon_outbound_map",{"admin_message_id":f"eq.{m['message_id']}","select":"*"})
    if not rows:
        return False
    mapping=rows[0]
    uid=mapping["sender_user_id"]
    user_mid=mapping["user_message_id"]
    try:
        method, payload = edit_request(m, uid, user_mid)
        await tg(method, payload)
        await technical("message.admin_edit_relay_succeeded", recipient_message_id=user_mid,
                        admin_message_id=m["message_id"])
        return True
    except Exception as exc:
        await react(ADMIN_ID,m["message_id"],"👎")
        await technical("message.admin_edit_relay_failed", "ERROR",
                        admin_message_id=m["message_id"], **exception_fields(exc))
        return False

async def send_to_user(m,uid,reply_to=None):
    if m.get("media_group_id"):
        await relay_album_to_user(m, uid, reply_to)
        return

    p={"chat_id":uid,"from_chat_id":ADMIN_ID,"message_id":m["message_id"]}
    if reply_to: p["reply_parameters"]={"message_id":reply_to}
    cp=await tg("copyMessage",p)
    await persist_after_delivery(
        lambda: map_outbound(cp["message_id"], uid, m["message_id"]),
        "message.outbound_mapping_persistence_failed",
        direction="admin_to_user",
        source_message_id=m["message_id"],
        destination_message_id=cp["message_id"],
        recipient_user_id=uid,
    )
    try:
        user = await get_user(uid)
    except Exception:
        user = None
    await technical("message.admin_to_user_sent", alias=user.get("alias") if user else None,
                    message_type=kind_of(m), admin_message_id=m["message_id"],
                    user_message_id=cp["message_id"])

async def open_alias(alias,cmdid):
    u=await get_alias(alias)
    if not u or u.get("blocked_by_admin"):
        await react(ADMIN_ID,cmdid,"👎")
        return
    uid=u["telegram_user_id"]
    await set_active(uid)
    await technical("admin.chat_selected", alias=u["alias"])

async def route_admin(m):
    text=m.get("text","")
    if text.startswith("/") and await admin_command(m):
        return

    reply=m.get("reply_to_message")
    if reply:
        rows=await db_get("anon_message_map",{
            "admin_message_id":f"eq.{reply['message_id']}",
            "select":"*"
        })
        if rows:
            mapping=rows[0]
            uid=mapping["sender_user_id"]
            user=await get_user(uid)
            if user and not user.get("blocked_by_admin"):
                try:
                    await send_to_user(m,uid,mapping.get("user_message_id"))
                except Exception as exc:
                    await technical("message.admin_reply_delivery_failed", "ERROR",
                                    admin_message_id=m["message_id"], **exception_fields(exc))
                    await react(ADMIN_ID,m["message_id"],"👎")
                    if m.get("media_group_id"):
                        raise
            else:
                await react(ADMIN_ID,m["message_id"],"👎")
            return

        rows=await db_get("anon_outbound_map",{
            "admin_message_id":f"eq.{reply['message_id']}",
            "select":"*"
        })
        if rows:
            mapping=rows[0]
            uid=mapping["sender_user_id"]
            user=await get_user(uid)
            if user and not user.get("blocked_by_admin"):
                try:
                    await send_to_user(m,uid,mapping["user_message_id"])
                except Exception as exc:
                    await technical("message.admin_reply_delivery_failed", "ERROR",
                                    admin_message_id=m["message_id"], **exception_fields(exc))
                    await react(ADMIN_ID,m["message_id"],"👎")
                    if m.get("media_group_id"):
                        raise
            else:
                await react(ADMIN_ID,m["message_id"],"👎")
            return

        await react(ADMIN_ID,m["message_id"],"👎")
        return

    state=await admin_state()
    uid=state.get("active_user_id")
    if uid:
        user=await get_user(uid)
        if user and not user.get("blocked_by_admin"):
            try:
                await send_to_user(m,uid)
            except Exception as exc:
                await technical("message.admin_delivery_failed", "ERROR",
                                admin_message_id=m["message_id"], **exception_fields(exc))
                await react(ADMIN_ID,m["message_id"],"👎")
                if m.get("media_group_id"):
                    raise
        else:
            await react(ADMIN_ID,m["message_id"],"👎")
        return

    await react(ADMIN_ID,m["message_id"],"👎")
    await technical("admin.message_rejected_no_target")

async def admin_command(message):
    text = message.get("text", "")
    match=re.fullmatch(r"/i(?:@\w+)?(?:\s+(.+))?\s*",text,re.I)
    if match:
        arg=(match.group(1) or "").strip()
        if re.fullmatch(r"[A-Za-z]+-\d{2}",arg):
            await open_alias(arg,message["message_id"])
        else:
            await react(ADMIN_ID,message["message_id"],"👎")
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

async def cleanup_old_records_if_due():
    global _last_retention_cleanup

    monotonic_now = time.monotonic()
    if monotonic_now - _last_retention_cleanup < RETENTION_CLEANUP_INTERVAL:
        return

    async with _retention_cleanup_lock:
        monotonic_now = time.monotonic()
        if monotonic_now - _last_retention_cleanup < RETENTION_CLEANUP_INTERVAL:
            return

        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        try:
            await db_delete(
                "anon_processed_updates",
                {"created_at": f"lt.{cutoff}"},
            )
            await db_delete(
                "anon_album_buffer",
                {"created_at": f"lt.{cutoff}"},
            )
            await technical(
                "maintenance.retention_cleanup_completed",
                notify_group=False,
                retention_days=30,
            )
        except Exception as exc:
            await technical(
                "maintenance.retention_cleanup_failed",
                "WARNING",
                notify_group=False,
                retention_days=30,
                **exception_fields(exc),
            )
        finally:
            _last_retention_cleanup = time.monotonic()


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
    if incoming_id and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", incoming_id):
        incoming_id = None
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
            "http.request_exception",            "ERROR",
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

@app.post("/render/webhook")
async def render_webhook(request: Request):
    if not RENDER_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Render webhook is not configured")
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) > 65536):
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(status_code=413, detail="payload too large")
    if not verify_render_webhook(request.headers, body):
        await technical("render.webhook_rejected", "WARNING", notify_group=False,
                        reason="invalid_signature_or_timestamp")
        raise HTTPException(status_code=401, detail="invalid Render webhook signature")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise HTTPException(status_code=400, detail="invalid event payload")

    event_data = payload["data"]
    event_id = event_data.get("id")
    event_type = payload.get("type")
    if not isinstance(event_id, str) or event_id != request.headers.get("webhook-id"):
        raise HTTPException(status_code=400, detail="event ID mismatch")
    if not isinstance(event_type, str):
        raise HTTPException(status_code=400, detail="missing event type")

    service_id = event_data.get("serviceId")
    if RENDER_SERVICE_ID and service_id != RENDER_SERVICE_ID:
        await technical("render.unrelated_service_event_ignored", notify_group=False,
                        event_id=event_id, event_type=event_type, service_id=service_id)
        return {"ok": True, "ignored": True}

    event_status = event_data.get("status")
    level = "ERROR" if event_status == "failed" or event_type in {
        "server_failed", "server_hardware_failure", "image_pull_failed",
        "pipeline_minutes_exhausted"
    } else "WARNING" if event_type in {"service_suspended", "maintenance_started"} else "INFO"

    platform_details = None
    details_error = None
    if RENDER_API_KEY:
        try:
            full_event = await asyncio.wait_for(fetch_render_event_details(event_id), timeout=4.5)
            if isinstance(full_event, dict):
                platform_details = sanitize_render_details(full_event.get("details", full_event))
        except Exception as exc:
            details_error = exception_fields(exc)

    log_kwargs = {
        "platform_event": event_type,
        "event_id": event_id,
        "occurred_at": payload.get("timestamp"),
        "service_id": service_id,
        "service_name": event_data.get("serviceName"),
        "status": event_status,
    }
    if platform_details is not None:
        log_kwargs["platform_details"] = platform_details
    if details_error is not None:
        log_kwargs["details_fetch_error"] = details_error

    try:
        await asyncio.wait_for(
            technical("render.platform_event", level, **log_kwargs),
            timeout=7.0,
        )
    except asyncio.TimeoutError:
        write_log_record(make_log_record(
            "render.telegram_forward_timeout", "WARNING",
            event_id=event_id, platform_event=event_type,
        ))
    return {"ok": True}


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
            await cleanup_old_records_if_due()
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
            if not registered or int(group.get("id", 0)) != registered:
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
        if not registered or int(chat.get("id", 0)) != registered:
            try:
                await tg("leaveChat", {"chat_id": chat["id"]})
                await technical("telegram.unregistered_group_left", notify_group=False,
                                group_type=chat.get("type"), trigger="group_message")
            except Exception as exc:
                await technical("telegram.unregistered_group_leave_failed", "ERROR",
                                notify_group=False, **exception_fields(exc))
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