"""Connection pools to monitored Postgres instances.

pgmon monitors only what's explicitly registered via the Databases screen
(stored in app.dbstore) -- no built-in database is auto-registered or
auto-monitored. Each registered database gets its own pool, created lazily
on first use and cached here for reuse. Every pool gets the same
treatment: jsonb codec, tagged application_name, and a bootstrapped
`pgmon` schema for its own snapshot history.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import asyncpg

from app.config import settings

# Tag this app's own connections so the "who's connected" monitoring view can
# exclude them.
APP_CONNECTION_NAME = "pg_ai_monitor"

_pools: dict[str, asyncpg.Pool] = {}
_read_only_dbs: set[str] = set()  # standbys: no pgmon schema, no snapshot history -- see _make_pool


@dataclass
class ConnInfo:
    host: str
    port: int
    dbname: str
    username: str
    password: str

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.username}:{self.password}@{self.host}:{self.port}/{self.dbname}"


def _jsonb_encode(value) -> str:
    # `metrics` dicts can contain datetime/Decimal values from asyncpg
    # records, so fall back to str() for anything json can't handle natively.
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Auto-encode/decode jsonb columns to/from Python objects.
    await conn.set_type_codec(
        "jsonb", encoder=_jsonb_encode, decoder=json.loads, schema="pg_catalog", format="text"
    )


async def _make_pool(db_id: str, conn_info: ConnInfo) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn=conn_info.dsn,
        min_size=1,
        max_size=5,
        command_timeout=15,
        timeout=10,  # bounds each connection attempt (incl. pool warmup) -- asyncpg's own default is 60s per attempt
        init=_init_connection,
        server_settings={"application_name": APP_CONNECTION_NAME},
    )
    try:
        async with pool.acquire() as conn:
            in_recovery = await conn.fetchval("SELECT pg_is_in_recovery()")
        if in_recovery:
            # A hot standby rejects all writes, including CREATE SCHEMA for
            # pgmon's own snapshot history -- registering one via the
            # Databases screen is still useful for live current-state
            # monitoring, just without trend history for it. Skipping
            # bootstrap here, rather than letting it fail, is what lets the
            # pool be created at all.
            _read_only_dbs.add(db_id)
        else:
            _read_only_dbs.discard(db_id)
            await _bootstrap_schema(pool)
    except Exception:
        await pool.close()
        raise
    return pool


def is_read_only(db_id: str) -> bool:
    return db_id in _read_only_dbs


async def test_connection(conn_info: ConnInfo) -> None:
    """Raises on failure (bad host/credentials/etc); returns None on success."""
    conn = await asyncpg.connect(dsn=conn_info.dsn, timeout=8, command_timeout=8)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


async def get_conn_info(db_id: str) -> ConnInfo:
    """Connection details for a db_id, without creating/touching its pool.

    Used by ora2pg_migration, which shells out to ora2pg/psql as separate
    processes and so needs a real host/port/dbname/user/password rather
    than an asyncpg pool object.
    """
    from app import dbstore  # local import: avoids a dbstore<->db import cycle

    record = await dbstore.get_database(db_id)
    if record is None:
        raise KeyError(f"No registered database with id {db_id!r}")
    return ConnInfo(
        host=record.host, port=record.port, dbname=record.dbname,
        username=record.username, password=record.password,
    )


async def get_pool(db_id: str) -> asyncpg.Pool:
    if db_id in _pools:
        return _pools[db_id]

    # Lazily create+cache a pool for a registered database.
    from app import dbstore  # local import: avoids a dbstore<->db import cycle

    record = await dbstore.get_database(db_id)
    if record is None:
        raise KeyError(f"No registered database with id {db_id!r}")
    conn_info = ConnInfo(
        host=record.host, port=record.port, dbname=record.dbname,
        username=record.username, password=record.password,
    )
    pool = await _make_pool(db_id, conn_info)
    _pools[db_id] = pool
    return pool


async def drop_pool(db_id: str) -> None:
    pool = _pools.pop(db_id, None)
    if pool is not None:
        await pool.close()


async def close_all_pools() -> None:
    for db_id in list(_pools.keys()):
        await drop_pool(db_id)


async def _bootstrap_schema(pool: asyncpg.Pool) -> None:
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema}".snapshots (
                id BIGSERIAL PRIMARY KEY,
                captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                metrics JSONB NOT NULL
            )
            """
        )
        await conn.execute(
            f'CREATE INDEX IF NOT EXISTS snapshots_captured_at_idx '
            f'ON "{schema}".snapshots (captured_at)'
        )
