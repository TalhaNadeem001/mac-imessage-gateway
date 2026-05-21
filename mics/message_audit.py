#!/usr/bin/env python3

import os
import sqlite3
import hashlib
import time
import shutil
from typing import Optional, List, Dict

# ============================================================
# CONFIG
# ============================================================

SOURCE_DB = os.path.expanduser("~/Library/Messages/chat.db")
WORKING_DB = "chat.db"
FORWARDED_DB = "forwarded_messages.db"

APPLE_EPOCH_OFFSET = 978307200  # seconds from 1970 → 2001


# ============================================================
# COPY CHAT.DB SAFELY
# ============================================================

def copy_chat_db():
    try:
        shutil.copy2(SOURCE_DB, WORKING_DB)

        for suffix in ["-wal", "-shm"]:
            src = SOURCE_DB + suffix
            if os.path.exists(src):
                shutil.copy2(src, WORKING_DB + suffix)

        print("✅ chat.db copied (with WAL/SHM if present)")

    except Exception as e:
        print(f"❌ Failed to copy chat.db: {e}")


# ============================================================
# TIME CONVERSION
# ============================================================

def apple_time_to_unix(ns: int) -> int:
    if not ns:
        return 0
    return int(ns / 1_000_000_000 + APPLE_EPOCH_OFFSET)


# ============================================================
# INIT FORWARDED DB
# ============================================================

def init_db():
    conn = sqlite3.connect(FORWARDED_DB)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS forwarded_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guid TEXT,
        sender TEXT,
        body TEXT,
        timestamp INTEGER,
        hash TEXT UNIQUE
    )
    """)

    conn.commit()
    conn.close()


# ============================================================
# HASH
# ============================================================

def generate_hash(sender: str, body: str, timestamp: int) -> str:
    base = f"{sender}:{body}:{timestamp}"
    return hashlib.sha1(base.encode()).hexdigest()


# ============================================================
# STORE FORWARDED MESSAGE (CALL THIS FROM YOUR API)
# ============================================================

def store_message(
    sender: str,
    body: str,
    timestamp: Optional[int] = None,
    guid: Optional[str] = None,
):
    if not timestamp:
        timestamp = int(time.time())

    msg_hash = generate_hash(sender, body, timestamp)

    conn = sqlite3.connect(FORWARDED_DB)
    cur = conn.cursor()

    try:
        cur.execute("""
        INSERT INTO forwarded_messages (guid, sender, body, timestamp, hash)
        VALUES (?, ?, ?, ?, ?)
        """, (guid, sender, body.strip(), timestamp, msg_hash))

        conn.commit()

    except sqlite3.IntegrityError:
        # Duplicate — ignore
        pass

    conn.close()


# ============================================================
# LOAD iMESSAGE DB
# ============================================================

def load_imessage_messages(chat_db_path: str) -> List[Dict]:
    conn = sqlite3.connect(chat_db_path)
    cur = conn.cursor()

    query = """
    SELECT
        message.guid,
        handle.id,
        message.text,
        message.attributedBody,
        message.date
    FROM message
    LEFT JOIN handle ON message.handle_id = handle.ROWID
    WHERE message.is_from_me = 0
    """

    rows = cur.execute(query).fetchall()
    conn.close()

    messages = []

    for guid, sender, text, attr_body, date in rows:
        body = text if text else ""

        timestamp = apple_time_to_unix(date)

        messages.append({
            "guid": guid,
            "sender": sender or "unknown",
            "body": body.strip(),
            "timestamp": timestamp,
        })

    return messages


# ============================================================
# LOAD FORWARDED
# ============================================================

def load_forwarded():
    conn = sqlite3.connect(FORWARDED_DB)
    cur = conn.cursor()

    rows = cur.execute("""
    SELECT guid, sender, body, timestamp
    FROM forwarded_messages
    """).fetchall()

    conn.close()

    return [
        {
            "guid": guid,
            "sender": sender,
            "body": body.strip(),
            "timestamp": timestamp,
        }
        for guid, sender, body, timestamp in rows
    ]


# ============================================================
# FIND MISSING (GUID MATCH)
# ============================================================

def find_missing(chat_db_path: str):
    imessages = load_imessage_messages(chat_db_path)
    forwarded = load_forwarded()

    forwarded_guids = set(
        m["guid"] for m in forwarded if m["guid"]
    )

    missing = []

    for msg in imessages:
        if msg["guid"] and msg["guid"] not in forwarded_guids:
            missing.append(msg)

    return missing


# ============================================================
# FUZZY MATCH (BACKUP)
# ============================================================

def find_missing_fuzzy(chat_db_path: str):
    imessages = load_imessage_messages(chat_db_path)
    forwarded = load_forwarded()

    forwarded_hashes = set(
        generate_hash(m["sender"], m["body"], m["timestamp"])
        for m in forwarded
    )

    missing = []

    for msg in imessages:
        h = generate_hash(msg["sender"], msg["body"], msg["timestamp"])
        if h not in forwarded_hashes:
            missing.append(msg)

    return missing


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 Starting message audit...")

    init_db()

    # Give macOS a moment to flush DB
    time.sleep(1)

    copy_chat_db()

    print("📥 Loading iMessage data...")
    missing = find_missing(WORKING_DB)

    print(f"\n❌ Missing (GUID match): {len(missing)}")

    if not missing:
        print("✅ No missing messages (GUID accurate)")
    else:
        print("\n🔍 Running fuzzy fallback check...")
        missing_fuzzy = find_missing_fuzzy(WORKING_DB)

        print(f"❌ Missing (fuzzy): {len(missing_fuzzy)}")

        print("\n--- Sample Missing Messages ---")
        for m in missing_fuzzy[:10]:
            print(m)

    print("\n✅ Audit complete.")