#!/usr/bin/env python3
"""
Optimized async HTTP API for sending iMessages via imessage_monitor.
Includes:
 - Scalable single-producer queue for outbound messages
 - Unified async send worker
 - Shared httpx client
 - Simplified inbound forwarding
 - Local SQLite staging for outbound sync to ORDERFLOW
 - Clean FaceTime watcher & restart logic
"""

from __future__ import annotations

import os
import logging
import asyncio
import subprocess
import shutil
import sqlite3
from typing import Optional, Annotated
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
import httpx
import uvicorn
import time
import re
import hashlib

from imessage_monitor.monitor import iMessageMonitor
from imessage_monitor.outbound import OutboundMessageSender
from imessage_monitor.exceptions import OutboundMessageError

from dotenv import load_dotenv

import message_store

load_dotenv()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("imessage_api")

COOLDOWN = 20  # per-call cooldown
GLOBAL_DEBOUNCE = 2  # prevent burst duplicates
cooldowns = {}
last_global = 0

UUID_REGEX = r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"


# ================================================================
#                  MODELS / VALIDATION
# ================================================================

NonEmptyStr = Annotated[str, Field(min_length=1)]

class SendRequest(BaseModel):
    to: NonEmptyStr
    message: Annotated[str, Field(min_length=1, max_length=10000)]

    @model_validator(mode="before")
    def strip_whitespace(cls, values):
        if isinstance(values.get("to"), str):
            values["to"] = values["to"].strip()
        if isinstance(values.get("message"), str):
            values["message"] = values["message"].strip()
        return values


# ================================================================
#                  GLOBALS — SINGLETONS
# ================================================================
app = FastAPI(title="iMessage HTTP API", version="0.2.0")

monitor: Optional[iMessageMonitor] = None
outbound: Optional[OutboundMessageSender] = None

SEND_QUEUE: asyncio.Queue = asyncio.Queue()
HTTP = httpx.AsyncClient(timeout=10)

API_KEY = os.environ.get("IMESSAGE_API_KEY", "changeme")
USER_ID = os.environ.get("IMESSAGE_USER_ID")
USER_ID_2 = os.environ.get("IMESSAGE_USER_ID_2")
FWD_URL = os.environ.get("FWD_URL", "https://zappd.app/sms/reply")
FWD_URL_2 = os.environ.get("FWD_URL_2", "https://corn.zappd.ai/sms/reply")
CONTACT = os.environ.get("CONTACT")
CONTACT_2 = os.environ.get("CONTACT_2")
INBOUND_BATCH_DELAY_SECONDS = float(
    os.environ.get("INBOUND_BATCH_DELAY_SECONDS", "0")
)
EDIT_POLL_INTERVAL_SECONDS = float(
    os.environ.get("EDIT_POLL_INTERVAL_SECONDS", "0.5")
)
EDIT_POLL_LIMIT = int(os.environ.get("EDIT_POLL_LIMIT", "80"))
OUTBOUND_MATCH_WINDOW_SECONDS = int(
    os.environ.get("OUTBOUND_MATCH_WINDOW_SECONDS", "300")
)
IMESSAGE_DB_PATH = os.environ.get("IMESSAGE_DB_PATH")
CHAT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
APPLE_SCRIPT = '''
set appName to "FaceTime"
tell application appName
    if it is running then quit
end tell
delay 1
do shell script "
  killall 'FaceTime' 2>/dev/null || true;
  killall 'avconferenced' 2>/dev/null || true;
  killall 'CallHistoryPluginHelper' 2>/dev/null || true;
"
'''

APPLE_DECLINE_ONLY = '''
use AppleScript version "2.4"
use scripting additions

tell application "System Events"
    tell process "NotificationCenter"
        try
            set callWindow to (first window whose (exists button "Decline") and (exists button "Accept"))

            set callerName to ""
            try
                if exists static text 1 of callWindow then
                    set callerName to value of static text 1 of callWindow
                end if
            end try

            try
                if callerName is "" then
                    set callerName to value of static text 1 of group 1 of UI element 1 of scroll area 1 of callWindow
                end if
            end try

            click button "Decline" of callWindow

            if callerName is not "" then
                display notification "Declined call from " & callerName with title "Call Auto-Declined"
            else
                display notification "Call declined" with title "Call Auto-Declined"
            end if

        on error errMsg
            display alert "No active incoming call notification found." message ("Error: " & errMsg)
        end try
    end tell
end tell
'''


