#!/usr/bin/env python3
"""
Local test harness for the iMessage gateway.

1. Point FWD_URL in your .env at this server (e.g. http://127.0.0.1:9000/webhook).
2. Run: python test/server.py
3. Open http://127.0.0.1:9000 in a browser.

Shows JSON sent to / received from the gateway and webhook.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "example.env")
load_dotenv(ROOT / "dev.env")
load_dotenv(ROOT / ".env")

IMESSAGE_HOST = os.environ.get("IMESSAGE_HOST", "127.0.0.1")
IMESSAGE_PORT = int(os.environ.get("IMESSAGE_PORT", "8000"))
IMESSAGE_API_KEY = os.environ.get("IMESSAGE_API_KEY", "")
TEST_HOST = os.environ.get("TEST_HOST", "127.0.0.1")
TEST_PORT = int(os.environ.get("TEST_PORT", "9000"))
WEBHOOK_PATH = "/webhook"

GATEWAY_BASE = f"http://{IMESSAGE_HOST}:{IMESSAGE_PORT}"
FWD_URL = os.environ.get(
    "FWD_URL", f"http://{TEST_HOST}:{TEST_PORT}{WEBHOOK_PATH}"
)

app = FastAPI(title="iMessage Gateway Test Harness")
STATIC_DIR = Path(__file__).resolve().parent / "static"
if not STATIC_DIR.is_dir():
    raise RuntimeError(
        f"Missing static directory: {STATIC_DIR} "
        "(expected test/static/index.html)"
    )

_events: deque[dict[str, Any]] = deque(maxlen=200)
_subscribers: list[asyncio.Queue[dict[str, Any]]] = []
_subscribers_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(event: dict[str, Any]) -> dict[str, Any]:
    event = {"id": str(uuid.uuid4()), "at": _now(), **event}
    _events.appendleft(event)
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
    return event


class SendBody(BaseModel):
    to: str = Field(min_length=1)
    message: str = Field(min_length=1)


class WebhookReplyBody(BaseModel):
    action: str | None = None
    messageId: str | None = None
    status: str = "ok"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def config():
    return {
        "gateway": GATEWAY_BASE,
        "fwdUrl": FWD_URL,
        "webhookPath": WEBHOOK_PATH,
        "testServer": f"http://{TEST_HOST}:{TEST_PORT}",
        "hasApiKey": bool(IMESSAGE_API_KEY),
    }


@app.get("/api/log")
async def log():
    return list(_events)


@app.get("/api/events")
async def events(request: Request):
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)

    async with _subscribers_lock:
        _subscribers.append(queue)

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'at': _now()})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping', 'at': _now()})}\n\n"
        finally:
            async with _subscribers_lock:
                if queue in _subscribers:
                    _subscribers.remove(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/api/send")
async def send_to_gateway(body: SendBody):
    url = f"{GATEWAY_BASE}/send"
    payload = {"to": body.to.strip(), "message": body.message.strip()}
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if IMESSAGE_API_KEY:
        headers["Authorization"] = f"Bearer {IMESSAGE_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)
            try:
                response_json = response.json()
            except Exception:
                response_json = {"raw": response.text}

            event = _record(
                {
                    "direction": "outbound",
                    "label": "POST /send → gateway",
                    "request": {"url": url, "headers": _redact_headers(headers), "body": payload},
                    "response": {
                        "status": response.status_code,
                        "body": response_json,
                    },
                    "ok": response.is_success,
                }
            )
            return JSONResponse(
                status_code=response.status_code,
                content={"eventId": event["id"], **event},
            )
    except httpx.RequestError as exc:
        event = _record(
            {
                "direction": "outbound",
                "label": "POST /send → gateway",
                "request": {"url": url, "headers": _redact_headers(headers), "body": payload},
                "response": {"error": str(exc)},
                "ok": False,
            }
        )
        return JSONResponse(
            status_code=502,
            content={"eventId": event["id"], **event},
        )


@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    """Receives inbound forwards from app.py (FWD_URL)."""
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode("utf-8", errors="replace")}

    reply = WebhookReplyBody()
    event = _record(
        {
            "direction": "inbound",
            "label": f"POST {WEBHOOK_PATH} ← gateway",
            "request": {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": payload,
            },
            "response": {"body": reply.model_dump(exclude_none=True)},
            "ok": True,
        }
    )
    return reply.model_dump(exclude_none=True)


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out = dict(headers)
    if "Authorization" in out:
        out["Authorization"] = "Bearer ***"
    return out


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import sys

    print(f"Gateway:  {GATEWAY_BASE}")
    print(f"Webhook:  http://{TEST_HOST}:{TEST_PORT}{WEBHOOK_PATH}")
    print(f"Set FWD_URL=http://{TEST_HOST}:{TEST_PORT}{WEBHOOK_PATH} in .env")
    print(f"UI:       http://{TEST_HOST}:{TEST_PORT}")
    try:
        uvicorn.run(
            app,
            host=TEST_HOST,
            port=TEST_PORT,
            reload=False,
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        )
    except OSError as exc:
        print(f"Failed to bind {TEST_HOST}:{TEST_PORT}: {exc}", file=sys.stderr)
        if getattr(exc, "errno", None) == 48:
            print(
                f"Port {TEST_PORT} is already in use. "
                f"Stop the other process or set TEST_PORT in dev.env.",
                file=sys.stderr,
            )
        raise SystemExit(1) from exc
