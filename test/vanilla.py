#!/usr/bin/env python3
"""
Minimal iMessage watcher: prints only NEW or EDIT inbound messages.
Run on macOS: python monitor_bare.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from imessage_monitor.monitor import iMessageMonitor

POLL_SECONDS = float(os.environ.get("EDIT_POLL_INTERVAL_SECONDS", "2"))
POLL_LIMIT = int(os.environ.get("EDIT_POLL_LIMIT", "80"))
# Set BARE_QUIET=1 to disable imessage_monitor's chat.db-wal watcher prints
BARE_QUIET = os.environ.get("BARE_QUIET", "").lower() in ("1", "true", "yes")

_seen_guids: set[str] = set()
_known_bodies: dict[str, str] = {}
_running = True


def _body(message: dict) -> str:
    return (
        message.get("message_text")
        or message.get("decoded_attributed_body")
        or ""
    ).strip()


def _guid(message: dict) -> str | None:
    return message.get("message_guid") or message.get("guid")


def _sender(message: dict) -> str:
    return (
        message.get("handle_id_str")
        or message.get("uncanonicalized_id")
        or message.get("chat_identifier")
        or "unknown"
    )


def _seed_recent(monitor: iMessageMonitor) -> None:
    """Mark existing messages seen so we don't print them as NEW on startup."""
    for message in monitor.get_recent_messages(POLL_LIMIT):
        if message.get("is_from_me"):
            continue
        guid = _guid(message)
        if guid:
            _seen_guids.add(guid)
            _known_bodies[guid] = _body(message)


def _print_new(message: dict) -> None:
    guid = _guid(message)
    if not guid or guid in _seen_guids:
        return
    body = _body(message)
    _seen_guids.add(guid)
    _known_bodies[guid] = body
    print(f"[NEW] {_sender(message)} ({guid}): {body}")


def _on_new_message(message: dict) -> None:
    if message.get("is_from_me"):
        return
    _print_new(message)


async def _edit_poll(monitor: iMessageMonitor) -> None:
    global _running
    while _running:
        await asyncio.sleep(POLL_SECONDS)
        try:
            messages = await asyncio.to_thread(
                monitor.get_recent_messages, POLL_LIMIT
            )
        except Exception as e:
            print(f"[error] edit poll: {e}", file=sys.stderr)
            continue

        for message in messages:
            if message.get("is_from_me"):
                continue
            guid = _guid(message)
            if not guid:
                continue

            if guid not in _seen_guids:
                _print_new(message)
                continue

            body = _body(message)
            prev = _known_bodies.get(guid)
            if prev is not None and prev != body:
                print(
                    f"[EDIT] {_sender(message)} ({guid}): {prev!r} -> {body!r}"
                )
            _known_bodies[guid] = body


async def main() -> None:
    if sys.platform != "darwin":
        print("This script must run on macOS.", file=sys.stderr)
        sys.exit(1)

    monitor = iMessageMonitor()
    if BARE_QUIET:
        monitor.config.monitoring.enable_real_time = False
        monitor.config.monitoring.poll_interval_seconds = POLL_SECONDS
    _seed_recent(monitor)

    monitor.start(message_callback=_on_new_message)
    asyncio.create_task(_edit_poll(monitor))

    print("Watching iMessages (NEW / EDIT only). Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        global _running
        _running = False
        monitor.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
