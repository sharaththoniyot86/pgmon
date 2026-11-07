"""Entry point WinSW launches as the pgmon Windows service.

The MSI itself only copies files and registers the service (see
pgmon-service.wxs) -- no WiX custom actions, since WiX can't be exercised on
non-Windows to verify custom-action sequencing. Everything install.sh does
interactively at install time on Linux (generate a session secret, write the
config file, create the first admin account) happens here instead, once, on
the service's first start -- idempotent, so every later start is a no-op
past the "config/admin already exist" checks.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(INSTALL_DIR))

CONFIG_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "pgmon"
DATA_DIR = CONFIG_DIR / "data"
ENV_FILE = CONFIG_DIR / "pgmon.env"
INITIAL_PASSWORD_FILE = CONFIG_DIR / "initial-admin-password.txt"
PORT = int(os.environ.get("PGMON_PORT", "8080"))


def ensure_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        session_secret = secrets.token_hex(32)
        users_db_path = str(DATA_DIR / "pgmon_users.db").replace("\\", "/")
        ENV_FILE.write_text(
            "# pgmon runtime configuration -- generated once on first service start.\n"
            "# Changing SESSION_SECRET invalidates existing sessions and makes\n"
            "# previously-encrypted secrets unreadable, so back this file up along\n"
            "# with the data/ folder next to it.\n"
            f"SESSION_SECRET={session_secret}\n"
            f"USERS_DB_PATH={users_db_path}\n"
            "BASE_PATH=\n"
            "DEPLOYMENT_MODE=standalone-windows\n"
            "POLL_INTERVAL_SECONDS=30\n"
            "SNAPSHOT_RETENTION_HOURS=48\n"
            "# ANTHROPIC_API_KEY=\n"
            "# ANTHROPIC_MODEL=claude-sonnet-5\n",
            encoding="utf-8",
        )


def load_config_into_env() -> None:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


async def ensure_admin_account() -> None:
    from app import userdb

    await userdb.init_db()
    if await userdb.user_count() > 0:
        return
    password = secrets.token_urlsafe(18)
    await userdb.create_user("admin", password, role="admin")
    INITIAL_PASSWORD_FILE.write_text(
        "pgmon initial admin account\n"
        "----------------------------\n"
        "username: admin\n"
        f"password: {password}\n\n"
        "This file is only written once, the first time the service starts with\n"
        "no accounts yet. Sign in, change the password (or run\n"
        "pgmon-reset-password.ps1), then delete this file.\n",
        encoding="utf-8",
    )


def main() -> None:
    ensure_config()
    load_config_into_env()
    asyncio.run(ensure_admin_account())

    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
