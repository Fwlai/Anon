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
_STARTED_AT = time.time()

ALBUM_QUIET_SECONDS = 3
ALBUM_MAX_WAIT_SECONDS = 45
ALBUM_PROCESSING_LEASE_SECONDS = 300
ALBUM_RECOVERY_INTERVAL_SECONDS = 5
_album_tasks = {}
_album_recovery_task = None

NOTIFY_COOLDOWN_SECONDS = 300
IMPORTANT_EVENTS = {
    "system.startup_completed",
    "system.startup_failed",
    "system.shutdown_started",
    "user.registered",
    "admin.user_blocked",
    "admin.user_unblocked",
}
NOTIFY_WARNING_EVENTS = {
    "message.album_late_part_rejected",
    "message.album_stale_processing_marked_unknown",
    "maintenance.retention_cleanup_failed",
    "telegram.unregistered_group_leave_failed",
}
GROUP_LOG_FIELDS = {
    "provider", "operation", "http_status", "error_code", "exception_type",
    "transport_error", "error", "stage", "direction", "message_count",
    "item_count", "retry_after_seconds",
}
_notify_state = {}
_stats = {"errors": 0, "warnings": 0, "last_error": None}
SENSITIVE_KEYS = {
    "alias", "sender_user_id", "recipient_user_id", "user_id", "telegram_user_id",
    "chat_id", "source_chat_id", "destination_chat_id", "admin_user_id",
    "active_user_id", "update_id", "telegram_update_id", "from_id", "uid",
    "message_id", "admin_message_id", "user_message_id", "source_message_id",
    "destination_message_id", "recipient_message_id", "source_message_ids",
    "destination_message_ids", "service", "repo", "branch", "commit", "url",
    "environment", "render", "request_id", "project", "project_name",
    "project_id", "projectname", "projectid", "service_name", "service_id",
    "servicename", "serviceid", "deployment_id", "deploymentid", "deploy_id",
    "deployid", "instance_id", "instanceid", "region", "platform", "hostname",
    "host", "render_service_id", "render_instance_id", "render_deploy_id",
    "event_id", "eventid",
}

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


def telegram_error_code(exc):
    if not isinstance(exc, IntegrationError) or exc.provider != "telegram":
        return None
    http_status = exc.details.get("http_status")
    code = exc.details.get("error_code")
    if isinstance(http_status, int) and http_status >= 500:
        return http_status
    if http_status == 429 or code == 429:
        return 429
    if isinstance(code, int):
        return code
    return http_status if isinstance(http_status, int) else None


def telegram_rate_limited(exc):
    return telegram_error_code(exc) == 429


def telegram_retry_delay(exc, attempt):
    suggested = (
        exc.details.get("retry_after_seconds")
        if isinstance(exc, IntegrationError) and exc.provider == "telegram"
        else None
    )
    if isinstance(suggested, int) and not isinstance(suggested, bool) and suggested > 0:
        return suggested
    return 2 + attempt * 2


def telegram_permanent_rejection(exc):
    code = telegram_error_code(exc)
    return code is not None and 400 <= code < 500 and code != 429


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


