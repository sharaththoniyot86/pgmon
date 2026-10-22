"""Audit trail of admin actions taken through pgmon itself -- who ran
VACUUM, killed a session, edited a setting, restored a backup, promoted a
replica, managed roles/users, etc. Separate from Postgres's own pgaudit
log (which covers SQL statements on the monitored server, not actions
taken through this app's UI, and isn't surfaced here at all).

Stored in the same local SQLite file as user accounts/registered
databases (settings.USERS_DB_PATH). Logging is best-effort: a failure to
write an audit row must never block or fail the action it's describing,
so log() swallows its own exceptions.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger("pgmon.audit")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode lets concurrent readers proceed without blocking on a writer
    # (the default rollback-journal mode serializes them); busy_timeout is
    # belt-and-suspenders on top of Python's own ~5s default lock wait.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db_sync() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                db_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS audit_log_created_at_idx ON audit_log (created_at)")
        conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


def _log_sync(username: str, action: str, detail: str | None, db_id: str | None) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO audit_log (username, action, detail, db_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, action, detail, db_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


async def log(username: str, action: str, detail: str | None = None, db_id: str | None = None) -> None:
    try:
        await asyncio.to_thread(_log_sync, username, action, detail, db_id)
    except Exception:
        logger.exception("failed to write audit log entry: user=%s action=%s", username, action)


def _list_sync(limit: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT username, action, detail, db_id, created_at FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def list_entries(limit: int = 200) -> list[dict]:
    return await asyncio.to_thread(_list_sync, limit)
