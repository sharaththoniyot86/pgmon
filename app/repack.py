"""Online bloat removal via the pg_repack client tool -- an alternative to
VACUUM FULL that doesn't take an ACCESS EXCLUSIVE lock for the duration:
it builds a fresh copy of the table in the background (using triggers to
capture concurrent writes), then swaps it in with only a brief lock at the
very end.

Needs the pg_repack *extension* installed on the target Postgres server
(separate from this app's own pg_repack *client* binary, from the
postgresql-18-repack apt package -- see this repo's Dockerfile) -- see
postgres-image/Dockerfile for how to add the extension to a Bitnami image
that doesn't ship it. Gracefully unavailable otherwise, same pattern as
pg_stat_statements/hypopg elsewhere in this app.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import asyncpg

from app.db import ConnInfo

_REPACK_INSTALL_HINT = (
    "pg_repack isn't installed on this Postgres server -- it's a separate extension, not bundled with core "
    "Postgres (see postgres-image/Dockerfile in this repo for how to add it), then CREATE EXTENSION pg_repack."
)


class RepackError(Exception):
    pass


async def get_repack_status(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        available = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_repack')")
        installed = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_repack')")
    return {
        "available": bool(available), "installed": bool(installed),
        "hint": None if installed else _REPACK_INSTALL_HINT,
    }


async def install_repack(pool: asyncpg.Pool) -> dict:
    status = await get_repack_status(pool)
    if status["installed"]:
        return {"ok": True, "message": "pg_repack is already installed."}
    if not status["available"]:
        raise RepackError(_REPACK_INSTALL_HINT)
    async with pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_repack")
        except asyncpg.PostgresError as e:
            raise RepackError(f"CREATE EXTENSION pg_repack failed: {e}")
    return {"ok": True, "message": "pg_repack installed."}


# ------------------------------------------------------------------ jobs
# Same lightweight in-memory job pattern as app/backup.py -- kept separate
# rather than reused since backup.py's job helpers are module-private and
# this is the only other place that needs the shape.
_jobs: dict[str, dict] = {}


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def _new_job() -> dict:
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "status": "running", "started_at": time.time(), "finished_at": None, "error": None, "log": []}
    _jobs[job_id] = job
    return job


def _log(job: dict, text: str) -> None:
    for line in text.splitlines():
        if line.strip():
            job["log"].append(line)
    if len(job["log"]) > 500:
        job["log"] = job["log"][-500:]


async def _run_repack_job(job: dict, conn_info: ConnInfo, schema: str, table: str) -> None:
    try:
        pool = await asyncpg.create_pool(dsn=conn_info.dsn, min_size=1, max_size=1, command_timeout=8)
        try:
            status = await get_repack_status(pool)
        finally:
            await pool.close()
        if not status["installed"]:
            raise RepackError(status["hint"])

        env = dict(os.environ, PGPASSWORD=conn_info.password)
        qualified = f'{schema}.{table}'
        _log(job, f"=== pg_repack: rebuilding {qualified} online ===")
        proc = await asyncio.create_subprocess_exec(
            "pg_repack", "-h", conn_info.host, "-p", str(conn_info.port), "-U", conn_info.username,
            "-d", conn_info.dbname, "-t", qualified, "--no-password", "--jobs=1",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            _log(job, line.decode(errors="replace"))
        rc = await proc.wait()
        if rc != 0:
            raise RepackError(f"pg_repack exited with code {rc} -- see log for details.")
        _log(job, "Repack complete.")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()


async def start_repack(conn_info: ConnInfo, schema: str, table: str) -> str:
    job = _new_job()
    asyncio.create_task(_run_repack_job(job, conn_info, schema, table))
    return job["id"]