def scrub(value):
    """Remove identifiers, aliases, and platform metadata from application logs."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if str(k).lower() not in SENSITIVE_KEYS}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"-?\b\d{7,}\b", "[id]", value)
    return value


def make_log_record(event, level="INFO", **details):
    record = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "level": level.upper(),
        "event": event,
    }
    if details:
        record["details"] = scrub(details)
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
        details = {
            "http_status": r.status_code,
            "error_code": data.get("error_code"),
            "error": safe_error_message(data.get("description")),
        }
        parameters = data.get("parameters")
        retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after > 0:
            details["retry_after_seconds"] = retry_after
        raise IntegrationError("telegram", method, **details)
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


async def set_open(uid, value):
    await db_patch(
        "anon_users",
        {"telegram_user_id": f"eq.{uid}"},
        {"is_open": bool(value)},
    )


async def refresh_header_button(uid, is_open, exclude_message_id=None):
    rows = await db_get("anon_admin_sessions", {
        "sender_user_id": f"eq.{uid}",
        "select": "last_header_message_id",
        "limit": "1",
    })
    if not rows:
        return
    message_id = rows[0].get("last_header_message_id")
    if not message_id or message_id == exclude_message_id:
        return
    try:
        await tg("editMessageReplyMarkup", {
            "chat_id": ADMIN_ID,
            "message_id": message_id,
            "reply_markup": header_reply_markup(
                {"telegram_user_id": uid}, is_open=is_open
            ),
        })
    except Exception as exc:
        await technical(
            "message.header_button_refresh_failed",
            "WARNING",
            notify_group=False,
            **exception_fields(exc),
        )


async def activate_alias(uid, exclude_header_message_id=None):
    state = await admin_state()
    previous_uid = state.get("active_user_id")
    await set_active(uid)

    if previous_uid is not None and previous_uid != uid:
        await set_open(previous_uid, False)
        await refresh_header_button(
            previous_uid, False, exclude_header_message_id
        )

    await set_open(uid, True)
    await refresh_header_button(uid, True, exclude_header_message_id)


async def close_active_alias(exclude_header_message_id=None):
    state = await admin_state()
    active_uid = state.get("active_user_id")
    await set_active(None)
    if active_uid is not None:
        await set_open(active_uid, False)
        await refresh_header_button(
            active_uid, False, exclude_header_message_id
        )
    return active_uid

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

def header_text(alias):
    return f'<blockquote>Alias: <code>{html.escape(alias)}</code></blockquote>'


def header_reply_markup(user, uid=None, is_open=None):
    uid = int(uid if uid is not None else user["telegram_user_id"])
    opened = user.get("is_open", False) if is_open is None else bool(is_open)
    return {
        "inline_keyboard": [[{
            "text": "open",
            "style": "success" if opened else "danger",
            "callback_data": f"anon:open:{uid}",
        }]]
    }


def reply_params(message_id):
    return {"message_id": message_id, "allow_sending_without_reply": True}


def header_text(alias):
    return f'<blockquote>Alias: <code>{html.escape(alias)}</code></blockquote>'


def reply_params(message_id):
    return {"message_id": message_id, "allow_sending_without_reply": True}


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


def should_notify_group(event, level):
    """Only errors, selected warnings, and notable events reach the log group."""
    level = level.upper()
    if level in ("ERROR", "CRITICAL"):
        return True
    if level == "WARNING":
        return event in NOTIFY_WARNING_EVENTS
    return event in IMPORTANT_EVENTS


def group_log_message(record, suppressed=0):
    event = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(record.get("event", "unknown")))[:100]
    level = str(record.get("level", "INFO")).upper()
    lines = [f"{level} · {event}", str(record.get("timestamp", ""))]
    details = scrub(record.get("details") or {})
    if isinstance(details, dict):
        selected = [f"{key}: {details[key]}" for key in GROUP_LOG_FIELDS
                    if key in details and details[key] not in (None, "", [], {})]
        if selected:
            lines.extend(selected[:6])
    if suppressed:
        lines.append(f"Repeated events suppressed: {suppressed}")
    return "\n".join(lines)[:900]


def notify_gate(event, level):
    """Suppress repeats of the same non-critical event for five minutes."""
    if event in IMPORTANT_EVENTS:
        return True, 0
    now = time.monotonic()
    state = _notify_state.get(event)
    if state and now - state["at"] < NOTIFY_COOLDOWN_SECONDS:
        state["suppressed"] += 1
        return False, 0
    suppressed = state["suppressed"] if state else 0
    _notify_state[event] = {"at": now, "suppressed": 0}
    return True, suppressed


async def technical(event, level="INFO", notify_group=True, **details):
    level = str(level).upper()
    if level == "ERROR":
        _stats["errors"] += 1
        _stats["last_error"] = (event, datetime.now(timezone.utc))
    elif level == "WARNING":
        _stats["warnings"] += 1
    notify = notify_group and should_notify_group(event, level)
    suppressed = 0
    if notify:
        notify, suppressed = notify_gate(event, level)
    record = make_log_record(event, level, **details)
    write_log_record(record)
    if not notify:
        return record
    try:
        chat_id = await log_chat_id()
        if not chat_id:
            return record
        try:
            await tg("sendMessage", {
                "chat_id": chat_id,
                "text": group_log_message(record, suppressed),
                "disable_notification": level not in ("ERROR", "CRITICAL"),
            })
        except Exception as exc:
            write_log_record(make_log_record(
                "logging.telegram_delivery_failed", "ERROR",
                source_event=event, **exception_fields(exc),
            ))
    except Exception as exc:
        write_log_record(make_log_record(
            "logging.telegram_delivery_failed", "ERROR",
            source_event=event, **exception_fields(exc),
        ))
    return record

def format_duration(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def build_health_report():
    lines = []
    healthy = True

    started = time.perf_counter()
    try:
        await db_get("anon_users", {"select": "alias", "limit": "1"})
        lines.append(f"Database: ✅ {round((time.perf_counter() - started) * 1000)} ms")
    except Exception:
        healthy = False
        lines.append("Database: ❌ unreachable")

    try:
        info = await tg("getWebhookInfo", {})
        pending = info.get("pending_update_count", 0)
        last_error = info.get("last_error_message")
        mark = "✅" if not last_error and pending < 20 else "⚠️"
        text = f"Telegram webhook: {mark} {pending} pending"
        if last_error:
            text += f" · last error: {safe_error_message(last_error)[:80]}"
            healthy = False
        lines.append(text)
    except Exception:
        healthy = False
        lines.append("Telegram webhook: ❌ could not check")

    try:
        unmapped = await db_get("anon_album_buffer", {
            "status": "eq.delivered_unmapped", "select": "message_id", "limit": "1",
        })
        failed = await db_get("anon_album_buffer", {
            "status": "eq.failed", "select": "message_id", "limit": "1",
        })
        pending = await db_get("anon_album_buffer", {
            "status": "eq.pending", "select": "message_id", "limit": "1",
        })
        processing = await db_get("anon_album_buffer", {
            "status": "eq.processing", "select": "message_id", "limit": "1",
        })
        if unmapped or failed:
            healthy = False
            lines.append("Albums: ⚠️ delivery outcome needs review")
        elif pending or processing:
            lines.append("Albums: ✅ recovery queue active")
        else:
            lines.append("Albums: ✅ no queued work")
    except Exception:
        healthy = False
        lines.append("Albums: ❌ recovery queue unavailable")

    errors = _stats["errors"]
    line = f"Since start: {errors} errors, {_stats['warnings']} warnings"
    if _stats["last_error"]:
        event, when = _stats["last_error"]
        line += f" (last: {event} at {when.strftime('%H:%M')} UTC)"
    lines.append(line)

    meta = render_metadata()
    server = [meta.get("service"), meta.get("repo")]
    if meta.get("branch") or meta.get("commit"):
        server.append(f"{meta.get('branch', '')}@{(meta.get('commit') or '')[:7]}")
    server_text = " · ".join(part for part in server if part)
    if server_text:
        lines.append(f"Server: {server_text}")
    lines.append(f"Uptime: {format_duration(time.time() - _STARTED_AT)}")

    head = "✅ Anon is healthy" if healthy else "⚠️ Anon has problems"
    return head + "\n" + "\n".join(lines)


async def send_health_report(chat_id):
    await tg("sendMessage", {"chat_id": chat_id, "text": await build_health_report()})

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
        if not telegram_permanent_rejection(exc):
            raise

async def copy_album(source_uid, destination_uid, group_id, message_ids, reply_target):
    """Copy source messages as an album, preserving order and reply context."""
    payload = {
        "chat_id": destination_uid,
        "from_chat_id": source_uid,
        "message_ids": message_ids,
    }
    if reply_target:
        payload["reply_parameters"] = reply_params(reply_target)
    return await tg("copyMessages", payload)

async def buffer_album_message(m, sender_uid, destination_uid, direction, reply_to=None):
    """Fast path: store one album part. Returns (source, group, accepted)."""
    source_uid = ADMIN_ID if direction == "admin_to_user" else sender_uid
    group_id = str(m["media_group_id"])
    group_filter = {
        "source_chat_id": f"eq.{source_uid}",
        "destination_chat_id": f"eq.{destination_uid}",
        "media_group_id": f"eq.{group_id}",
        "select": "status,message_id",
        "order": "created_at.asc",
        "limit": "100",
    }
    existing = await db_get("anon_album_buffer", group_filter)
    if any(row["status"] != "pending" for row in existing):
        await react_status(source_uid, m["message_id"], False)
        await technical(
            "message.album_late_part_rejected",
            "WARNING",
            direction=direction,
            media_group_id=group_id,
            message_id=m["message_id"],
            existing_states=sorted({row["status"] for row in existing}),
        )
        return source_uid, group_id, False

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
    return source_uid, group_id, True


async def claim_album_messages(m, sender_uid, destination_uid, direction, reply_to=None):
    source_uid = ADMIN_ID if direction == "admin_to_user" else sender_uid
    group_id = str(m["media_group_id"])
    await asyncio.sleep(ALBUM_QUIET_SECONDS + 0.3)
    deadline = time.monotonic() + ALBUM_MAX_WAIT_SECONDS
    while True:
        rows = await db_rpc("anon_claim_album", {
            "p_source_chat_id": source_uid,
            "p_destination_chat_id": destination_uid,
            "p_media_group_id": group_id,
            "p_quiet_seconds": ALBUM_QUIET_SECONDS,
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
        pending = await db_get("anon_album_buffer", {
            "source_chat_id": f"eq.{source_uid}",
            "destination_chat_id": f"eq.{destination_uid}",
            "media_group_id": f"eq.{group_id}",
            "status": "eq.pending",
            "select": "message_id",
            "limit": "1",
        })
        if not pending or time.monotonic() >= deadline:
            return source_uid, group_id, []
        await asyncio.sleep(1)


async def finish_album(source_uid, destination_uid, group_id, mappings=None, status="delivered"):
    return await db_rpc("anon_finish_album", {
        "p_source_chat_id": source_uid,
        "p_destination_chat_id": destination_uid,
        "p_media_group_id": group_id,
        "p_mappings": mappings,
        "p_status": status,
    })


async def run_album_worker(key, factory):
    try:
        for attempt in range(4):
            try:
                await factory()
                return
            except Exception as exc:
                if telegram_rate_limited(exc) and attempt < 3:
                    await asyncio.sleep(telegram_retry_delay(exc, attempt))
                    continue
                await technical("message.album_delivery_failed", "ERROR",
                                **exception_fields(exc))
                return
    finally:
        _album_tasks.pop(key, None)


def start_album_worker(key, factory):
    task = _album_tasks.get(key)
    if task is not None and not task.done():
        return
    _album_tasks[key] = asyncio.create_task(run_album_worker(key, factory))


async def recover_pending_albums_once():
    """Recreate workers for quiet, durable album groups after process restarts."""
    expired_items = await db_rpc("anon_expire_stale_albums", {
        "p_lease_seconds": ALBUM_PROCESSING_LEASE_SECONDS,
    })
    if isinstance(expired_items, int) and expired_items:
        await technical(
            "message.album_stale_processing_marked_unknown",
            "ERROR",
            notify_group=False,
            item_count=expired_items,
        )

    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=ALBUM_QUIET_SECONDS)
    ).isoformat()
    rows = await db_get("anon_album_buffer", {
        "status": "eq.pending",
        "last_seen_at": f"lt.{cutoff}",
        "select": (
            "source_chat_id,destination_chat_id,sender_user_id,direction,"
            "media_group_id,message_id"
        ),
        "order": "created_at.asc",
        "limit": "500",
    })

    groups = {}
    for row in rows:
        key = (
            row["source_chat_id"],
            row["destination_chat_id"],
            str(row["media_group_id"]),
        )
        groups.setdefault(key, []).append(row)

    started = 0
    for (source_uid, destination_uid, group_id), items in groups.items():
        direction = items[0]["direction"]
        sender_uid = items[0]["sender_user_id"]
        message = {
            "message_id": max(item["message_id"] for item in items),
            "media_group_id": group_id,
            "from": {"id": sender_uid},
        }
        worker_key = (direction, source_uid, destination_uid, group_id)
        current = _album_tasks.get(worker_key)
        if current is not None and not current.done():
            continue

        if direction == "user_to_admin":
            user = await get_user(sender_uid)
            if not user:
                await technical(
                    "message.album_recovery_user_missing",
                    "ERROR",
                    notify_group=False,
                    message_count=len(items),
                )
                continue
            start_album_worker(
                worker_key,
                lambda m=message, u=user: deliver_album_to_admin(m, u),
            )
        elif direction == "admin_to_user":
            start_album_worker(
                worker_key,
                lambda m=message, uid=destination_uid: deliver_album_to_user(m, uid),
            )
        else:
            await technical(
                "message.album_recovery_invalid_direction",
                "ERROR",
                notify_group=False,
            )
            continue
        started += 1

    return started


async def album_recovery_loop():
    while True:
        try:
            await recover_pending_albums_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await technical(
                "message.album_recovery_scan_failed",
                "WARNING",
                notify_group=False,
                **exception_fields(exc),
            )
        await asyncio.sleep(ALBUM_RECOVERY_INTERVAL_SECONDS)


async def relay_album_to_admin(m, user, reply_to=None):
    """Webhook-facing: buffer this part and return immediately."""
    uid = m["from"]["id"]
    source_uid, group_id, accepted = await buffer_album_message(
        m, uid, ADMIN_ID, "user_to_admin", reply_to
    )
    if accepted:
        start_album_worker(
            ("user_to_admin", source_uid, ADMIN_ID, group_id),
            lambda: deliver_album_to_admin(m, user, reply_to),
        )


async def relay_album_to_user(m, uid, reply_to=None):
    """Webhook-facing: buffer this part and return immediately."""
    source_uid, group_id, accepted = await buffer_album_message(
        m, uid, uid, "admin_to_user", reply_to
    )
    if accepted:
        start_album_worker(
            ("admin_to_user", source_uid, uid, group_id),
            lambda: deliver_album_to_user(m, uid, reply_to),
        )


async def release_album_before_delivery(source_uid, destination_uid, group_id, stage):
    try:
        await finish_album(
            source_uid, destination_uid, group_id, status="pending"
        )
    except Exception as exc:
        await technical(
            "message.album_pre_delivery_retry_state_failed",
            "ERROR",
            notify_group=False,
            stage=stage,
            **exception_fields(exc),
        )


async def deliver_album_to_admin(m, user, reply_to=None):
    uid = m["from"]["id"]
    source_uid, group_id, rows = await claim_album_messages(
        m, uid, ADMIN_ID, "user_to_admin", reply_to
    )
    if not rows:
        return

    message_ids = sorted(row["message_id"] for row in rows)
    if user.get("blocked_by_admin"):
        await finish_album(source_uid, ADMIN_ID, group_id, status="failed")
        await react_status(uid, message_ids[-1], False)
        await technical(
            "message.album_delivery_skipped_blocked_user",
            "WARNING",
            notify_group=False,
            alias=user.get("alias"),
            message_count=len(message_ids),
        )
        return

    try:
        sessions = await db_get(
            "anon_admin_sessions",
            {"sender_user_id": f"eq.{uid}", "select": "*"},
        )
    except Exception:
        await release_album_before_delivery(
            source_uid, ADMIN_ID, group_id, "admin_session_lookup"
        )
        raise
    now = datetime.now(timezone.utc)
    header = not sessions
    if sessions:
        last = datetime.fromisoformat(
            sessions[0]["last_message_at"].replace("Z", "+00:00")
        )
        header = (now - last).total_seconds() >= 600

    if header:
        try:
            sent_header = await tg("sendMessage", {
                "chat_id": ADMIN_ID,
                "text": header_text(user["alias"]),
                "parse_mode": "HTML",
                "reply_markup": header_reply_markup(user, source_uid),
            })
        except Exception as exc:
            if telegram_rate_limited(exc):
                try:
                    await finish_album(source_uid, ADMIN_ID, group_id, status="pending")
                except Exception as persist_exc:
                    await technical(
                        "message.album_retry_state_failed", "ERROR",
                        direction="user_to_admin", alias=user["alias"],
                        stage="header_send", **exception_fields(persist_exc),
                    )
                raise
            terminal_status = (
                "failed" if telegram_permanent_rejection(exc) else "delivered_unmapped"
            )
            try:
                await finish_album(
                    source_uid, ADMIN_ID, group_id, status=terminal_status
                )
            except Exception as persist_exc:
                await technical(
                    "message.album_terminal_state_persistence_failed", "ERROR",
                    direction="user_to_admin", alias=user["alias"],
                    terminal_status=terminal_status, stage="header_send",
                    **exception_fields(persist_exc),
                )
            await react_status(uid, m["message_id"], False)
            await technical(
                "message.album_header_rejected" if terminal_status == "failed"
                else "message.album_header_outcome_unknown",
                "ERROR",
                direction="user_to_admin",
                alias=user["alias"],
                automatic_retry_suppressed=True,
                **exception_fields(exc),
            )
            return
        await persist_after_delivery(
            lambda: map_admin_message(sent_header["message_id"], uid),
            "message.album_header_mapping_persistence_failed",
            direction="user_to_admin",
            alias=user["alias"],
            admin_message_id=sent_header["message_id"],
            sender_user_id=uid,
        )
        await persist_after_delivery(
            lambda: db_post(
                "anon_admin_sessions",
                {
                    "sender_user_id": uid,
                    "last_message_at": now.isoformat(),
                    "last_header_message_id": sent_header["message_id"],
                },
                "resolution=merge-duplicates,return=minimal",
            ),
            "message.session_persistence_failed",
            direction="user_to_admin",
            sender_user_id=uid,
        )

    reply_target = next(
        (row.get("reply_to_message_id") for row in rows
         if row.get("reply_to_message_id")),
        None,
    )

    try:
        copied = await copy_album(source_uid, ADMIN_ID, group_id, message_ids, reply_target)
    except Exception as exc:
        if telegram_rate_limited(exc):
            try:
                await finish_album(source_uid, ADMIN_ID, group_id, status="pending")
            except Exception as persist_exc:
                await technical(
                    "message.album_retry_state_failed", "ERROR",
                    direction="user_to_admin", alias=user["alias"],
                    **exception_fields(persist_exc),
                )
            raise

        terminal_status = (
            "failed" if telegram_permanent_rejection(exc) else "delivered_unmapped"
        )
        try:
            await finish_album(
                source_uid, ADMIN_ID, group_id, status=terminal_status
            )
        except Exception as persist_exc:
            await technical(
                "message.album_terminal_state_persistence_failed", "ERROR",
                direction="user_to_admin", alias=user["alias"],
                terminal_status=terminal_status,
                **exception_fields(persist_exc),
            )
        await react_status(uid, m["message_id"], False)
        await technical(
            "message.album_delivery_rejected" if terminal_status == "failed"
            else "message.album_delivery_outcome_unknown",
            "ERROR",
            direction="user_to_admin",
            alias=user["alias"],
            automatic_retry_suppressed=True,
            **exception_fields(exc),
        )
        return

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

    if not header:
        await persist_after_delivery(
            lambda: db_post(
                "anon_admin_sessions",
                {"sender_user_id": uid, "last_message_at": now.isoformat()},
                "resolution=merge-duplicates,return=minimal",
            ),
            "message.album_session_update_failed",
            direction="user_to_admin",
            alias=user["alias"],
            message_count=len(message_ids),
        )

    await technical(
        "message.album_forwarded_to_admin",
        alias=user["alias"],
        message_count=len(message_ids),
        source_message_ids=message_ids,
        reply_context_included=any(row.get("reply_to_message_id") for row in rows),
    )


async def deliver_album_to_user(m, uid, reply_to=None):
    source_uid, group_id, rows = await claim_album_messages(
        m, uid, uid, "admin_to_user", reply_to
    )
    if not rows:
        return

    message_ids = sorted(row["message_id"] for row in rows)
    try:
        user = await get_user(uid)
    except Exception:
        await release_album_before_delivery(
            source_uid, uid, group_id, "recipient_lookup"
        )
        raise
    if not user or user.get("blocked_by_admin"):
        await finish_album(source_uid, uid, group_id, status="failed")
        await react(ADMIN_ID, message_ids[-1], "👎")
        await technical(
            "message.album_delivery_skipped_blocked_user",
            "WARNING",
            notify_group=False,
            message_count=len(message_ids),
        )
        return

    reply_target = next(
        (row.get("reply_to_message_id") for row in rows
         if row.get("reply_to_message_id")),
        None,
    )

    try:
        copied = await copy_album(source_uid, uid, group_id, message_ids, reply_target)
    except Exception as exc:
        if telegram_rate_limited(exc):
            try:
                await finish_album(source_uid, uid, group_id, status="pending")
            except Exception as persist_exc:
                await technical(
                    "message.album_retry_state_failed", "ERROR",
                    direction="admin_to_user",
                    **exception_fields(persist_exc),
                )
            raise

        terminal_status = (
            "failed" if telegram_permanent_rejection(exc) else "delivered_unmapped"
        )
        try:
            await finish_album(
                source_uid, uid, group_id, status=terminal_status
            )
        except Exception as persist_exc:
            await technical(
                "message.album_terminal_state_persistence_failed", "ERROR",
                direction="admin_to_user",
                terminal_status=terminal_status,
                **exception_fields(persist_exc),
            )
        await react(ADMIN_ID, m["message_id"], "👎")
        await technical(
            "message.album_delivery_rejected" if terminal_status == "failed"
            else "message.album_delivery_outcome_unknown",
            "ERROR",
            direction="admin_to_user",
            automatic_retry_suppressed=True,
            **exception_fields(exc),
        )
        return

    if not isinstance(copied, list) or len(copied) != len(message_ids):
        try:
            await finish_album(source_uid, uid, group_id, status="delivered_unmapped")
        except Exception as exc:
            await technical(
                "message.album_unmapped_state_persistence_failed",
                "ERROR",
                direction="admin_to_user",
                delivery_result_received=True,
                **exception_fields(exc),
            )
        await technical(
            "message.album_copy_incomplete", "ERROR",
            direction="admin_to_user",
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
            direction="admin_to_user",
            message_count=len(message_ids), delivery_already_completed=True,
            **exception_fields(exc),
        )
        return

    await technical(
        "message.admin_album_to_user_sent",
        alias=user.get("alias") if user else None,
        message_count=len(message_ids),
        source_message_ids=message_ids,
        reply_context_included=any(row.get("reply_to_message_id") for row in rows),
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
        try:
            state = await admin_state()
            is_active = state.get("active_user_id") == uid
            h=await tg("sendMessage",{
                "chat_id":ADMIN_ID,
                "text":header_text(user["alias"]),
                "parse_mode":"HTML",
                "reply_markup":header_reply_markup(user, uid, is_active),
            })
        except Exception as exc:
            if telegram_rate_limited(exc):
                raise
            await react_status(uid, m["message_id"], False)
            await technical(
                "message.header_delivery_rejected" if telegram_permanent_rejection(exc)
                else "message.header_delivery_outcome_unknown",
                "ERROR",
                direction="user_to_admin",
                alias=user["alias"],
                automatic_retry_suppressed=True,
                **exception_fields(exc),
            )
            return
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
                {
                    "sender_user_id": uid,
                    "last_message_at": now.isoformat(),
                    "last_header_message_id": h["message_id"],
                },
                "resolution=merge-duplicates,return=minimal",
            ),
            "message.session_persistence_failed",
            direction="user_to_admin",
            sender_user_id=uid,
        )
    p={"chat_id":ADMIN_ID,"from_chat_id":uid,"message_id":m["message_id"]}
    if reply_to: p["reply_parameters"]=reply_params(reply_to)
    try:
        cp=await tg("copyMessage",p)
    except Exception as exc:
        if telegram_rate_limited(exc):
            raise
        await react_status(uid, m["message_id"], False)
        await technical(
            "message.delivery_rejected" if telegram_permanent_rejection(exc)
            else "message.delivery_outcome_unknown",
            "ERROR",
            direction="user_to_admin",
            alias=user["alias"],
            automatic_retry_suppressed=True,
            **exception_fields(exc),
        )
        return

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
        if not telegram_permanent_rejection(exc):
            raise
        return False

async def send_to_user(m,uid,reply_to=None):
    if m.get("media_group_id"):
        routes = await db_rpc("anon_reserve_admin_album_route", {
            "p_source_chat_id": ADMIN_ID,
            "p_media_group_id": str(m["media_group_id"]),
            "p_candidate_user_id": uid,
            "p_reply_to_message_id": reply_to,
        })
        if not routes:
            raise RuntimeError("Admin album route reservation returned no recipient")

        route = routes[0]
        selected_uid = route["recipient_user_id"]
        await relay_album_to_user(
            m, selected_uid, route.get("reply_to_message_id")
        )
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

async def open_alias(alias,cmdid,exclude_header_message_id=None):
    u=await get_alias(alias)
    if not u or u.get("blocked_by_admin"):
        await react(ADMIN_ID,cmdid,"👎")
        return False
    uid=u["telegram_user_id"]
    await activate_alias(uid, exclude_header_message_id)
    await technical("admin.chat_selected", alias=u["alias"])
    return True


async def handle_admin_callback(query):
    callback_id = query.get("id")
    sender_id = (query.get("from") or {}).get("id")
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    data = query.get("data", "")

    if sender_id != ADMIN_ID or chat_id != ADMIN_ID:
        await tg("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Not authorized",
            "show_alert": True,
        })
        return

    match = re.fullmatch(r"anon:open:(\d+)", data)
    if not match:
        await tg("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Invalid button",
            "show_alert": True,
        })
        return

    user = await get_user(int(match.group(1)))
    if not user or user.get("blocked_by_admin"):
        await tg("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "This chat is unavailable",
            "show_alert": True,
        })
        return

    state = await admin_state()
    if state.get("active_user_id") == user["telegram_user_id"]:
        await close_active_alias(message.get("message_id"))
        is_open = False
        answer_text = "Chat closed"
    else:
        opened = await open_alias(
            user["alias"],
            message.get("message_id"),
            exclude_header_message_id=message.get("message_id"),
        )
        if not opened:
            await tg("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "This chat is unavailable",
                "show_alert": True,
            })
            return
        is_open = True
        answer_text = "Chat opened"

    await tg("editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message["message_id"],
        "reply_markup": header_reply_markup(user, is_open=is_open),
    })
    await tg("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": answer_text,
    })

async def mapped_admin_reply_target_for_user(uid, admin_message_id):
    rows = await db_get("anon_message_map", {
        "admin_message_id": f"eq.{admin_message_id}",
        "sender_user_id": f"eq.{uid}",
        "select": "user_message_id",
        "limit": "1",
    })
    if rows:
        return rows[0]["user_message_id"]
    rows = await db_get("anon_outbound_map", {
        "admin_message_id": f"eq.{admin_message_id}",
        "sender_user_id": f"eq.{uid}",
        "select": "user_message_id",
        "limit": "1",
    })
    return rows[0]["user_message_id"] if rows else None


async def route_admin(m):
    text=m.get("text","")
    if text.startswith("/") and await admin_command(m):
        return

    if m.get("media_group_id"):
        album_rows = await db_get("anon_album_routes", {
            "source_chat_id": f"eq.{ADMIN_ID}",
            "media_group_id": f"eq.{m['media_group_id']}",
            "select": "recipient_user_id,reply_to_message_id",
            "limit": "1",
        })
        if album_rows:
            uid = album_rows[0]["recipient_user_id"]
            reply_target = album_rows[0].get("reply_to_message_id")
            if reply_target is None and m.get("reply_to_message"):
                mapped_reply_target = await mapped_admin_reply_target_for_user(
                    uid, m["reply_to_message"]["message_id"]
                )
                if mapped_reply_target is not None:
                    await db_patch(
                        "anon_album_routes",
                        {
                            "source_chat_id": f"eq.{ADMIN_ID}",
                            "media_group_id": f"eq.{m['media_group_id']}",
                            "recipient_user_id": f"eq.{uid}",
                            "reply_to_message_id": "is.null",
                        },
                        {"reply_to_message_id": mapped_reply_target},
                    )
                    reserved = await db_rpc("anon_reserve_admin_album_route", {
                        "p_source_chat_id": ADMIN_ID,
                        "p_media_group_id": str(m["media_group_id"]),
                        "p_candidate_user_id": uid,
                        "p_reply_to_message_id": mapped_reply_target,
                    })
                    if reserved:
                        reply_target = (
                            reserved[0].get("reply_to_message_id")
                            or mapped_reply_target
                        )
                    else:
                        reply_target = mapped_reply_target
            user = await get_user(uid)
            if user and not user.get("blocked_by_admin"):
                try:
                    await send_to_user(m, uid, reply_target)
                except Exception as exc:
                    await technical(
                        "message.admin_album_delivery_failed",
                        "ERROR",
                        **exception_fields(exc),
                    )
                    await react(ADMIN_ID, m["message_id"], "👎")
                    raise
            else:
                await react(ADMIN_ID, m["message_id"], "👎")
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
                    if m.get("media_group_id") or telegram_rate_limited(exc):
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
                    if m.get("media_group_id") or telegram_rate_limited(exc):
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
                if m.get("media_group_id") or telegram_rate_limited(exc):
                    raise
        else:
            await react(ADMIN_ID,m["message_id"],"👎")
        return

    await react(ADMIN_ID,m["message_id"],"👎")
    await technical("admin.message_rejected_no_target")

async def admin_command(message):
    text = message.get("text", "")
    if re.fullmatch(r"/health(?:@\w+)?\s*", text, re.I):
        return True
    match=re.fullmatch(r"/i(?:@\w+)?(?:\s+(.+))?\s*",text,re.I)
    if match:
        arg=(match.group(1) or "").strip()
        if re.fullmatch(r"[A-Za-z]+-\d{2}",arg):
            await open_alias(arg,message["message_id"])
        else:
            await react(ADMIN_ID,message["message_id"],"👎")
        return True
    if re.fullmatch(r"/c(?:@\w+)?\s*",text,re.I):
        await close_active_alias()
        await technical("admin.chat_closed")
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
            await send_user(target["telegram_user_id"], "You have been blocked by Fwlai")
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
    rows = await db_rpc("anon_claim_update", {
        "p_update_id": update_id,
        "p_lease_seconds": 300,
    })
    if isinstance(rows, list) and rows:
        return rows[0]
    return {"claimed": False, "state": "missing"}


async def complete_update(update_id, lease_token):
    completed = await db_rpc("anon_complete_update", {
        "p_update_id": update_id,
        "p_lease_token": lease_token,
    })
    if completed is not True:
        raise RuntimeError("Telegram update lease could not be completed")


async def ack_update(update_id, lease_token=None):
    if update_id is not None:
        await complete_update(update_id, lease_token)
    return {"ok": True}


async def unmark_update(update_id, lease_token):
    try:
        r = await client.delete(
            f"{REST}/anon_processed_updates",
            params={
                "update_id": f"eq.{update_id}",
                "lease_token": f"eq.{lease_token}",
            },
            headers=db_headers("return=minimal"),
        )
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
            await db_delete(
                "anon_album_routes",
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
    update_type = next((key for key in (
        "message", "edited_message", "callback_query", "my_chat_member"
    ) if key in update), "unknown")
    await technical("telegram.update_received", notify_group=False,
                    update_type=update_type)
    lease_token = None
    if update_id is not None:
        try:
            claim = await mark_update(update_id)
            if not claim.get("claimed"):
                if claim.get("state") == "processed":
                    return {"ok": True, "duplicate": True}
                if claim.get("state") == "unknown":
                    await technical(
                        "telegram.update_lease_expired_unknown",
                        "ERROR",
                        notify_group=False,
                        update_id=update_id,
                        automatic_retry_suppressed=True,
                    )
                    return {"ok": True, "unknown": True}
                raise HTTPException(
                    status_code=503,
                    detail="Telegram update claim is not available yet",
                )
            lease_token = claim.get("lease_token")
            if not lease_token:
                raise RuntimeError("Telegram update claim returned no lease token")
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
        return await ack_update(update_id, lease_token)
    callback_query = update.get("callback_query")
    if callback_query:
        try:
            await handle_admin_callback(callback_query)
        except Exception as exc:
            await technical("admin.callback_failed", "ERROR", **exception_fields(exc))
            if update_id is not None:
                await unmark_update(update_id, lease_token)
            raise HTTPException(status_code=503, detail="callback handling failed")
        return await ack_update(update_id, lease_token)

    edited = update.get("edited_message")
    message = update.get("message") or edited
    if not message:
        return await ack_update(update_id, lease_token)
    sender = message.get("from") or {}
    uid = sender.get("id")
    if not uid or sender.get("is_bot"):
        return await ack_update(update_id, lease_token)

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
        if (
            registered and int(chat.get("id", 0)) == registered
            and uid == ADMIN_ID and not edited
            and re.fullmatch(r"/health(?:@\w+)?\s*", message.get("text", ""), re.I)
        ):
            try:
                await send_health_report(chat["id"])
            except Exception as exc:
                await technical("system.health_report_failed", "ERROR",
                                notify_group=False, **exception_fields(exc))
            return await ack_update(update_id, lease_token)
        return await ack_update(update_id, lease_token)

    if uid == ADMIN_ID:
        if edited:
            try:
                await edit_admin_outbound_copy(message)
            except Exception as exc:
                await technical("message.admin_edit_relay_failed", "ERROR",
                                admin_message_id=message.get("message_id"),
                                **exception_fields(exc))
                if not telegram_permanent_rejection(exc):
                    if update_id is not None:
                        await unmark_update(update_id, lease_token)
                    raise HTTPException(status_code=503, detail="admin edit relay failed")
            return await ack_update(update_id, lease_token)
        try:
            await route_admin(message)
        except Exception as exc:
            await technical("admin.routing_failed", "ERROR", **exception_fields(exc))
            if update_id is not None: await unmark_update(update_id, lease_token)
            raise HTTPException(status_code=503, detail="admin routing failed")
        return await ack_update(update_id, lease_token)

    if edited:
        try:
            await edit_incoming_copy(message)
        except Exception as exc:
            await technical("message.incoming_edit_handler_failed", "ERROR", **exception_fields(exc))
            if not telegram_permanent_rejection(exc):
                if update_id is not None:
                    await unmark_update(update_id, lease_token)
                raise HTTPException(status_code=503, detail="incoming edit relay failed")
        return await ack_update(update_id, lease_token)
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
                await unmark_update(update_id, lease_token)
            raise HTTPException(status_code=503, detail="start failed")
        return await ack_update(update_id, lease_token)

    user = await get_user(uid)
    if not user:
        try:
            user, created = await allocate_alias(uid)
        except Exception as exc:
            await technical("user.alias_allocation_failed", "ERROR", **exception_fields(exc))
            if update_id is not None:
                await unmark_update(update_id, lease_token)
            raise HTTPException(status_code=503, detail="alias allocation failed")
        if created:
            await technical("user.registered", alias=user["alias"], registration_source="first_message")
    try:
        await handle_user(message, user)
    except Exception as exc:
        await technical("message.webhook_handler_failed", "ERROR", alias=user["alias"],
                        message_type=kind_of(message), **exception_fields(exc))
        if update_id is not None:
            await unmark_update(update_id, lease_token)
        raise HTTPException(status_code=503, detail="relay failed")
    return await ack_update(update_id, lease_token)

@app.on_event("startup")
async def startup():
    global _album_recovery_task
    await technical("system.startup_started", notify_group=False)
    if all([BOT_TOKEN, ADMIN_ID, WEBHOOK_SECRET, SUPABASE_URL, SUPABASE_KEY]):
        try:
            await db_get("anon_users", {"select": "telegram_user_id", "limit": "1"})
            await db_rpc("anon_expire_stale_albums", {
                "p_lease_seconds": ALBUM_PROCESSING_LEASE_SECONDS,
            })
            base_url = os.getenv("RENDER_EXTERNAL_URL", "https://anon-mr5s.onrender.com").rstrip("/")
            await tg("setWebhook", {
                "url": f"{base_url}/telegram/webhook",
                "secret_token": WEBHOOK_SECRET,
                "allowed_updates": ["message", "edited_message", "callback_query", "my_chat_member"]
            })
            _album_recovery_task = asyncio.create_task(album_recovery_loop())
            await technical("system.startup_completed")
        except Exception as exc:
            await technical("system.startup_failed", "ERROR", **exception_fields(exc))
            raise RuntimeError("Anon startup checks failed; refusing to serve webhooks") from exc
    else:
        missing = [name for name, value in {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ADMIN_ID": ADMIN_ID,
            "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "SUPABASE_URL": SUPABASE_URL,
            "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_KEY,
        }.items() if not value]
        await technical("system.configuration_incomplete", "ERROR", missing_variables=missing)
        raise RuntimeError(
            "Anon configuration is incomplete; refusing to serve webhooks"
        )

@app.on_event("shutdown")
async def shutdown():
    global _album_recovery_task
    if BOT_TOKEN and ADMIN_ID:
        try:
            await technical("system.shutdown_started")
        except Exception as exc:
            log.error(make_log_record("system.shutdown_log_failed", "ERROR",
                                      **exception_fields(exc)))
    tasks = []
    if _album_recovery_task is not None:
        _album_recovery_task.cancel()
        tasks.append(_album_recovery_task)
        _album_recovery_task = None
    tasks.extend(list(_album_tasks.values()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await client.aclose()
