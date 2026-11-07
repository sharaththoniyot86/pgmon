"""Live-configurable AI provider settings -- lets an admin point every AI
feature (health/security/backup/performance reviews, the AI Assistant chat,
the AI Query Optimizer) at Anthropic's cloud API, an Anthropic-compatible
enterprise gateway, or a local/self-hosted OpenAI-compatible endpoint
(vLLM, Ollama, LocalAI, Azure OpenAI, an internal LLM proxy, ...) from the
AI Assistant tab, without redeploying or editing env vars.

Stored in the same local SQLite file as everything else pgmon keeps
locally (settings.USERS_DB_PATH), one row (id=1). The API key is encrypted
at rest (Fernet, key derived from SESSION_SECRET) -- same approach as
registered database passwords in app.dbstore, for the same reason: unlike
login passwords, the plaintext has to be recoverable to actually make API
calls with it.

This is additive, not a replacement for ANTHROPIC_API_KEY/ANTHROPIC_MODEL:
per-field, an unset value here falls back to those env vars (see
get_effective), so a deployment that never touches this UI keeps behaving
exactly as before.
"""
from __future__ import annotations

import asyncio
import sqlite3

from cryptography.fernet import Fernet, InvalidToken

from app import crypto
from app.config import settings

PROVIDERS = ("anthropic", "openai_compatible")
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


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
            CREATE TABLE IF NOT EXISTS ai_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                provider TEXT NOT NULL DEFAULT 'anthropic',
                api_key_encrypted TEXT,
                base_url TEXT,
                model TEXT,
                updated_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


def _fernet() -> Fernet:
    return Fernet(crypto.derive_fernet_key("ai-api-key"))


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Written before per-purpose key derivation existed -- fall back to
        # the old shared derivation rather than losing an already-configured
        # AI key. New encryptions always use the current (HKDF) key.
        return Fernet(crypto.legacy_fernet_key()).decrypt(token.encode("utf-8")).decode("utf-8")


def _get_row_sync() -> sqlite3.Row | None:
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
    finally:
        conn.close()


async def get_public() -> dict:
    """Safe to send to the browser -- never the API key itself, just
    whether one is configured and where it came from."""
    row = await asyncio.to_thread(_get_row_sync)
    stored_key = bool(row and row["api_key_encrypted"])
    return {
        "provider": (row["provider"] if row else None) or "anthropic",
        "base_url": (row["base_url"] if row else None) or "",
        "model": (row["model"] if row else None) or "",
        "has_api_key": stored_key or bool(settings.ANTHROPIC_API_KEY),
        "key_source": "database" if stored_key else ("env" if settings.ANTHROPIC_API_KEY else None),
    }


async def get_effective() -> dict:
    """Resolved config actually used to make AI calls. DB-stored values
    win field-by-field, falling back to the ANTHROPIC_API_KEY/
    ANTHROPIC_MODEL env vars -- so leaving the key blank in the UI while
    changing only the model still uses the env var's key, for the
    'anthropic' provider (there's no env var fallback for an
    openai_compatible key/base_url; those only ever come from here)."""
    row = await asyncio.to_thread(_get_row_sync)
    provider = ((row["provider"] if row else None) or "anthropic")

    api_key = None
    if row and row["api_key_encrypted"]:
        try:
            api_key = await asyncio.to_thread(_decrypt, row["api_key_encrypted"])
        except InvalidToken:
            api_key = None  # SESSION_SECRET changed since this was stored -- treat as unset
    if not api_key and provider == "anthropic":
        api_key = settings.ANTHROPIC_API_KEY or None

    base_url = (row["base_url"] if row else None) or None
    model = (row["model"] if row else None) or None
    if not model:
        model = DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else None

    return {"provider": provider, "api_key": api_key, "base_url": base_url, "model": model}


async def is_enabled() -> bool:
    eff = await get_effective()
    return bool(eff["api_key"]) and (eff["provider"] != "openai_compatible" or bool(eff["model"]))


def _save_sync(provider: str, api_key: str | None, base_url: str | None, model: str | None) -> None:
    conn = _connect()
    try:
        existing = conn.execute("SELECT api_key_encrypted FROM ai_settings WHERE id = 1").fetchone()
        if api_key:
            encrypted = _encrypt(api_key)
        elif existing:
            encrypted = existing["api_key_encrypted"]  # blank key on save means "keep the current one"
        else:
            encrypted = None
        conn.execute(
            """
            INSERT INTO ai_settings (id, provider, api_key_encrypted, base_url, model, updated_at)
            VALUES (1, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                provider = excluded.provider, api_key_encrypted = excluded.api_key_encrypted,
                base_url = excluded.base_url, model = excluded.model, updated_at = excluded.updated_at
            """,
            (provider, encrypted, base_url or None, model or None),
        )
        conn.commit()
    finally:
        conn.close()


async def save(provider: str, api_key: str | None, base_url: str | None, model: str | None) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider!r}")
    await asyncio.to_thread(_save_sync, provider, api_key or None, base_url, model)


def _clear_sync() -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM ai_settings WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


async def clear() -> None:
    """Back to env-var-only (ANTHROPIC_API_KEY/ANTHROPIC_MODEL) behavior."""
    await asyncio.to_thread(_clear_sync)
