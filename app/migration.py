"""Data migration into a pgmon-monitored Postgres database from either
Oracle or another Postgres instance.

Design choices, deliberately kept simple for a v1:
  - The *destination* is always the database the Migration tab is open on
    (its already-managed connection pool is reused for all writes).
  - The *source* connection (Oracle or Postgres) is ad hoc: entered in the
    tab, tested live, and used only for the duration of a migration job --
    never persisted to disk, unlike the registered databases in dbstore.py.
  - A job copies one or more whole tables, table-by-table, streaming rows
    in batches via COPY (asyncpg's copy_records_to_table) rather than
    row-by-row INSERTs, so it's reasonably fast for real data volumes.
  - Only column data is migrated -- primary keys, foreign keys, indexes,
    and constraints are NOT recreated on the destination. This is a
    deliberate scope boundary (see README), not an oversight: constraint
    design is something a human should review per-table, not something a
    generic migration tool should guess at.
  - Jobs run as in-memory asyncio background tasks tracked in `_jobs`, the
    same lightweight pattern the poller already uses elsewhere in this
    app -- no external queue/worker needed for a single-process app.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field

import asyncpg
import oracledb

from app.metrics import _quote_ident

# CLOB/BLOB/NCLOB come back as str/bytes directly instead of LOB locator
# objects -- simpler and fine for a batch migration tool (no need for true
# LOB streaming semantics), and NUMBER columns come back as Decimal instead
# of float so numeric precision survives the trip into Postgres.
oracledb.defaults.fetch_lobs = False
oracledb.defaults.fetch_decimals = True

DEFAULT_BATCH_SIZE = 2000


class MigrationError(Exception):
    pass


# --------------------------------------------------------------- job store
_jobs: dict[str, dict] = {}


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job is None or job["status"] != "running":
        return False
    job["cancel_requested"] = True
    return True


def _new_job(source_type: str, tables: list[dict]) -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "source_type": source_type,
        "status": "running",
        "cancel_requested": False,
        "started_at": time.time(),
        "finished_at": None,
        "tables": [
            {
                "source": t["source_label"],
                "dest": t["dest_label"],
                "status": "pending",
                "rows_copied": 0,
                "row_estimate": t.get("row_estimate"),
                "error": None,
                "started_at": None,
                "finished_at": None,
            }
            for t in tables
        ],
    }
    _jobs[job_id] = job
    return job


def _pg_safe_ident(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    if not name or not re.match(r"[a-z_]", name):
        name = "t_" + name
    return name[:63]


# ------------------------------------------------------------ Oracle side
def _oracle_dsn_kwargs(host: str, port: int, service_name: str, username: str, password: str) -> dict:
    return dict(host=host, port=port, service_name=service_name, user=username, password=password)


async def test_oracle_connection(host: str, port: int, service_name: str, username: str, password: str) -> dict:
    conn = await asyncio.wait_for(
        oracledb.connect_async(**_oracle_dsn_kwargs(host, port, service_name, username, password)), timeout=10
    )
    try:
        cur = conn.cursor()
        await cur.execute("SELECT banner FROM v$version WHERE rownum = 1")
        row = await cur.fetchone()
        version = row[0] if row else None
    except Exception:
        version = None  # not every account can read v$version; connecting at all is the real test
    finally:
        await conn.close()
    return {"ok": True, "version": version}


# Oracle DATA_TYPE (from ALL_TAB_COLUMNS) -> Postgres column type. Anything
# not recognized falls back to `text`, which is always a safe (if lossy for
# round-tripping) landing type.
def _oracle_type_to_pg(data_type: str, precision, scale) -> str:
    dt = (data_type or "").upper()
    if dt in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        return "text"
    if dt == "NUMBER":
        if precision is None and scale is None:
            return "numeric"
        if scale in (0, None):
            if precision is not None and precision <= 4:
                return "smallint"
            if precision is not None and precision <= 9:
                return "integer"
            if precision is not None and precision <= 18:
                return "bigint"
            return "numeric"
        return f"numeric({precision or 38},{scale})"
    if dt == "BINARY_FLOAT":
        return "real"
    if dt in ("FLOAT", "BINARY_DOUBLE"):
        return "double precision"
    if dt == "DATE":
        return "timestamp"
    if dt.startswith("TIMESTAMP"):
        return "timestamptz" if "TIME ZONE" in dt else "timestamp"
    if dt in ("CLOB", "NCLOB", "LONG"):
        return "text"
    if dt in ("BLOB", "RAW", "LONG RAW"):
        return "bytea"
    return "text"


async def list_oracle_tables(host: str, port: int, service_name: str, username: str, password: str, owner: str) -> list[dict]:
    conn = await asyncio.wait_for(
        oracledb.connect_async(**_oracle_dsn_kwargs(host, port, service_name, username, password)), timeout=10
    )
    try:
        cur = conn.cursor()
        await cur.execute(
            "SELECT table_name, num_rows FROM all_tables WHERE owner = :owner ORDER BY table_name",
            owner=owner.upper(),
        )
        rows = await cur.fetchall()
    finally:
        await conn.close()
    return [{"table_name": r[0], "row_estimate": r[1]} for r in rows]


async def _get_oracle_columns(conn, owner: str, table: str) -> list[dict]:
    cur = conn.cursor()
    await cur.execute(
        """
        SELECT column_name, data_type, data_precision, data_scale
        FROM all_tab_columns
        WHERE owner = :owner AND table_name = :table_name
        ORDER BY column_id
        """,
        owner=owner.upper(), table_name=table.upper(),
    )
    rows = await cur.fetchall()
    if not rows:
        raise MigrationError(f"No columns found for {owner}.{table} -- does it exist, and is it visible to this user?")
    return [
        {"name": r[0], "pg_type": _oracle_type_to_pg(r[1], r[2], r[3])}
        for r in rows
    ]


async def _migrate_one_oracle_table(
    oracle_params: dict, owner: str, table: str,
    dest_pool: asyncpg.Pool, dest_schema: str, dest_table: str,
    create_if_missing: bool, truncate_first: bool, batch_size: int,
    job_table: dict, job: dict,
) -> None:
    conn = await asyncio.wait_for(oracledb.connect_async(**oracle_params), timeout=10)
    try:
        columns = await _get_oracle_columns(conn, owner, table)
        col_names = [c["name"].lower() for c in columns]
        # Oracle folds unquoted identifiers to uppercase, and ALL_TABLES /
        # ALL_TAB_COLUMNS above are queried with owner/table uppercased to
        # match. The actual data SELECT below quotes owner/table verbatim,
        # so it must be uppercased the same way -- otherwise a lowercase
        # owner/table as typed by the user (e.g. "demo") gets quoted into
        # a case-sensitive reference that doesn't match the real,
        # uppercase-stored object and raises ORA-00942.
        owner = owner.upper()
        table = table.upper()

        async with dest_pool.acquire() as dconn:
            if create_if_missing:
                cols_sql = ", ".join(f"{_quote_ident(n)} {c['pg_type']}" for n, c in zip(col_names, columns))
                await dconn.execute(
                    f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(dest_schema)}"
                )
                await dconn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_quote_ident(dest_schema)}.{_quote_ident(dest_table)} ({cols_sql})"
                )
            if truncate_first:
                await dconn.execute(f"TRUNCATE TABLE {_quote_ident(dest_schema)}.{_quote_ident(dest_table)}")

            oracle_cols_sql = ", ".join(f'"{c["name"]}"' for c in columns)
            cur = conn.cursor()
            cur.arraysize = batch_size
            cur.prefetchrows = batch_size
            await cur.execute(f"SELECT {oracle_cols_sql} FROM {_quote_ident_oracle(owner)}.{_quote_ident_oracle(table)}")

            total = 0
            while True:
                if job["cancel_requested"]:
                    job_table["status"] = "cancelled"
                    return
                rows = await cur.fetchmany(batch_size)
                if not rows:
                    break
                await dconn.copy_records_to_table(
                    dest_table, records=rows, columns=col_names, schema_name=dest_schema,
                )
                total += len(rows)
                job_table["rows_copied"] = total

        job_table["status"] = "done"
    finally:
        await conn.close()


def _quote_ident_oracle(name: str) -> str:
    # Oracle identifiers are unquoted-uppercase by convention; quoting them
    # exactly as stored (from ALL_TABLES/ALL_TAB_COLUMNS, already correct
    # case) avoids Oracle's own case-folding surprises.
    return '"' + name.replace('"', '""') + '"'


async def _run_oracle_job(
    job: dict, oracle_params: dict, owner: str, tables: list[str],
    dest_pool: asyncpg.Pool, dest_schema: str, create_if_missing: bool,
    truncate_first: bool, batch_size: int,
) -> None:
    for table, job_table in zip(tables, job["tables"]):
        if job["cancel_requested"]:
            job_table["status"] = "cancelled"
            continue
        job_table["status"] = "running"
        job_table["started_at"] = time.time()
        dest_table = _pg_safe_ident(table)
        try:
            await _migrate_one_oracle_table(
                oracle_params, owner, table, dest_pool, dest_schema, dest_table,
                create_if_missing, truncate_first, batch_size, job_table, job,
            )
        except Exception as e:
            job_table["status"] = "error"
            job_table["error"] = str(e)
        job_table["finished_at"] = time.time()
    job["status"] = "cancelled" if job["cancel_requested"] else "done"
    job["finished_at"] = time.time()


async def start_oracle_to_postgres(
    host: str, port: int, service_name: str, username: str, password: str, owner: str,
    tables: list[str], dest_pool: asyncpg.Pool, dest_schema: str,
    create_if_missing: bool = True, truncate_first: bool = False, batch_size: int = DEFAULT_BATCH_SIZE,
) -> str:
    oracle_params = _oracle_dsn_kwargs(host, port, service_name, username, password)
    job_tables = [
        {"source_label": f"{owner}.{t}", "dest_label": f"{dest_schema}.{_pg_safe_ident(t)}"}
        for t in tables
    ]
    job = _new_job("oracle", job_tables)
    asyncio.create_task(
        _run_oracle_job(job, oracle_params, owner, tables, dest_pool, dest_schema, create_if_missing, truncate_first, batch_size)
    )
    return job["id"]


# ---------------------------------------------------------- Postgres side
async def test_postgres_connection(host: str, port: int, dbname: str, username: str, password: str) -> dict:
    dsn = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"
    conn = await asyncio.wait_for(asyncpg.connect(dsn=dsn, timeout=8, command_timeout=8), timeout=10)
    try:
        version = await conn.fetchval("SHOW server_version")
    finally:
        await conn.close()
    return {"ok": True, "version": version}


async def list_postgres_tables(host: str, port: int, dbname: str, username: str, password: str) -> list[dict]:
    dsn = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"
    conn = await asyncio.wait_for(asyncpg.connect(dsn=dsn, timeout=8, command_timeout=8), timeout=10)
    try:
        rows = await conn.fetch(
            """
            SELECT n.nspname AS schema_name, c.relname AS table_name,
                   NULLIF(c.reltuples::bigint, -1) AS row_estimate
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg\\_%'
            ORDER BY n.nspname, c.relname
            """
        )
    finally:
        await conn.close()
    return [dict(r) for r in rows]


async def _get_postgres_columns(conn, schema: str, table: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS pg_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        schema, table,
    )
    if not rows:
        raise MigrationError(f"No columns found for {schema}.{table} -- does it exist?")
    return [dict(r) for r in rows]


async def _migrate_one_postgres_table(
    source_dsn: str, source_schema: str, source_table: str,
    dest_pool: asyncpg.Pool, dest_schema: str, dest_table: str,
    create_if_missing: bool, truncate_first: bool, batch_size: int,
    job_table: dict, job: dict,
) -> None:
    sconn = await asyncio.wait_for(asyncpg.connect(dsn=source_dsn, timeout=8, command_timeout=None), timeout=10)
    try:
        columns = await _get_postgres_columns(sconn, source_schema, source_table)
        col_names = [c["column_name"] for c in columns]

        async with dest_pool.acquire() as dconn:
            if create_if_missing:
                cols_sql = ", ".join(f"{_quote_ident(c['column_name'])} {c['pg_type']}" for c in columns)
                await dconn.execute(f"CREATE SCHEMA IF NOT EXISTS {_quote_ident(dest_schema)}")
                await dconn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_quote_ident(dest_schema)}.{_quote_ident(dest_table)} ({cols_sql})"
                )
            if truncate_first:
                await dconn.execute(f"TRUNCATE TABLE {_quote_ident(dest_schema)}.{_quote_ident(dest_table)}")

            select_cols_sql = ", ".join(_quote_ident(c) for c in col_names)
            query = f"SELECT {select_cols_sql} FROM {_quote_ident(source_schema)}.{_quote_ident(source_table)}"

            total = 0
            async with sconn.transaction():
                cursor = await sconn.cursor(query)
                while True:
                    if job["cancel_requested"]:
                        job_table["status"] = "cancelled"
                        return
                    rows = await cursor.fetch(batch_size)
                    if not rows:
                        break
                    await dconn.copy_records_to_table(
                        dest_table, records=rows, columns=col_names, schema_name=dest_schema,
                    )
                    total += len(rows)
                    job_table["rows_copied"] = total

        job_table["status"] = "done"
    finally:
        await sconn.close()


async def _run_postgres_job(
    job: dict, source_dsn: str, tables: list[tuple[str, str]],
    dest_pool: asyncpg.Pool, dest_schema: str, create_if_missing: bool,
    truncate_first: bool, batch_size: int,
) -> None:
    for (schema, table), job_table in zip(tables, job["tables"]):
        if job["cancel_requested"]:
            job_table["status"] = "cancelled"
            continue
        job_table["status"] = "running"
        job_table["started_at"] = time.time()
        dest_table = _pg_safe_ident(table)
        try:
            await _migrate_one_postgres_table(
                source_dsn, schema, table, dest_pool, dest_schema, dest_table,
                create_if_missing, truncate_first, batch_size, job_table, job,
            )
        except Exception as e:
            job_table["status"] = "error"
            job_table["error"] = str(e)
        job_table["finished_at"] = time.time()
    job["status"] = "cancelled" if job["cancel_requested"] else "done"
    job["finished_at"] = time.time()


async def start_postgres_to_postgres(
    host: str, port: int, dbname: str, username: str, password: str,
    tables: list[tuple[str, str]], dest_pool: asyncpg.Pool, dest_schema: str,
    create_if_missing: bool = True, truncate_first: bool = False, batch_size: int = 5000,
) -> str:
    source_dsn = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"
    job_tables = [
        {"source_label": f"{schema}.{table}", "dest_label": f"{dest_schema}.{_pg_safe_ident(table)}"}
        for schema, table in tables
    ]
    job = _new_job("postgres", job_tables)
    asyncio.create_task(
        _run_postgres_job(job, source_dsn, tables, dest_pool, dest_schema, create_if_missing, truncate_first, batch_size)
    )
    return job["id"]