# ================================================================
#                  AUTH
# ================================================================

async def require_api_key(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")

    key = auth.split(" ", 1)[1].strip()
    if key != API_KEY:
        raise HTTPException(403, "Invalid API key")

    return True


# ================================================================
#                  OUTBOUND QUEUE WORKER
# ================================================================

def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def send_message_via_osascript(to: str, message: str) -> None:
    if not shutil.which("osascript"):
        raise RuntimeError("osascript not found. This sender requires macOS.")

    script = (
        'tell application "Messages"\n'
        f'    send "{_escape_applescript_string(message)}" to buddy "{_escape_applescript_string(to)}"\n'
        "end tell\n"
    )

    process = await asyncio.create_subprocess_exec(
        "osascript",
        "-e",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        details = (stderr or stdout or b"").decode("utf-8", "ignore").strip()
        raise OutboundMessageError(details or f"osascript failed with exit code {process.returncode}")


async def send_worker(outbound: OutboundMessageSender):
    while True:
        to, message = await SEND_QUEUE.get()
        try:
            await send_message_via_osascript(to, message)
            logger.info("Sent message to %s", to)
        except OutboundMessageError as exc:
            logger.error("OutboundMessageError sending to %s: %s", to, exc)
        except Exception as exc:
            logger.exception("Unexpected send error to %s: %s", to, exc)
        finally:
            SEND_QUEUE.task_done()


async def enqueue_send(to: str, message: str):
    await SEND_QUEUE.put((to, message))


# ================================================================
#                  MESSAGE HELPERS
# ================================================================

_known_bodies: dict[str, str] = {}
_forwarded_guids: set[str] = set()
_seen_guids: set[str] = set()
_last_customer_body: dict[str, str] = {}
_edit_poll_running = False


def _message_body(message: dict) -> str:
    raw = (
        message.get("message_text")
        or message.get("decoded_attributed_body")
        or ""
    )
    x=message_store.normalize_body(raw)
    return x


def _normalize_guid(guid: str) -> str:
    return guid.strip().lower()


def _message_guid(message: dict) -> Optional[str]:
    raw = message.get("message_guid") or message.get("guid")
    if not raw or not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    return _normalize_guid(trimmed) if trimmed else None


def _sender_key(message: dict) -> Optional[str]:
    return (
        message.get("handle_id_str")
        or message.get("uncanonicalized_id")
        or message.get("chat_identifier")
    )


def _customer_peer(message: dict) -> Optional[str]:
    return _sender_key(message) or message.get("chat_identifier")


def _lookup_chat_addressed_info(message: dict) -> tuple[Optional[str], Optional[str]]:
    """Read chat.last_addressed_handle and last_addressed_sim_id from chat.db."""
    message_id = message.get("message_id")
    chat_guid = message.get("chat_guid")
    chat_identifier = message.get("chat_identifier")

    if message_id is None and not chat_guid and not chat_identifier:
        return None, None

    try:
        conn = sqlite3.connect(f"file:{CHAT_DB_PATH}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            if message_id is not None:
                cur.execute(
                    """
                    SELECT c.last_addressed_handle, c.last_addressed_sim_id
                    FROM chat c
                    JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
                    WHERE cmj.message_id = ?
                    LIMIT 1
                    """,
                    (message_id,),
                )
            elif chat_guid:
                cur.execute(
                    """
                    SELECT last_addressed_handle, last_addressed_sim_id
                    FROM chat
                    WHERE guid = ?
                    LIMIT 1
                    """,
                    (chat_guid,),
                )
            else:
                cur.execute(
                    """
                    SELECT last_addressed_handle, last_addressed_sim_id
                    FROM chat
                    WHERE chat_identifier = ?
                    LIMIT 1
                    """,
                    (chat_identifier,),
                )
            row = cur.fetchone()
            if not row:
                return None, None
            return row[0], row[1]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Failed to lookup chat addressed info: %s", exc)
        return None, None


def _normalize_handle(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped.casefold() if stripped else None


def _dual_contact_routing_enabled() -> bool:
    return bool(_normalize_handle(CONTACT) and _normalize_handle(CONTACT_2))


async def _resolve_forward_target(message: dict) -> tuple[str, Optional[str]]:
    """Return (fwd_url, user_id) based on CONTACT/CONTACT_2 vs last_addressed_handle."""
    if not _dual_contact_routing_enabled():
        return FWD_URL, USER_ID

    last_addressed_handle, _ = await asyncio.to_thread(
        _lookup_chat_addressed_info, message
    )
    handle = _normalize_handle(last_addressed_handle)
    contact = _normalize_handle(CONTACT)
    contact_2 = _normalize_handle(CONTACT_2)

    if handle and handle == contact:
        logger.debug(
            "Routing to primary FWD_URL (CONTACT match) handle=%s",
            last_addressed_handle,
        )
        return FWD_URL, USER_ID

    if handle and handle == contact_2:
        if not USER_ID_2:
            logger.error(
                "IMESSAGE_USER_ID_2 not set; cannot route CONTACT_2 match handle=%s",
                last_addressed_handle,
            )
        logger.debug(
            "Routing to FWD_URL_2 (CONTACT_2 match) handle=%s",
            last_addressed_handle,
        )
        return FWD_URL_2, USER_ID_2

    logger.warning(
        "last_addressed_handle=%r did not match CONTACT/CONTACT_2; falling back to primary",
        last_addressed_handle,
    )
    return FWD_URL, USER_ID


def _filter_consecutive_repeats(messages: list[dict], customer: str) -> list[dict]:
    filtered: list[dict] = []
    prev_body = _last_customer_body.get(customer)
    for message in messages:
        body = _message_body(message)
        if body and body == prev_body:
            guid = _message_guid(message)
            logger.info(
                "Skipping consecutive repeat inbound guid=%s customer=%s",
                guid,
                customer,
            )
            continue
        filtered.append(message)
        prev_body = body
    if filtered:
        _last_customer_body[customer] = prev_body
    return filtered


# ================================================================
#                  SQLITE SYNC
# ================================================================

async def _run_store(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def sync_message_to_sqlite(message: dict) -> None:
    guid = _message_guid(message)
    body = _message_body(message)
    if not guid or not body:
        return

    peer = _customer_peer(message)
    if not peer:
        return

    is_from_me = bool(message.get("is_from_me"))
    sender = "cashier" if is_from_me else "customer"
    await _run_store(
        message_store.upsert_mirror,
        guid=guid,
        body=body,
        peer=peer,
        sender=sender,
        is_from_me=is_from_me,
        status="synced" if is_from_me else "received",
    )


# ================================================================
#                  INBOUND / OUTBOUND FORWARDING
# ================================================================

@dataclass
class _SenderBatch:
    messages_by_guid: dict[str, dict] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    flush_task: Optional[asyncio.Task] = None


_inbound_batches: dict[str, _SenderBatch] = {}
_inbound_batches_lock = asyncio.Lock()


def _batch_messages_in_order(batch: _SenderBatch) -> list[dict]:
    return [batch.messages_by_guid[g] for g in batch.order if g in batch.messages_by_guid]


def _refresh_batch_bodies(batch: _SenderBatch, recent_messages: list[dict]) -> int:
    """Replace pending batch entries with the latest chat.db bodies before flush."""
    by_guid: dict[str, dict] = {}
    for msg in recent_messages:
        guid = _message_guid(msg)
        if guid and not msg.get("is_from_me"):
            by_guid[guid] = msg

    updated = 0
    for guid in batch.messages_by_guid:
        fresh = by_guid.get(guid)
        if not fresh:
            continue
        new_body = _message_body(fresh)
        old_body = _message_body(batch.messages_by_guid[guid])
        batch.messages_by_guid[guid] = fresh
        _known_bodies[guid] = new_body
        if new_body != old_body:
            updated += 1
    return updated


async def _refresh_batch_from_monitor(batch: _SenderBatch) -> int:
    if not monitor:
        return 0
    try:
        recent = await asyncio.to_thread(
            monitor.get_recent_messages, EDIT_POLL_LIMIT
        )
    except Exception as exc:
        logger.warning("Pre-flush refresh failed: %s", exc)
        return 0
    return _refresh_batch_bodies(batch, recent)


def _remember_body(message: dict) -> None:
    guid = _message_guid(message)
    if guid:
        _known_bodies[guid] = _message_body(message)


def _mark_messages_forwarded(messages: list[dict]) -> None:
    for message in messages:
        guid = _message_guid(message)
        if guid:
            _forwarded_guids.add(guid)
            _known_bodies[guid] = _message_body(message)


def _build_inbound_payload(customer: str, message: dict, user_id: str) -> dict:
    guid = _message_guid(message)
    if not guid:
        raise ValueError("message missing guid")
    return {
        "From": customer,
        "To": message.get("chat_identifier") or "unknown",
        "Body": _message_body(message),
        "userId": user_id,
        "messageId": guid,
        "sender": "customer",
    }


def _build_edit_payload(customer: str, message: dict, user_id: str) -> dict:
    guid = _message_guid(message)
    if not guid:
        raise ValueError("message missing guid")
    return {
        "From": customer,
        "To": message.get("chat_identifier") or "unknown",
        "Body": _message_body(message),
        "userId": user_id,
        "messageId": guid,
        "sender": "customer",
        "edit": True,
    }


def _build_outbound_payload(customer: str, message: dict, user_id: str) -> dict:
    guid = _message_guid(message)
    if not guid:
        raise ValueError("message missing guid")
    return {
        "From": customer,
        "To": message.get("chat_identifier") or "unknown",
        "Body": _message_body(message),
        "userId": user_id,
        "messageId": guid,
        "sender": "cashier",
    }


async def _post_forward_payload(
    payload: dict,
    log_label: str,
    fwd_url: str,
    *,
    is_edit: bool = False,
) -> None:
    guid = payload.get("messageId")
    kind = "edit" if is_edit else payload.get("sender", "message")
    try:
        print(payload)
        response = await HTTP.post(fwd_url, json=payload)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            data = {}
        action = data.get("action")
        message_id = data.get("messageId")
        if action or message_id:
            logger.info(
                "Forwarded %s %s guid=%s url=%s ORDERFLOW action=%s messageId=%s",
                kind,
                log_label,
                guid,
                fwd_url,
                action,
                message_id,
            )
        else:
            logger.info(
                "Forwarded %s %s guid=%s url=%s", kind, log_label, guid, fwd_url
            )
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500] if e.response is not None else ""
        logger.warning(
            "ORDERFLOW rejected %s guid=%s status=%s body=%s",
            kind,
            guid,
            e.response.status_code if e.response else "?",
            body,
        )
        if guid:
            await _run_store(message_store.mark_failed, guid)
        raise
    except Exception as e:
        logger.warning("Failed to POST %s guid=%s to %s: %s", kind, guid, fwd_url, e)
        if guid:
            await _run_store(message_store.mark_failed, guid)
        raise

    if guid:
        _forwarded_guids.add(guid)
        await _run_store(message_store.mark_synced, guid)


async def forward_outbound_to_orderflow(message: dict, customer: str) -> None:
    guid = _message_guid(message)
    if not guid:
        logger.warning("Outbound message missing guid; skipping ORDERFLOW sync")
        return

    if await _run_store(message_store.should_skip_outbound_forward, guid):
        logger.info(
            "Skipping ORDERFLOW sync for guid=%s (already in local DB)",
            guid,
        )
        return

    fwd_url, user_id = await _resolve_forward_target(message)
    if not user_id:
        logger.error(
            "No userId resolved for outbound sync guid=%s; cannot sync",
            guid,
        )
        return

    try:

        payload = _build_outbound_payload(customer, message, user_id)
        print(payload)
        await _post_forward_payload(payload, f"to {customer}", fwd_url)
    except Exception as e:
        logger.warning("Failed to sync outbound guid=%s to ORDERFLOW: %s", guid, e)


async def _forward_messages_now(messages: list[dict], customer: str) -> None:
    if not messages:
        return

    messages = _filter_consecutive_repeats(messages, customer)
    if not messages:
        return

    for message in messages:
        guid = _message_guid(message)
        if not guid:
            logger.warning("Inbound message missing guid; skipping forward")
            continue
        try:
            fwd_url, user_id = await _resolve_forward_target(message)
            if not user_id:
                logger.error(
                    "No userId resolved for inbound forward guid=%s; skipping",
                    guid,
                )
                continue
            payload = _build_inbound_payload(customer, message, user_id)
            await _post_forward_payload(payload, f"from {customer}", fwd_url)
            _mark_messages_forwarded([message])
        except Exception as e:
            logger.warning("Failed to forward inbound guid=%s: %s", guid, e)


async def _forward_edit(message: dict, customer: str) -> None:
    guid = _message_guid(message)
    if not guid:
        return
    fwd_url, user_id = await _resolve_forward_target(message)
    if not user_id:
        logger.error(
            "No userId resolved for edit forward guid=%s; skipping",
            guid,
        )
        return
    payload = _build_edit_payload(customer, message, user_id)
    try:
        await _post_forward_payload(
            payload, f"from {customer}", fwd_url, is_edit=True
        )
        _known_bodies[guid] = _message_body(message)
        logger.info("Forwarded edit guid=%s from %s", guid, customer)
    except Exception as e:
        logger.warning("Failed to forward edit guid=%s: %s", guid, e)


async def _flush_sender_batch_after_delay(customer: str) -> None:
    try:
        await asyncio.sleep(INBOUND_BATCH_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    async with _inbound_batches_lock:
        batch = _inbound_batches.pop(customer, None)
    if batch and batch.messages_by_guid:
        refreshed = await _refresh_batch_from_monitor(batch)
        if refreshed:
            logger.info(
                "Pre-flush coalesced %s edit(s) for customer=%s (normal forward, not edit)",
                refreshed,
                customer,
            )
        await _forward_messages_now(_batch_messages_in_order(batch), customer)


async def _enqueue_inbound_batch(message: dict, customer: str) -> None:
    guid = _message_guid(message)
    if not guid:
        logger.warning("Inbound message missing guid; skipping batch")
        return

    async with _inbound_batches_lock:
        batch = _inbound_batches.setdefault(customer, _SenderBatch())
        existing = batch.messages_by_guid.get(guid)
        if guid not in batch.messages_by_guid:
            batch.order.append(guid)
        batch.messages_by_guid[guid] = message
        if batch.flush_task and not batch.flush_task.done():
            batch.flush_task.cancel()
        batch.flush_task = asyncio.create_task(
            _flush_sender_batch_after_delay(customer)
        )
    if existing and _message_body(existing) != _message_body(message):
        logger.info(
            "Coalesced monitor edit into pending batch guid=%s customer=%s",
            guid,
            customer,
        )
    _remember_body(message)
    _seen_guids.add(guid)


async def flush_all_inbound_batches() -> None:
    async with _inbound_batches_lock:
        pending = dict(_inbound_batches)
        _inbound_batches.clear()

    for customer, batch in pending.items():
        if batch.flush_task and not batch.flush_task.done():
            batch.flush_task.cancel()
        if batch.messages_by_guid:
            refreshed = await _refresh_batch_from_monitor(batch)
            if refreshed:
                logger.info(
                    "Pre-flush coalesced %s edit(s) for customer=%s (normal forward, not edit)",
                    refreshed,
                    customer,
                )
            await _forward_messages_now(_batch_messages_in_order(batch), customer)


async def _update_pending_edit(message: dict, customer: str) -> bool:
    guid = _message_guid(message)
    if not guid:
        return False

    async with _inbound_batches_lock:
        batch = _inbound_batches.get(customer)
        if not batch or guid not in batch.messages_by_guid:
            return False
        batch.messages_by_guid[guid] = message
        if batch.flush_task and not batch.flush_task.done():
            batch.flush_task.cancel()
        batch.flush_task = asyncio.create_task(
            _flush_sender_batch_after_delay(customer)
        )
    _known_bodies[guid] = _message_body(message)
    await sync_message_to_sqlite(message)
    logger.info(
        "Coalesced edit into pending batch guid=%s customer=%s (will forward as new, not edit)",
        guid,
        customer,
    )
    return True


async def _process_edit_poll_message(message: dict) -> None:
    if message.get("is_from_me"):
        await sync_message_to_sqlite(message)
        return

    guid = _message_guid(message)
    if not guid:
        return

    body = _message_body(message)
    if _known_bodies.get(guid) == body:
        return

    customer = _sender_key(message)
    if not customer:
        return

    pending_customer: Optional[str] = None
    async with _inbound_batches_lock:
        for batch_customer, batch in _inbound_batches.items():
            if guid in batch.messages_by_guid:
                pending_customer = batch_customer
                break

    if pending_customer:
        await _update_pending_edit(message, pending_customer)
        return

    if guid in _forwarded_guids:
        await _forward_edit(message, customer)
        return

    _known_bodies[guid] = body
    if guid not in _seen_guids:
        _seen_guids.add(guid)
        await forward_incoming_message(message)


def _seed_recent_messages(mon: iMessageMonitor) -> None:
    loop = asyncio.get_event_loop()
    for message in mon.get_recent_messages(EDIT_POLL_LIMIT):
        guid = _message_guid(message)
        if not guid:
            continue
        _seen_guids.add(guid)
        _known_bodies[guid] = _message_body(message)
        if _message_body(message) and _customer_peer(message):
            loop.create_task(sync_message_to_sqlite(message))


async def _run_edit_poll_once() -> None:
    if not monitor:
        return
    try:
        messages = await asyncio.to_thread(
            monitor.get_recent_messages, EDIT_POLL_LIMIT
        )
    except Exception as e:
        logger.warning("Edit poll failed: %s", e)
        return
    for message in messages:
        await _process_edit_poll_message(message)


async def _edit_poll_loop() -> None:
    global _edit_poll_running
    _edit_poll_running = True
    while _edit_poll_running:
        await _run_edit_poll_once()
        await asyncio.sleep(EDIT_POLL_INTERVAL_SECONDS)


async def handle_monitor_message(message: dict) -> None:
    print(f"Received message: {message.get('message_text')}")
    if message.get("is_from_me"):
        guid = _message_guid(message)
        body = _message_body(message)
        
        customer = _customer_peer(message)
        if not guid or not body or not customer:
            return

        _last_customer_body.pop(customer, None)

        pending = await _run_store(
            message_store.find_pending_outbound,
            peer=customer,
            body=body,
            max_age_seconds=OUTBOUND_MATCH_WINDOW_SECONDS,
        )
        if pending:
            await _run_store(message_store.attach_guid, pending["id"], guid)
            logger.info(
                "Matched pending /send row id=%s to guid=%s customer=%s (local only, no /sms/reply)",
                pending["id"],
                guid,
                customer,
            )
            return

        await sync_message_to_sqlite(message)
        await forward_outbound_to_orderflow(message, customer)
        return

    await sync_message_to_sqlite(message)
    await forward_incoming_message(message)


async def forward_incoming_message(message: dict):
    if message.get("is_from_me"):
        return

    customer = _sender_key(message)
    if not customer:
        return

    guid = _message_guid(message)
    if guid:
        _seen_guids.add(guid)
        _remember_body(message)

    if INBOUND_BATCH_DELAY_SECONDS <= 0:
        await _forward_messages_now([message], customer)
        return

    await _enqueue_inbound_batch(message, customer)


# ================================================================
#                  FACETIME WATCH / AUTO-RESTART
# ================================================================

async def restart_messages():
    try:
        subprocess.run(["osascript", "-e", APPLE_SCRIPT], check=True)
        logger.info("Messages app restarted")
    except subprocess.CalledProcessError as e:
        logger.warning("AppleScript error: %s", e)


async def run_auto_decline_applescript():
    process = await asyncio.create_subprocess_exec(
        "osascript", "-e", APPLE_DECLINE_ONLY,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if stdout:
        logger.debug("AppleScript output: %s", stdout.decode())
    if stderr:
        logger.warning("AppleScript stderr: %s", stderr.decode())


async def watch_for_facetime_notifications():
    global cooldowns, last_global

    process = await asyncio.create_subprocess_shell(
        "log stream --predicate 'eventMessage contains \"FaceTime\"' --info",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    while True:
        line = await process.stdout.readline()
        if not line:
            break

        text = line.decode("utf-8", "ignore")

        if "incoming" not in text.lower():
            continue

        match = re.search(UUID_REGEX, text)
        if match:
            call_id = match.group(0).replace("-", "")
        else:
            call_id = "fallback-" + hashlib.sha1(text.encode()).hexdigest()[:12]

        now = time.time()

        if now - last_global < GLOBAL_DEBOUNCE:
            continue
        last_global = now

        last_event = cooldowns.get(call_id, 0)
        if now - last_event < COOLDOWN:
            logger.debug("Duplicate prevented (cooldown): %s", call_id)
            continue

        cooldowns[call_id] = now

        logger.info("Incoming FaceTime (ID=%s) — restarting Messages", call_id)

        await restart_messages()

        await enqueue_send(
            "",
            "Corn On The Corner, This is our storefront location: "
            "1041 Howard St, Dearborn, MI 48124. Please text your order "
            "including a name and confirm the given pick up time. Thank you."
        )


# ================================================================
#                  STARTUP / SHUTDOWN
# ================================================================

@app.on_event("startup")
async def startup_event():
    global monitor, outbound

    db_path = IMESSAGE_DB_PATH or str(message_store.default_db_path())
    await _run_store(message_store.init_db, db_path)

    if not USER_ID:
        logger.error("IMESSAGE_USER_ID is not set; inbound forward to ORDERFLOW will fail")

    if _dual_contact_routing_enabled():
        logger.info(
            "Dual-contact routing enabled (CONTACT / CONTACT_2 → FWD_URL / FWD_URL_2)"
        )
        if not USER_ID_2:
            logger.warning(
                "IMESSAGE_USER_ID_2 is not set; CONTACT_2 matches will fail to forward"
            )
    else:
        logger.info("Dual-contact routing disabled; using primary FWD_URL + USER_ID")

    loop = asyncio.get_event_loop()

    monitor = iMessageMonitor()
    outbound = OutboundMessageSender(monitor.config)
    _seed_recent_messages(monitor)

    monitor.start(
        message_callback=lambda msg: loop.create_task(
            handle_monitor_message(msg)
        )
    )

    asyncio.create_task(send_worker(outbound))
    asyncio.create_task(_edit_poll_loop())

    logger.info("iMessage monitor started")
    logger.info("Message store: %s", db_path)
    logger.info(
        "Edit poll every %ss (recent %s messages)",
        EDIT_POLL_INTERVAL_SECONDS,
        EDIT_POLL_LIMIT,
    )
    logger.info(
        "Outbound match window: %ss",
        OUTBOUND_MATCH_WINDOW_SECONDS,
    )
    logger.info("Outbound queue worker running")
    logger.info("FaceTime watcher started")
    if INBOUND_BATCH_DELAY_SECONDS > 0:
        logger.info(
            "Inbound batch delay: %ss per sender",
            INBOUND_BATCH_DELAY_SECONDS,
        )


@app.on_event("shutdown")
async def shutdown_event():
    global monitor, _edit_poll_running
    _edit_poll_running = False
    await flush_all_inbound_batches()
    try:
        if monitor:
            monitor.stop()
    except Exception:
        pass


# ================================================================
#                  API ROUTES
# ================================================================
@app.head("/")
@app.get("/")
async def home():
    return {
        "status": "ok",
        "service": "zappd_node",
        "uptime": "running",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/send/v2")
async def send_message_v2(req: SendRequest):
    if not shutil.which("osascript"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="osascript not found. This sender requires macOS.",
        )

    to = _escape_applescript_string(req.to)
    message = _escape_applescript_string(req.message)
    script = f"""
tell application "Messages" to activate
delay 0.5
tell application "System Events"
    tell process "Messages"
        keystroke "n" using command down
        delay 0.4
        keystroke "{to}"
        delay 0.4
        key code 36
        delay 0.4
        key code 48
        delay 0.3
        keystroke "{message}"
        delay 0.3
        key code 36
    end tell
end tell
"""
    process = await asyncio.create_subprocess_exec(
        "osascript",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(script.encode("utf-8"))
    if process.returncode != 0:
        details = (stderr or stdout or b"").decode("utf-8", "ignore").strip()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=details or f"osascript failed with exit code {process.returncode}",
        )

    logger.info("Sent message via UI automation to %s", req.to)
    return {"status": "sent", "to": req.to}


@app.post("/send")
async def send_message(req: SendRequest):
    logger.info("Queued outbound message to %s", req.to)
    await _run_store(
        message_store.insert_pending_outbound,
        peer=req.to,
        body=req.message,
    )
    await enqueue_send(req.to, req.message)
    return {"status": "queued", "to": req.to}


# ================================================================
#                  ENTRYPOINT
# ================================================================

if __name__ == "__main__":
    host = os.environ.get("IMESSAGE_HOST", "127.0.0.1")
    port = int(os.environ.get("IMESSAGE_PORT", "8000"))
    logger.info("Starting on http://%s:%s", host, port)
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        log_level=LOG_LEVEL.lower(),
    )
