"""CLI to create or update a login account in the SQLite users DB.

Usage (run inside the app's container, e.g. via `kubectl exec`, or in the
app's venv on a standalone install -- see packaging/linux/pgmon-reset-password):
    python -m app.create_user <username> <password>              # create (admin by default)
    python -m app.create_user <username> <password> --role viewer  # create as viewer
    python -m app.create_user <username> <password> --update      # change password

Passing '-' as <password> reads it from stdin instead (one line, trailing
newline stripped) -- use this rather than a literal argv password wherever
the password shouldn't sit in `ps` output for the duration of the call.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys

from app import userdb


async def main() -> None:
    argv = sys.argv[1:]
    update = "--update" in argv
    role = "admin"
    if "--role" in argv:
        i = argv.index("--role")
        try:
            role = argv[i + 1]
        except IndexError:
            print("--role needs a value (admin or viewer).", file=sys.stderr)
            sys.exit(1)
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print("Usage: python -m app.create_user <username> <password> [--update] [--role admin|viewer]", file=sys.stderr)
        sys.exit(1)
    username, password = args
    if password == "-":
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            print("No password read from stdin.", file=sys.stderr)
            sys.exit(1)
    await userdb.init_db()
    if update:
        ok = await userdb.set_password(username, password)
        if not ok:
            print(f"No such user: {username}", file=sys.stderr)
            sys.exit(1)
        print(f"Password updated for user '{username}'.")
        return
    try:
        await userdb.create_user(username, password, role=role)
    except sqlite3.IntegrityError:
        print(f"User '{username}' already exists (use --update to change their password).", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(f"User '{username}' created ({role}).")


if __name__ == "__main__":
    asyncio.run(main())
