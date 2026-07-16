"""Local SQLite log for iMessage relay — stages /send and mirrors chat.db traffic."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("imessage_api.message_store")

_DB_PATH: Optional[Path] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  guid         TEXT UNIQUE,
  body         TEXT NOT NULL,
  peer         TEXT NOT NULL,
  sender       TEXT NOT NULL,
  is_from_me   INTEGER NOT NULL,
  status       TEXT NOT NULL,
  forwarded    INTEGER NOT NULL DEFAULT 0,
  is_edit      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_pending
  ON messages(status) WHERE guid IS NULL;
"""


def default_db_path() -> Path:
    return Path(__file__).resolve().parent / "messages.db"


def init_db(path: Optional[str | Path] = None) -> Path:
    global _DB_PATH
    resolved = Path(path) if path else default_db_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _DB_PATH = resolved
    logger.info("Message store initialized at %s", resolved)
    return resolved


def _conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        init_db()
    assert _DB_PATH is not None
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_body(body: str) -> str:
    if not body:
        return ""
    # print(f"Before normalization: {body}")
    cleaned = body.replace("\x00", "")
    cleaned = cleaned.strip("\ufffd").strip()
    # print(f"After normalization: {cleaned}")
    return cleaned


normalize_body = _normalize_body


def _peer_digits(peer: str) -> str:
    return re.sub(r"\D", "", peer or "")


def peers_match(a: str, b: str) -> bool:
    da, db = _peer_digits(a), _peer_digits(b)
    if not da or not db:
        return (a or "").strip().lower() == (b or "").strip().lower()
    if da == db:
        return True
    if len(da) >= 10 and len(db) >= 10:
        return da[-10:] == db[-10:]
    return False


def insert_pending_outbound(*, peer: str, body: str) -> int:
    """Stage POST /send before chat.db assigns a GUID."""
    normalized_body = _normalize_body(body)
    now = _now_iso()
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO messages (
              guid, body, peer, sender, is_from_me, status, forwarded, created_at, updated_at
            ) VALUES (NULL, ?, ?, 'cashier', 1, 'pending', 0, ?, ?)
            """,
            (normalized_body, peer.strip(), now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    except Exception as exc:
        logger.warning("insert_pending_outbound failed: %s", exc)
        raise
    finally:
        conn.close()


def find_pending_outbound(
    *,
    peer: str,
    body: str,
    max_age_seconds: int = 300,
) -> Optional[dict[str, Any]]:
    normalized_body = _normalize_body(body)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")

    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE guid IS NULL
              AND status = 'pending'
              AND sender = 'cashier'
              AND body = ?
              AND created_at >= ?
            ORDER BY created_at DESC
            """,
            (normalized_body, cutoff),
        ).fetchall()
        for row in rows:
            if peers_match(row["peer"], peer):
                return dict(row)

        fallback_rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE guid IS NULL
              AND status = 'pending'
              AND sender = 'cashier'
              AND created_at >= ?
            ORDER BY created_at DESC
            """,
            (cutoff,),
        ).fetchall()
        peer_matches = [dict(r) for r in fallback_rows if peers_match(r["peer"], peer)]
        if not peer_matches:
            return None

        for row in peer_matches:
            if _normalize_body(row["body"]) == normalized_body:
                return row

        if len(peer_matches) == 1:
            return peer_matches[0]

        return None
    except Exception as exc:
        logger.warning("find_pending_outbound failed: %s", exc)
        return None
    finally:
        conn.close()


def attach_guid(row_id: int, guid: str) -> None:
    guid = guid.strip().lower()
    now = _now_iso()
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE messages
            SET guid = ?, status = 'matched', updated_at = ?
            WHERE id = ? AND guid IS NULL
            """,
            (guid, now, row_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.execute(
            """
            UPDATE messages
            SET status = 'matched', updated_at = ?
            WHERE id = ?
            """,
            (now, row_id),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("attach_guid failed id=%s guid=%s: %s", row_id, guid, exc)
    finally:
        conn.close()


def upsert_mirror(
    *,
    guid: str,
    body: str,
    peer: str,
    sender: str,
    is_from_me: bool,
    status: str = "synced",
) -> None:
    guid = guid.strip().lower()
    normalized_body = _normalize_body(body)
    now = _now_iso()
    conn = _conn()
    try:
        existing = conn.execute(
            "SELECT id, body, forwarded FROM messages WHERE guid = ?", (guid,)
        ).fetchone()
        if existing:
            is_edit = 1 if existing["body"] != normalized_body else 0
            conn.execute(
                """
                UPDATE messages
                SET body = ?, peer = ?, sender = ?, is_from_me = ?,
                    status = ?, is_edit = ?, updated_at = ?
                WHERE guid = ?
                """,
                (
                    normalized_body,
                    peer.strip(),
                    sender,
                    1 if is_from_me else 0,
                    status,
                    is_edit,
                    now,
                    guid,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO messages (
                  guid, body, peer, sender, is_from_me, status, forwarded,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    guid,
                    normalized_body,
                    peer.strip(),
                    sender,
                    1 if is_from_me else 0,
                    status,
                    now,
                    now,
                ),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("upsert_mirror failed guid=%s: %s", guid, exc)
    finally:
        conn.close()


def is_forwarded(guid: str) -> bool:
    guid = guid.strip().lower()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT forwarded FROM messages WHERE guid = ?", (guid,)
        ).fetchone()
        return bool(row and row["forwarded"])
    except Exception as exc:
        logger.warning("is_forwarded failed guid=%s: %s", guid, exc)
        return False
    finally:
        conn.close()


def should_skip_outbound_forward(guid: str) -> bool:
    """
    True when this outbound message should not be POSTed to ORDERFLOW.
    Covers rows already forwarded, or staged via POST /send (pending/matched).
    """
    guid = guid.strip().lower()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT forwarded, sender, status FROM messages WHERE guid = ?",
            (guid,),
        ).fetchone()
        if not row:
            return False
        if row["forwarded"]:
            return True
        if row["sender"] == "cashier" and row["status"] in ("pending", "matched"):
            return True
        return False
    except Exception as exc:
        logger.warning("should_skip_outbound_forward failed guid=%s: %s", guid, exc)
        return False
    finally:
        conn.close()


def mark_synced(guid: str) -> None:
    guid = guid.strip().lower()
    now = _now_iso()
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE messages
            SET forwarded = 1, status = 'synced', updated_at = ?
            WHERE guid = ?
            """,
            (now, guid),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("mark_synced failed guid=%s: %s", guid, exc)
    finally:
        conn.close()


def mark_failed(guid: str) -> None:
    guid = guid.strip().lower()
    now = _now_iso()
    conn = _conn()
    try:
        conn.execute(
            """
            UPDATE messages
            SET status = 'failed', updated_at = ?
            WHERE guid = ?
            """,
            (now, guid),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("mark_failed guid=%s: %s", guid, exc)
    finally:
        conn.close()
