"""Registry of additional PostgreSQL instances/databases to monitor, stored
in the same local SQLite file as user accounts (settings.USERS_DB_PATH).

Connection passwords are encrypted at rest (Fernet, key derived from
SESSION_SECRET) since -- unlike login passwords -- we need to recover the
plaintext to actually open connections.

This is the sole source of truth for what pgmon monitors -- there is no
built-in/default database.
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from app import crypto
from app.config import settings


@dataclass
class DatabaseRecord:
    id: str
    name: str
    host: str
    port: int
    dbname: str
    username: str
    password: str
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
            CREATE TABLE IF NOT EXISTS databases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                dbname TEXT NOT NULL,
                username TEXT NOT NULL,
                password_encrypted TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


def _fernet() -> Fernet:
    return Fernet(crypto.derive_fernet_key("db-credentials"))


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Written before per-purpose key derivation existed -- fall back to
        # the old shared derivation rather than treating every
        # already-registered database as gone. New encryptions always use
        # the current (HKDF, domain-separated) key.
        return Fernet(crypto.legacy_fernet_key()).decrypt(token.encode("utf-8")).decode("utf-8")


def _row_to_public_dict(row: sqlite3.Row) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "host": row["host"],
        "port": row["port"],
        "dbname": row["dbname"],
        "username": row["username"],
        "is_default": False,
        "created_at": row["created_at"],
    }


def _add_database_sync(name: str, host: str, port: int, dbname: str, username: str, password: str) -> dict:
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO databases (name, host, port, dbname, username, password_encrypted, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, host, port, dbname, username, _encrypt(password), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM databases WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_public_dict(row)
    finally:
        conn.close()


async def add_database(name: str, host: str, port: int, dbname: str, username: str, password: str) -> dict:
    """Raises sqlite3.IntegrityError if `name` is already taken."""
    return await asyncio.to_thread(_add_database_sync, name, host, port, dbname, username, password)


def _list_databases_sync() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM databases ORDER BY created_at ASC").fetchall()
        return [_row_to_public_dict(r) for r in rows]
    finally:
        conn.close()


async def list_databases() -> list[dict]:
    return await asyncio.to_thread(_list_databases_sync)


def _get_database_sync(db_id: str) -> DatabaseRecord | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM databases WHERE id = ?", (db_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        password = _decrypt(row["password_encrypted"])
    except InvalidToken:
        # SESSION_SECRET changed since this was stored -- surface as "gone"
        # rather than crashing the caller.
        return None
    return DatabaseRecord(
        id=str(row["id"]), name=row["name"], host=row["host"], port=row["port"],
        dbname=row["dbname"], username=row["username"], password=password,
        created_at=row["created_at"],
    )


async def get_database(db_id: str) -> DatabaseRecord | None:
    return await asyncio.to_thread(_get_database_sync, db_id)


def _delete_database_sync(db_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM databases WHERE id = ?", (db_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def delete_database(db_id: str) -> bool:
    return await asyncio.to_thread(_delete_database_sync, db_id)
