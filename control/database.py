"""
control/database.py
Phase 5B — SQLite storage layer for AVA.

Replaces flat JSON files with a thread-safe SQLite database.
Auto-migrates ava_memory.json on first run if it exists.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ava.database")

DB_PATH = os.getenv("DB_PATH", "/home/manoj/ava-data/ava.db")

# Thread-local storage so each thread gets its own connection
_local = threading.local()

_DDL = """
CREATE TABLE IF NOT EXISTS query_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    query        TEXT NOT NULL,
    response     TEXT NOT NULL,
    confidence   TEXT,
    intent       TEXT,
    sources_used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ava_memory (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    command     TEXT NOT NULL,
    risk_level  TEXT NOT NULL,
    status      TEXT DEFAULT 'pending',
    approved_by TEXT,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details    TEXT NOT NULL,
    user       TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection, creating it if needed."""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    """Create tables and migrate legacy JSON files on first run."""
    conn = _get_conn()
    conn.executescript(_DDL)
    conn.commit()
    logger.info(f"[DB] SQLite initialised at {DB_PATH}")

    _migrate_memory_json()


# ─── query_history ────────────────────────────────────────────────────────────

def save_query(
    query: str,
    response: str,
    confidence: str = None,
    intent: str = None,
    sources_used: int = 0,
) -> int:
    """Insert a query/response pair and return its new row id."""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO query_history
               (timestamp, query, response, confidence, intent, sources_used)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), query, response,
         confidence, intent, sources_used),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_queries(n: int = 5) -> list[dict]:
    """Return the n most recent query/response rows, oldest first."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, timestamp, query, response, confidence, intent, sources_used
           FROM query_history
           ORDER BY id DESC
           LIMIT ?""",
        (n,),
    ).fetchall()
    # Reverse so oldest is first (natural conversation order)
    return [dict(r) for r in reversed(rows)]


# ─── ava_memory ───────────────────────────────────────────────────────────────

def save_memory(key: str, value) -> None:
    """Upsert a memory key (value is JSON-serialised automatically)."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO ava_memory (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE
               SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, json.dumps(value), datetime.utcnow().isoformat()),
    )
    conn.commit()


def get_memory(key: str, default=None):
    """Retrieve a memory value (JSON-deserialised) or *default* if missing."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM ava_memory WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def get_all_memory() -> dict:
    """Return all memory as {key: value} dict."""
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM ava_memory").fetchall()
    result = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except Exception:
            result[r["key"]] = r["value"]
    return result


# ─── approval_queue ───────────────────────────────────────────────────────────

def add_approval(command: str, risk_level: str) -> int:
    """Queue a command for human approval. Returns row id."""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO approval_queue (timestamp, command, risk_level)
           VALUES (?, ?, ?)""",
        (datetime.utcnow().isoformat(), command, risk_level),
    )
    conn.commit()
    return cur.lastrowid


def get_pending_approvals() -> list[dict]:
    """Return all approval_queue rows with status='pending'."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, timestamp, command, risk_level, status
           FROM approval_queue
           WHERE status='pending'
           ORDER BY id ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


# ─── audit_log ────────────────────────────────────────────────────────────────

def add_audit(event_type: str, details: str, user: str = None) -> None:
    """Append an audit log entry."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO audit_log (timestamp, event_type, details, user)
           VALUES (?, ?, ?, ?)""",
        (datetime.utcnow().isoformat(), event_type, details, user),
    )
    conn.commit()


# ─── Migration ────────────────────────────────────────────────────────────────

def _migrate_memory_json() -> None:
    """One-time import of ava_memory.json into SQLite ava_memory table."""
    memory_json = os.getenv("MEMORY_PATH", "/home/manoj/ava-data/ava_memory.json")
    if not Path(memory_json).exists():
        return

    conn = _get_conn()
    already = conn.execute("SELECT COUNT(*) FROM ava_memory").fetchone()[0]
    if already > 0:
        logger.info("[DB] ava_memory already populated — skipping JSON migration")
        return

    try:
        with open(memory_json) as f:
            data: dict = json.load(f)
        for key, value in data.items():
            conn.execute(
                """INSERT OR IGNORE INTO ava_memory (key, value, updated_at)
                   VALUES (?, ?, ?)""",
                (key, json.dumps(value), datetime.utcnow().isoformat()),
            )
        conn.commit()
        logger.info(f"[DB] Migrated {len(data)} keys from {memory_json}")
    except Exception as e:
        logger.warning(f"[DB] Memory JSON migration failed: {e}")
