"""Recurring backup schedules, configured from the Backup & Recovery tab
instead of an external cron -- so a schedule (type, frequency, retention)
lives next to the backups it produces and pgmon's own restore/download/
verification UI, rather than in a Kubernetes CronJob the app can't see.

Stored in the same local SQLite file as user accounts/registered
databases (settings.USERS_DB_PATH); the actual run loop is app/scheduler.py.
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings

FREQUENCIES_HOURS = {"hourly": 1, "daily": 24, "weekly": 24 * 7}


@dataclass
class ScheduleRecord:
    id: int
    db_id: str
    backup_type: str
    frequency: str
    retention_days: int
    enabled: bool
    last_run_at: str | None
    last_status: str | None
    created_at: str


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
            CREATE TABLE IF NOT EXISTS backup_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                db_id TEXT NOT NULL,
                backup_type TEXT NOT NULL,
                frequency TEXT NOT NULL,
                retention_days INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                last_status TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


def _row_to_record(row: sqlite3.Row) -> ScheduleRecord:
    return ScheduleRecord(
        id=row["id"], db_id=row["db_id"], backup_type=row["backup_type"], frequency=row["frequency"],
        retention_days=row["retention_days"], enabled=bool(row["enabled"]),
        last_run_at=row["last_run_at"], last_status=row["last_status"], created_at=row["created_at"],
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    r = _row_to_record(row)
    return {
        "id": r.id, "db_id": r.db_id, "backup_type": r.backup_type, "frequency": r.frequency,
        "retention_days": r.retention_days, "enabled": r.enabled,
        "last_run_at": r.last_run_at, "last_status": r.last_status, "created_at": r.created_at,
    }


def _create_sync(db_id: str, backup_type: str, frequency: str, retention_days: int) -> dict:
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO backup_schedules (db_id, backup_type, frequency, retention_days, enabled, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (db_id, backup_type, frequency, retention_days, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM backup_schedules WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


async def create_schedule(db_id: str, backup_type: str, frequency: str, retention_days: int) -> dict:
    if backup_type not in ("logical", "physical", "incremental"):
        raise ValueError(f"Unknown backup type: {backup_type!r}")
    if frequency not in FREQUENCIES_HOURS:
        raise ValueError(f"Unknown frequency: {frequency!r}")
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1.")
    return await asyncio.to_thread(_create_sync, db_id, backup_type, frequency, retention_days)


def _list_sync(db_id: str | None) -> list[dict]:
    conn = _connect()
    try:
        if db_id:
            rows = conn.execute(
                "SELECT * FROM backup_schedules WHERE db_id = ? ORDER BY created_at ASC", (db_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM backup_schedules ORDER BY created_at ASC").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


async def list_schedules(db_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(_list_sync, db_id)


def _list_all_records_sync() -> list[ScheduleRecord]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM backup_schedules WHERE enabled = 1").fetchall()
        return [_row_to_record(r) for r in rows]
    finally:
        conn.close()


async def list_enabled_records() -> list[ScheduleRecord]:
    """Used by the scheduler loop -- typed records, not the API's dict shape."""
    return await asyncio.to_thread(_list_all_records_sync)


def _set_enabled_sync(schedule_id: int, enabled: bool) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("UPDATE backup_schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, schedule_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def set_enabled(schedule_id: int, enabled: bool) -> bool:
    return await asyncio.to_thread(_set_enabled_sync, schedule_id, enabled)


def _record_run_sync(schedule_id: int, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE backup_schedules SET last_run_at = ?, last_status = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), status, schedule_id),
        )
        conn.commit()
    finally:
        conn.close()


async def record_run(schedule_id: int, status: str) -> None:
    await asyncio.to_thread(_record_run_sync, schedule_id, status)


def _delete_sync(schedule_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM backup_schedules WHERE id = ?", (schedule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def delete_schedule(schedule_id: int) -> bool:
    return await asyncio.to_thread(_delete_sync, schedule_id)
