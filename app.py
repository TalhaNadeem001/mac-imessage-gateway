#!/usr/bin/env python3
"""
Optimized async HTTP API for sending iMessages via imessage_monitor.
Includes:
 - Scalable single-producer queue for outbound messages
 - Unified async send worker
 - Shared httpx client
 - Simplified inbound forwarding
 - Clean FaceTime watcher & restart logic
"""

from __future__ import annotations

import os
import asyncio
import subprocess
import shutil
from typing import Optional, Annotated
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
import httpx
import uvicorn
import time
import re
import asyncio
import hashlib

from imessage_monitor.monitor import iMessageMonitor
from imessage_monitor.outbound import OutboundMessageSender
from imessage_monitor.exceptions import OutboundMessageError

from dotenv import load_dotenv
load_dotenv()


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
FWD_URL = os.environ.get("FWD_URL", "https://zappd.app/sms/reply")
INBOUND_BATCH_DELAY_SECONDS = float(
    os.environ.get("INBOUND_BATCH_DELAY_SECONDS", "0")
)

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
    # AppleScript strings are double-quoted; escape backslashes + quotes.
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def send_message_via_osascript(to: str, message: str) -> None:
    """
    Send an iMessage via the macOS Messages.app using osascript.

    Equivalent to:
      osascript <<EOF
      tell application "Messages"
          send "hello" to buddy "+123..."
      end tell
      EOF
    """
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
    """Single worker that processes SEND_QUEUE sequentially."""
    while True:
        to, message = await SEND_QUEUE.get()
        try:
            await send_message_via_osascript(to, message)
            print(f"📤 Sent message to {to}")
        except OutboundMessageError as exc:
            print(f"❌ OutboundMessageError sending to {to}: {exc}")
        except Exception as exc:
            print(f"❌ Unexpected send error to {to}: {exc}")
        finally:
            SEND_QUEUE.task_done()


async def enqueue_send(to: str, message: str):
    """Public helper to submit outgoing messages."""
    await SEND_QUEUE.put((to, message))


# ================================================================
#                  INBOUND FORWARDING
# ================================================================

def _message_body(message: dict) -> str:
    return (
        message.get("message_text")
        or message.get("decoded_attributed_body")
        or ""
    )


def _sender_key(message: dict) -> Optional[str]:
    return (
        message.get("handle_id_str")
        or message.get("uncanonicalized_id")
        or message.get("chat_identifier")
    )


@dataclass
class _SenderBatch:
    messages: list[dict] = field(default_factory=list)
    flush_task: Optional[asyncio.Task] = None


_inbound_batches: dict[str, _SenderBatch] = {}
_inbound_batches_lock = asyncio.Lock()


async def _post_forward_payload(payload: dict, sender: str, audit_messages: list[dict]) -> None:
    await HTTP.post(FWD_URL, json=payload)
    print(f"➡️ Forwarded inbound message from {sender}")
    from message_audit import store_message

    for message in audit_messages:
        store_message(
            sender=sender,
            body=_message_body(message),
            guid=message.get("guid"),
        )


async def _forward_messages_now(messages: list[dict], sender: str) -> None:
    if not messages:
        return

    last = messages[-1]
    bodies = [_message_body(m) for m in messages]
    combined_body = "\n".join(b for b in bodies if b)

    payload = {
        "From": sender,
        "To": last.get("chat_identifier") or "unknown",
        "Body": combined_body,
        "userId": USER_ID,
    }

    try:
        await _post_forward_payload(payload, sender, messages)
    except Exception as e:
        print(f"⚠️ Failed to forward inbound message: {e}")


async def _flush_sender_batch_after_delay(sender: str) -> None:
    try:
        await asyncio.sleep(INBOUND_BATCH_DELAY_SECONDS)
    except asyncio.CancelledError:
        return

    async with _inbound_batches_lock:
        batch = _inbound_batches.pop(sender, None)
    if batch and batch.messages:
        await _forward_messages_now(batch.messages, sender)


