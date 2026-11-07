"""User accounts, stored in a local SQLite database (separate from the
Postgres instance being monitored) at settings.USERS_DB_PATH.

Password hashing (bcrypt) is CPU-bound and sqlite3 I/O is blocking, so both
run off the event loop via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import bcrypt

from app.config import settings


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode lets concurrent readers proceed without blocking on a writer
    # (the default rollback-journal mode serializes them); busy_timeout is
    # belt-and-suspenders on top of Python's own ~5s default lock wait.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


ROLES = ("admin", "viewer")


def _init_db_sync() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # Added after the original single-admin design -- existing rows get
        # 'admin' via the column default itself (SQLite backfills it), so
        # the original bootstrap account keeps full access with no explicit
        # migration step. 'viewer' is for read-only accounts added later.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


def _create_user_sync(username: str, password: str, role: str) -> None:
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


async def create_user(username: str, password: str, role: str = "admin") -> None:
    """Raises sqlite3.IntegrityError if the username already exists.
    Defaults to 'admin' -- matches the original (pre-role) behavior for the
    CLI (app/create_user.py) and the initial bootstrap account; the web
    Users UI passes an explicit role instead of relying on this default."""
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role!r}")
    await asyncio.to_thread(_create_user_sync, username, password, role)


def _list_users_sync() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT username, role, created_at FROM users ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def list_users() -> list[dict]:
    return await asyncio.to_thread(_list_users_sync)


def _get_role_sync(username: str) -> str | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
        return row["role"] if row else None
    finally:
        conn.close()


async def get_role(username: str) -> str | None:
    return await asyncio.to_thread(_get_role_sync, username)


def _set_role_sync(username: str, role: str) -> bool:
    conn = _connect()
    try:
        if role != "admin":
            remaining = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND username != ?", (username,)
            ).fetchone()[0]
            if remaining == 0:
                raise ValueError("Can't demote the only remaining admin.")
        cur = conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def set_role(username: str, role: str) -> bool:
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role!r}")
    return await asyncio.to_thread(_set_role_sync, username, role)


def _delete_user_sync(username: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return False
        if row["role"] == "admin":
            remaining = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND username != ?", (username,)
            ).fetchone()[0]
            if remaining == 0:
                raise ValueError("Can't delete the only remaining admin.")
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def delete_user(username: str) -> bool:
    return await asyncio.to_thread(_delete_user_sync, username)


def _set_password_sync(username: str, password: str) -> bool:
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def set_password(username: str, password: str) -> bool:
    return await asyncio.to_thread(_set_password_sync, username, password)


def _user_count_sync() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


async def user_count() -> int:
    return await asyncio.to_thread(_user_count_sync)


def _verify_credentials_sync(username: str, password: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        # Still run bcrypt to keep timing roughly constant for unknown usernames.
        bcrypt.checkpw(b"decoy-password", bcrypt.hashpw(b"decoy", bcrypt.gensalt(rounds=12)))
        return False
    return bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8"))


async def verify_credentials(username: str, password: str) -> bool:
    return await asyncio.to_thread(_verify_credentials_sync, username, password)
