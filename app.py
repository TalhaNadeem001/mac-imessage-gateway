#!/usr/bin/env python3
"""
Simple HTTP API to send iMessages using the imessage_monitor OutboundMessageSender.

POST /send
  Headers:
    - Authorization: Bearer <API_KEY>   (or set IMESSAGE_API_KEY env var)
  JSON body:
    { "to": "+1234567890", "message": "Hello!" }

Run:
  IMESSAGE_API_KEY=yourkey python3 app.py
"""

from __future__ import annotations
import os
import asyncio
import subprocess
from typing import Optional, Annotated

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
import uvicorn
import httpx

from imessage_monitor.monitor import iMessageMonitor
from imessage_monitor.outbound import OutboundMessageSender
from imessage_monitor.exceptions import OutboundMessageError

# ---------------------------
# Pydantic Models
# ---------------------------

NonEmptyStr = Annotated[str, Field(min_length=1)]

class SendRequest(BaseModel):
    to: NonEmptyStr
    message: Annotated[str, Field(min_length=1, max_length=10000)]

    @model_validator(mode="before")
    def strip_whitespace(cls, values):
        to = values.get("to")
        msg = values.get("message")
        if isinstance(to, str):
            values["to"] = to.strip()
        if isinstance(msg, str):
            values["message"] = msg.strip()
        return values

# ---------------------------
# Globals
# ---------------------------

app = FastAPI(title="iMessage HTTP API", version="0.1.0")
monitor: Optional[iMessageMonitor] = None
outbound: Optional[OutboundMessageSender] = None

API_KEY = os.environ.get("IMESSAGE_API_KEY", "changeme")

APPLE_SCRIPT = '''
set appName to "FaceTime"

tell application appName
    if it is running then quit
    delay 1
end tell

do shell script "killall " & quoted form of appName & " || true"
'''

# ---------------------------
# API Key Dependency
# ---------------------------

async def require_api_key(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Missing or invalid Authorization header")
    key = auth.split(" ", 1)[1].strip()
    if key != API_KEY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Invalid API key")
    return True

# ---------------------------
# iMessage Utilities
# ---------------------------

async def forward_incoming_message(message: dict):
    if message.get("is_from_me"):
        return

    sender = message.get("handle_id_str") or message.get("uncanonicalized_id") or message.get("chat_identifier")
    body = message.get("message_text") or message.get("decoded_attributed_body") or ""
    to_number = message.get("chat_identifier") or message.get("account_login") or "unknown"

    if not sender:
        return

    payload = {"From": sender, "To": to_number, "Body": body}

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                os.environ.get("NGROK_URL"),
                json=payload,
                timeout=10.0
            )
    except Exception as e:
        print(f"⚠ Failed to forward message from {sender}: {e}")

async def restart_messages():
    try:
        subprocess.run(["osascript", "-e", APPLE_SCRIPT], check=True)
    except subprocess.CalledProcessError as e:
        print(f"AppleScript error: {e}")

async def watch_for_facetime_notifications():
    global outbound
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
        if "Incoming call" in text or "incoming" in text.lower():
            print(text)
            print("📞 FaceTime notification detected, restarting Messages…")
            await restart_messages()
            
            message = "Corn On The Corner, This is our storefront location: 1041 Howard St, Dearborn, MI 48124. Please text your order including a name and confirm the given pick up time. Thank you."
            if outbound:
                try:
                    coro = outbound.send_message("7345893340", message)
                    if asyncio.iscoroutine(coro):
                        await coro
                except OutboundMessageError as exc:
                    print(f"Failed to send message: {exc}")
                except Exception as exc:
                    print(f"Unexpected error sending message: {exc}")

# ---------------------------
# Startup & Shutdown
# ---------------------------

@app.on_event("startup")
async def startup_event():
    global monitor, outbound
    monitor = iMessageMonitor()
    outbound = OutboundMessageSender(monitor.config)

    loop = asyncio.get_event_loop()
    monitor.start(message_callback=lambda msg: loop.create_task(forward_incoming_message(msg)))
    print("✅ iMessage monitor started")

    asyncio.create_task(watch_for_facetime_notifications())
    print("📞 FaceTime auto-decline running")

@app.on_event("shutdown")
async def shutdown_event():
    global monitor
    if monitor:
        try:
            monitor.stop()
        except Exception:
            pass

# ---------------------------
# API Endpoints
# ---------------------------

@app.post("/send")
async def send_message(req: SendRequest):
    global outbound
    if not outbound:
        raise HTTPException(status_code=500, detail="Outbound sender not initialized")

    recipient = req.to
    text = req.message

    try:
        coro = outbound.send_message(recipient, text)
        if asyncio.iscoroutine(coro):
            await coro
    except OutboundMessageError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to send message: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error sending message: {exc}")

    return {"status": "ok", "to": recipient}

# ---------------------------
# Main
# ---------------------------

if __name__ == "__main__":
    host = os.environ.get("IMESSAGE_HOST", "127.0.0.1")
    port = int(os.environ.get("IMESSAGE_PORT", "8000"))
    print(f"Starting iMessage API on http://{host}:{port}")
    uvicorn.run("app:app", host=host, port=port, log_level="info")