async def _enqueue_inbound_batch(message: dict, sender: str) -> None:
    async with _inbound_batches_lock:
        batch = _inbound_batches.setdefault(sender, _SenderBatch())
        batch.messages.append(message)
        if batch.flush_task and not batch.flush_task.done():
            batch.flush_task.cancel()
        batch.flush_task = asyncio.create_task(
            _flush_sender_batch_after_delay(sender)
        )


async def flush_all_inbound_batches() -> None:
    async with _inbound_batches_lock:
        pending = dict(_inbound_batches)
        _inbound_batches.clear()

    for sender, batch in pending.items():
        if batch.flush_task and not batch.flush_task.done():
            batch.flush_task.cancel()
        if batch.messages:
            await _forward_messages_now(batch.messages, sender)


async def forward_incoming_message(message: dict):
    """Forward inbound iMessage data to ngrok endpoint."""
    if message.get("is_from_me"):
        return

    sender = _sender_key(message)
    if not sender:
        return

    if INBOUND_BATCH_DELAY_SECONDS <= 0:
        await _forward_messages_now([message], sender)
        return

    await _enqueue_inbound_batch(message, sender)


# ================================================================
#                  FACETIME WATCH / AUTO-RESTART
# ================================================================

async def restart_messages():
    try:
        subprocess.run(["osascript", "-e", APPLE_SCRIPT], check=True)
        print("🔄 Messages app restarted")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ AppleScript error: {e}")

async def run_auto_decline_applescript():
    process = await asyncio.create_subprocess_exec(
        "osascript", "-e", APPLE_DECLINE_ONLY,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if stdout:
        print("📟 AppleScript output:", stdout.decode())
    if stderr:
        print("⚠️ AppleScript error:", stderr.decode())

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

        # ---- Unified UUID extraction ----
        match = re.search(UUID_REGEX, text)
        if match:
            call_id = match.group(0).replace("-", "")
        else:
            # Stable fallback
            call_id = "fallback-" + hashlib.sha1(text.encode()).hexdigest()[:12]

        now = time.time()

        # ---- Global debounce ----
        if now - last_global < GLOBAL_DEBOUNCE:
            continue
        last_global = now

        # ---- Per-call cooldown ----
        last_event = cooldowns.get(call_id, 0)
        if now - last_event < COOLDOWN:
            print("⚠️ Duplicate prevented (cooldown):", call_id)
            continue

        cooldowns[call_id] = now

        print(f"📞 Incoming FaceTime (ID={call_id}) → restarting Messages")

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

    loop = asyncio.get_event_loop()

    monitor = iMessageMonitor()
    outbound = OutboundMessageSender(monitor.config)

    # Register inbound callback
    # Monitor is always checking the OS to see if a message is recieved
    # If recieved will send to application
    monitor.start(
        message_callback=lambda msg: loop.create_task(
            forward_incoming_message(msg)
        )
    )

    asyncio.create_task(send_worker(outbound))

    print("✅ iMessage monitor started")
    print("🚀 Outbound queue worker running")
    print("📞 FaceTime watcher started")
    if INBOUND_BATCH_DELAY_SECONDS > 0:
        print(
            f"📥 Inbound batch delay: {INBOUND_BATCH_DELAY_SECONDS}s per sender"
        )


@app.on_event("shutdown")
async def shutdown_event():
    global monitor
    await flush_all_inbound_batches()
    try:
        if monitor:
            monitor.stop()
    except Exception:
        pass


# ================================================================
#                  API ROUTES
# ================================================================

@app.get("/")
async def home():
    return {
        "status": "ok",
        "service": "zappd_node",
        "uptime": "running",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.post("/send")
async def send_message(req: SendRequest):
    # Recieves message from application and puts it in a queue
    # macOS is responsible for sending messages
    await enqueue_send(req.to, req.message)
    return {"status": "queued", "to": req.to}


# ================================================================
#                  ENTRYPOINT
# ================================================================

if __name__ == "__main__":
    host = os.environ.get("IMESSAGE_HOST", "127.0.0.1")
    port = int(os.environ.get("IMESSAGE_PORT", "8000"))
    print(f"Starting on http://{host}:{port}")
    uvicorn.run("app:app", host=host, port=port, log_level="info")
    
