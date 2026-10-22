"""Session auth via a signed, timed cookie. Credentials themselves are
looked up in the SQLite user store (app/userdb.py)."""
from __future__ import annotations

from fastapi import Cookie, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import crypto, userdb
from app.config import settings

COOKIE_NAME = "pgmon_session"


def _serializer() -> URLSafeTimedSerializer:
    # HKDF-derived, not the raw SESSION_SECRET: domain-separated from the
    # key that encrypts stored DB/AI credentials (app/crypto.py) so a
    # weakness specific to one doesn't hand over the other. This does
    # invalidate every existing session the moment it ships -- an expected,
    # one-time side effect, same as any SESSION_SECRET rotation.
    return URLSafeTimedSerializer(crypto.derive_key("session"), salt="pgmon-session")


async def verify_credentials(username: str, password: str) -> bool:
    return await userdb.verify_credentials(username, password)


def create_session_token(username: str) -> str:
    return _serializer().dumps({"user": username})


def read_session_token(token: str) -> str | None:
    try:
        data = _serializer().loads(token, max_age=settings.SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("user")


async def require_user(pgmon_session: str | None = Cookie(default=None)) -> str:
    if pgmon_session:
        user = read_session_token(pgmon_session)
        if user:
            return user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


async def require_admin(pgmon_session: str | None = Cookie(default=None)) -> str:
    """Same session check as require_user, plus role='admin'. Used on every
    endpoint that mutates Postgres, Kubernetes, or pgmon's own persisted
    state; 'viewer' accounts get 401 same as an unauthenticated request
    would from require_user, then 403 specifically for being the wrong
    role, so the UI can tell "log in" from "you don't have permission" apart."""
    user = await require_user(pgmon_session)
    if await userdb.get_role(user) != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return user
