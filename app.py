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
from typing import Optional, Annotated

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
import httpx
import uvicorn
import time
import re
import asyncio

from imessage_monitor.monitor import iMessageMonitor
from imessage_monitor.outbound import OutboundMessageSender
from imessage_monitor.exceptions import OutboundMessageError

COOLDOWN = 10  # seconds
cooldowns = {}  # {call_session_id: last_timestamp}


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

async def send_worker(outbound: OutboundMessageSender):
    """Single worker that processes SEND_QUEUE sequentially."""
    while True:
        to, message = await SEND_QUEUE.get()
        try:
            await outbound.send_message(to, message)
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

async def forward_incoming_message(message: dict):
    """Forward inbound iMessage data to ngrok endpoint."""
    if message.get("is_from_me"):
        return

    sender = (
        message.get("handle_id_str")
        or message.get("uncanonicalized_id")
        or message.get("chat_identifier")
    )
    if not sender:
        return

    payload = {
        "From": sender,
        "To": message.get("chat_identifier") or "unknown",
        "Body": (
            message.get("message_text")
            or message.get("decoded_attributed_body")
            or ""
        ),
    }

    try:
        await HTTP.post(
            "https://zappd.ngrok.app/sms/reply",
            json=payload,
        )
        print(f"➡️ Forwarded inbound message from {sender}")
    except Exception as e:
        print(f"⚠️ Failed to forward inbound message: {e}")


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
    """Monitor unified log for FaceTime notifications with per-call cooldown."""
    global cooldowns

    process = await asyncio.create_subprocess_shell(
        "log stream --predicate 'eventMessage contains \"FaceTime\"' --info",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Patterns to extract unique call identifiers from log output
    id_patterns = [
        r"call[-_ ]?id[: ]+([0-9a-f\-]+)",
        r"call[-_ ]?uuid[: ]+([0-9a-f\-]+)",
        r"uuid[: ]+([0-9a-f\-]+)",
        r"id[: ]+([0-9a-fx]+)",
        r"session[-_ ]?id[: ]+([0-9a-f\-]+)",
    ]

    while True:
        line = await process.stdout.readline()
        if not line:
            break

        text = line.decode("utf-8", "ignore")

        if "incoming" not in text.lower():
            continue  # ignore non-incoming events

        # Try to extract a unique call/session identifier
        call_id = None
        for pattern in id_patterns:
            match = re.search(pattern, text, re.I)
            if match:
                call_id = match.group(1)
                break

        # If nothing was found, fallback to hashing the log line
        if not call_id:
            call_id = f"fallback-{hash(text)}"

        now = time.time()
        last_event = cooldowns.get(call_id, 0)

        # Apply per-call cooldown
        if now - last_event < COOLDOWN:
            # Same call still inside cooldown window → skip
            continue

        # Mark this call as handled
        cooldowns[call_id] = now

        print(f"📞 Incoming FaceTime (ID={call_id}) → restarting Messages")

        # Restart Messages app
        await run_auto_decline_applescript()
        await restart_messages()


        # Send your automated reply
        await enqueue_send(
            "7345893340",  # your store number
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
    monitor.start(
        message_callback=lambda msg: loop.create_task(
            forward_incoming_message(msg)
        )
    )

    asyncio.create_task(send_worker(outbound))
    asyncio.create_task(watch_for_facetime_notifications())

    print("✅ iMessage monitor started")
    print("🚀 Outbound queue worker running")
    print("📞 FaceTime watcher started")


@app.on_event("shutdown")
async def shutdown_event():
    global monitor
    try:
        if monitor:
            monitor.stop()
    except Exception:
        pass


# ================================================================
#                  API ROUTES
# ================================================================

@app.post("/send")
async def send_message(req: SendRequest):
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