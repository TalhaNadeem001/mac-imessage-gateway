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

from imessage_monitor.monitor import iMessageMonitor
from imessage_monitor.outbound import OutboundMessageSender
from imessage_monitor.exceptions import OutboundMessageError


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
            "https://c4272991e20e.ngrok-free.app/sms/reply",
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


async def watch_for_facetime_notifications():
    """Monitor unified log for FaceTime notifications."""
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
        if "incoming" in text.lower():
            print("📞 FaceTime incoming → restarting Messages")
            await restart_messages()

            # Automated response
            await enqueue_send(
                "7345893340",
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