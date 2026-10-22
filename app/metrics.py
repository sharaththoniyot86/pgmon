"""Read-only introspection queries against pg_stat_* / pg_catalog views."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime

import asyncpg

from app.config import settings
from app import db as db_module
from app import extension_catalog
from app import roles as roles_mod


async def ping(pool: asyncpg.Pool) -> dict:
    """Cheap liveness check for the Databases list screen."""
    start = time.monotonic()
    try:
        await asyncio.wait_for(pool.fetchval("SELECT 1"), timeout=3)
        return {"status": "up", "latency_ms": round((time.monotonic() - start) * 1000, 1), "error": None}
    except Exception as e:
        return {"status": "down", "latency_ms": None, "error": str(e)[:200]}


async def get_quick_stats(pool: asyncpg.Pool) -> dict:
    """Cheap single-query stats for the Databases list cards: current
    connection count, cache hit ratio, server uptime, and version. No
    history needed -- same pg_postmaster_start_time() source as the
    Overview tab's Uptime stat card, just surfaced here too so it's
    visible without opening a database."""
    async with pool.acquire() as conn:
        conn_count = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        db_stat = await conn.fetchrow(
            "SELECT blks_hit, blks_read FROM pg_stat_database WHERE datname = current_database()"
        )
        uptime_seconds = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))"
        )
        # SHOW server_version (not SELECT version()) -- e.g. "16.15
        # (Debian 16.15-1.pgdg13+2)"; only the leading "16.15" is wanted
        # here, the build/platform suffix is already shown in full on the
        # Overview tab for whoever opens the database and wants it.
        server_version = await conn.fetchval("SHOW server_version")
    cache_hit = None
    if db_stat and (db_stat["blks_hit"] + db_stat["blks_read"]) > 0:
        cache_hit = round(100.0 * db_stat["blks_hit"] / (db_stat["blks_hit"] + db_stat["blks_read"]), 2)
    return {
        "connection_count": conn_count,
        "cache_hit_ratio_pct": cache_hit,
        "uptime_seconds": float(uptime_seconds) if uptime_seconds is not None else None,
        "version": server_version.split()[0] if server_version else None,
    }


async def get_tps(pool: asyncpg.Pool) -> float | None:
    """Transactions/sec, derived from the two most recent poller snapshots
    (commits+rollbacks delta / time delta). None until at least two
    snapshots exist for this database (first ~poll interval after it's
    registered)."""
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f'SELECT captured_at, metrics FROM "{schema}".snapshots ORDER BY captured_at DESC LIMIT 2'
            )
        except asyncpg.exceptions.UndefinedTableError:
            return None  # read-only standby -- no pgmon schema, see db._make_pool
    if len(rows) < 2:
        return None
    newest, older = rows[0], rows[1]
    dt = (newest["captured_at"] - older["captured_at"]).total_seconds()
    if dt <= 0:
        return None

    def total_xact(m: dict) -> int:
        db_stat = (m.get("overview") or {}).get("db_stat") or {}
        return (db_stat.get("xact_commit") or 0) + (db_stat.get("xact_rollback") or 0)

    delta = total_xact(newest["metrics"]) - total_xact(older["metrics"])
    return round(max(delta, 0) / dt, 2)


async def get_overview(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        version = await conn.fetchval("SHOW server_version")
        db_size_bytes = await conn.fetchval(
            "SELECT pg_database_size(current_database())"
        )
        max_conn = await conn.fetchval("SHOW max_connections")
        uptime_seconds = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))"
        )
        conn_counts = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE state = 'active')                  AS active,
                count(*) FILTER (WHERE state = 'idle')                    AS idle,
                count(*) FILTER (WHERE state = 'idle in transaction')     AS idle_in_txn,
                count(*)                                                  AS total
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND application_name <> $1
            """,
            db_module.APP_CONNECTION_NAME,
        )
        db_stat = await conn.fetchrow(
            """
            SELECT
                xact_commit, xact_rollback, blks_read, blks_hit,
                tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
                deadlocks, temp_files, temp_bytes, conflicts
            FROM pg_stat_database
            WHERE datname = current_database()
            """
        )
        cache_hit_ratio = None
        if db_stat and (db_stat["blks_hit"] + db_stat["blks_read"]) > 0:
            cache_hit_ratio = round(
                100.0 * db_stat["blks_hit"] / (db_stat["blks_hit"] + db_stat["blks_read"]), 2
            )

        replication = await conn.fetch(
            "SELECT client_addr, state, sync_state, "
            "write_lag, flush_lag, replay_lag FROM pg_stat_replication"
        )

    return {
        "version": version,
        "db_size_bytes": db_size_bytes,
        "max_connections": int(max_conn),
        "uptime_seconds": float(uptime_seconds),
        "connections": dict(conn_counts) if conn_counts else {},
        "cache_hit_ratio_pct": cache_hit_ratio,
        "db_stat": dict(db_stat) if db_stat else {},
        "replicas": [dict(r) for r in replication],
    }


async def get_settings(pool: asyncpg.Pool) -> dict:
    """Every server parameter from pg_settings, with a reliable "changed
    from default" flag. Postgres itself already tracks this precisely via
    `source`: it reads 'default' only when nothing overrode the compiled-in
    default (not postgresql.conf, not ALTER SYSTEM, not the environment,
    not the current session) -- comparing that is far more reliable than
    comparing `setting` to `boot_val` as text, since some values are
    reformatted (e.g. memory sizes) even when unchanged in spirit."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, setting, unit, category, short_desc,
                   boot_val, reset_val, source, context, vartype, pending_restart,
                   min_val, max_val, enumvals
            FROM pg_settings
            ORDER BY (source <> 'default') DESC, name ASC
            """
        )
    settings = []
    for r in rows:
        d = dict(r)
        d["changed"] = d["source"] != "default"
        # 'internal' settings (server_version, block_size, ...) are compiled
        # in and can never be changed by any means -- everything else can at
        # least be written to postgresql.auto.conf via ALTER SYSTEM, even if
        # it then needs a reload or a full restart to actually take effect.
        d["editable"] = d["context"] != "internal"
        settings.append(d)
    return {
        "settings": settings,
        "changed_count": sum(1 for s in settings if s["changed"]),
        "total_count": len(settings),
    }


# Contexts where ALTER SYSTEM + pg_reload_conf() is enough to take effect
# immediately for existing sessions. 'postmaster' needs a full server
# restart; 'backend'/'superuser-backend' only apply to new connections.
_RELOAD_TAKES_EFFECT_CONTEXTS = {"sighup", "superuser", "user"}


async def set_parameter(pool: asyncpg.Pool, name: str, value: str | None, reset: bool = False) -> dict:
    """Persistently changes a server parameter via ALTER SYSTEM (writes
    postgresql.auto.conf) and reloads config so the change takes effect
    immediately wherever Postgres allows that without a restart. Both the
    parameter name and value are passed as bind parameters into Postgres's
    own format('...%I...%L...') to build the ALTER SYSTEM statement --
    %I/%L quote exactly the way Postgres's own parser would, which is
    safer than any hand-rolled escaping, since ALTER SYSTEM (like all DDL)
    can't be parameterized directly the way a normal query can."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT context, vartype, enumvals, min_val, max_val, setting, source, pending_restart"
            " FROM pg_settings WHERE name = $1",
            name,
        )
        if row is None:
            raise UnsafeQueryError(f"No such parameter: {name}")
        if row["context"] == "internal":
            raise UnsafeQueryError(f"{name} is an internal, compiled-in setting and cannot be changed.")

        if reset:
            stmt = await conn.fetchval("SELECT format('ALTER SYSTEM RESET %I', $1::text)", name)
        else:
            if value is None or value == "":
                raise UnsafeQueryError("A value is required (or pass reset=true).")
            if row["vartype"] == "enum" and row["enumvals"] is not None and value not in row["enumvals"]:
                raise UnsafeQueryError(f"{value!r} is not a valid value for {name}. Allowed: {', '.join(row['enumvals'])}")
            if row["vartype"] == "bool" and value.lower() not in ("on", "off", "true", "false", "yes", "no", "1", "0"):
                raise UnsafeQueryError(f"{value!r} is not a valid boolean for {name} (use on/off).")
            stmt = await conn.fetchval("SELECT format('ALTER SYSTEM SET %I = %L', $1::text, $2::text)", name, value)

        try:
            await conn.execute(stmt)
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(str(e))

        # pg_reload_conf() just signals the postmaster (SIGHUP), which then
        # re-reads the config files and forwards the signal on to every
        # backend, including this one -- all of that is real, variable-
        # latency OS signal delivery, not something that completes before
        # this call returns. Reading pg_settings again immediately can
        # therefore still show the pre-change value even though the reload
        # was requested correctly, so poll briefly for the settled state
        # instead of trusting a single read right after the reload.
        #
        # Exception: a 'postmaster'-context setting's live `setting`/`source`
        # never change until an actual restart, and `pending_restart` can
        # stay stuck at whatever it already was (a known Postgres quirk, not
        # something a reload resolves either way) -- so there's nothing to
        # wait for there; take one read and move on rather than spinning for
        # the full timeout every time.
        await conn.execute("SELECT pg_reload_conf()")

        after = await conn.fetchrow(
            "SELECT setting, unit, source, pending_restart FROM pg_settings WHERE name = $1", name
        )
        if row["context"] != "postmaster":
            before_source, before_pending_restart = row["source"], row["pending_restart"]
            for _ in range(20):  # ~2s total at 100ms apiece
                settled = (
                    after["pending_restart"] != before_pending_restart
                    or (after["source"] == "default") != (before_source == "default")
                    or after["setting"] != row["setting"]
                )
                if settled:
                    break
                await asyncio.sleep(0.1)
                after = await conn.fetchrow(
                    "SELECT setting, unit, source, pending_restart FROM pg_settings WHERE name = $1", name
                )

    context = row["context"]
    if context == "postmaster":
        note = "This setting only takes effect after a full Postgres server restart -- pgmon does not restart the server automatically."
    elif context in ("backend", "superuser-backend"):
        note = "This setting takes effect for new connections only -- existing sessions (including this app's) keep their old value."
    elif context in _RELOAD_TAKES_EFFECT_CONTEXTS:
        note = "Applied immediately via a config reload."
    else:
        note = None

    return {
        "name": name,
        "context": context,
        "after": dict(after) if after else None,
        "restart_required": bool(after["pending_restart"]) if after else (context == "postmaster"),
        "note": note,
    }


async def get_connections(pool: asyncpg.Pool) -> list[dict]:
    # Excludes ALL of pgmon's own pooled connections (matched by application_name,
    # not just the single pid handling this request) -- otherwise this tool's own
    # idle pool connections show up as unexplained "mystery" entries in the
    # connections view, and as sessions a user could plausibly try to terminate.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                pid, usename, application_name, client_addr::text AS client_addr,
                state, wait_event_type, wait_event,
                EXTRACT(EPOCH FROM (now() - query_start))::float AS query_seconds,
                EXTRACT(EPOCH FROM (now() - state_change))::float AS state_seconds,
                left(query, 500) AS query
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND application_name <> $1
            ORDER BY query_start ASC NULLS LAST
            """,
            db_module.APP_CONNECTION_NAME,
        )
    return [dict(r) for r in rows]


async def get_locks(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                blocked.pid            AS blocked_pid,
                blocked_act.usename    AS blocked_user,
                left(blocked_act.query, 300) AS blocked_query,
                blocking.pid           AS blocking_pid,
                blocking_act.usename   AS blocking_user,
                left(blocking_act.query, 300) AS blocking_query
            FROM pg_locks blocked
            JOIN pg_stat_activity blocked_act ON blocked_act.pid = blocked.pid
            JOIN pg_locks blocking
                ON blocking.locktype = blocked.locktype
               AND blocking.database IS NOT DISTINCT FROM blocked.database
               AND blocking.relation IS NOT DISTINCT FROM blocked.relation
               AND blocking.page IS NOT DISTINCT FROM blocked.page
               AND blocking.tuple IS NOT DISTINCT FROM blocked.tuple
               AND blocking.transactionid IS NOT DISTINCT FROM blocked.transactionid
               AND blocking.pid != blocked.pid
               AND blocking.granted
            JOIN pg_stat_activity blocking_act ON blocking_act.pid = blocking.pid
            WHERE NOT blocked.granted
            """
        )
    return [dict(r) for r in rows]


async def terminate_backend(pool: asyncpg.Pool, pid: int) -> bool:
    """Force-kill a backend (SELECT pg_terminate_backend) -- used to unblock
    a session stuck waiting on a lock. Refuses to terminate any of pgmon's own
    pooled connections, not just the one exact pid handling this request --
    the pool holds up to several connections tagged the same application_name,
    and get_connections() already hides them, but this is the actual safety
    boundary so it must not rely on the list filter alone. Returns True if a
    backend with that pid existed and was signalled (matches Postgres's own
    pg_terminate_backend return value)."""
    async with pool.acquire() as conn:
        target_app_name = await conn.fetchval(
            "SELECT application_name FROM pg_stat_activity WHERE pid = $1", pid
        )
        if target_app_name == db_module.APP_CONNECTION_NAME:
            raise UnsafeQueryError("Refusing to terminate pgmon's own connection.")
        return await conn.fetchval("SELECT pg_terminate_backend($1)", pid)


_PGSS_INSTALL_HINT = (
    "pg_stat_statements requires shared_preload_libraries=pg_stat_statements at server "
    "start (a full restart, not just a reload), then CREATE EXTENSION pg_stat_statements;"
)


async def get_pg_stat_statements_status(pool: asyncpg.Pool) -> dict:
    """Everything needed to explain *why* per-query stats might be missing or
    incomplete, and to drive the SQL Performance tab's summary cards."""
    async with pool.acquire() as conn:
        preload = await conn.fetchval("SHOW shared_preload_libraries")
        preloaded = "pg_stat_statements" in [p.strip() for p in (preload or "").split(",")]
        installed = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements')"
        )
        extversion = None
        stats_since = None
        track = track_io_timing = pgss_max = None
        if installed:
            extversion = await conn.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname = 'pg_stat_statements'"
            )
            track_io_timing = await conn.fetchval("SHOW track_io_timing")
            try:
                track = await conn.fetchval("SHOW pg_stat_statements.track")
                pgss_max = await conn.fetchval("SHOW pg_stat_statements.max")
            except asyncpg.PostgresError:
                pass  # GUC only visible once the library is actually loaded
            try:
                stats_since = await conn.fetchval("SELECT min(stats_since) FROM pg_stat_statements")
            except asyncpg.PostgresError:
                stats_since = None  # older pg_stat_statements versions lack stats_since
    return {
        "preloaded": preloaded,
        "installed": installed,
        "available": preloaded and installed,
        "extversion": extversion,
        "track": track,
        "pg_stat_statements_max": pgss_max,
        "track_io_timing": track_io_timing,
        "stats_since": stats_since,
        "hint": None if (preloaded and installed) else _PGSS_INSTALL_HINT,
    }


async def install_pg_stat_statements(pool: asyncpg.Pool) -> dict:
    """Best-effort `CREATE EXTENSION` -- only actually works once
    shared_preload_libraries already includes pg_stat_statements (which needs
    a server restart to take effect and isn't something a SQL statement can
    do). Surfaces that distinction clearly rather than a raw Postgres error."""
    status = await get_pg_stat_statements_status(pool)
    if status["installed"]:
        return {"ok": True, "message": "pg_stat_statements is already installed."}
    if not status["preloaded"]:
        raise UnsafeQueryError(_PGSS_INSTALL_HINT)
    async with pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"CREATE EXTENSION pg_stat_statements failed: {e}")
    return {"ok": True, "message": "pg_stat_statements installed."}


async def reset_query_stats(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        try:
            await conn.execute("SELECT pg_stat_statements_reset()")
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"Reset failed: {e}")


# (pg_stat_statements column, output alias, SQL to select it -- None means
# just the column name). Postgres has renamed/added a few of these across
# versions (blk_read_time -> shared_blk_read_time in PG17, wal_buffers_full
# added in PG18, ...), so the actual SELECT list below is built by checking
# which of these really exist rather than assuming one server version.
_QUERY_COLUMN_WISHLIST = [
    ("min_exec_time", "min_ms", "round(min_exec_time::numeric, 2)"),
    ("max_exec_time", "max_ms", "round(max_exec_time::numeric, 2)"),
    ("stddev_exec_time", "stddev_ms", "round(stddev_exec_time::numeric, 2)"),
    ("mean_plan_time", "mean_plan_ms", "round(mean_plan_time::numeric, 2)"),
    ("shared_blks_hit", "shared_blks_hit", None),
    ("shared_blks_read", "shared_blks_read", None),
    ("shared_blks_dirtied", "shared_blks_dirtied", None),
    ("shared_blks_written", "shared_blks_written", None),
    ("temp_blks_read", "temp_blks_read", None),
    ("temp_blks_written", "temp_blks_written", None),
    ("shared_blk_read_time", "blk_read_ms", "round(shared_blk_read_time::numeric, 2)"),
    ("blk_read_time", "blk_read_ms", "round(blk_read_time::numeric, 2)"),
    ("shared_blk_write_time", "blk_write_ms", "round(shared_blk_write_time::numeric, 2)"),
    ("blk_write_time", "blk_write_ms", "round(blk_write_time::numeric, 2)"),
    ("wal_records", "wal_records", None),
    ("wal_bytes", "wal_bytes", "wal_bytes::numeric"),
    ("parallel_workers_launched", "parallel_workers_launched", None),
]

# order_by values the UI can request -> the SQL expression to sort on.
# Whitelisted (not user SQL) so this can't be used to inject anything.
_QUERY_ORDER_BY = {
    "total_time": "total_exec_time",
    "mean_time": "mean_exec_time",
    "calls": "calls",
    "rows": "rows",
    "shared_blks_read": "shared_blks_read",
    "temp_blks_written": "temp_blks_written",
    "wal_bytes": "wal_bytes",
}


async def _pgss_available_columns(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'pg_stat_statements'"
    )
    return {r["column_name"] for r in rows}


async def get_top_queries(
    pool: asyncpg.Pool, limit: int = 25, order_by: str = "total_time",
    all_databases: bool = False, include_monitoring: bool = False,
) -> dict:
    status = await get_pg_stat_statements_status(pool)
    if not status["available"]:
        return {"available": False, "reason": status["hint"] or _PGSS_INSTALL_HINT, "queries": [], "order_by": order_by}

    if order_by not in _QUERY_ORDER_BY:
        raise UnsafeQueryError(f"Unknown order_by: {order_by!r}. Allowed: {', '.join(_QUERY_ORDER_BY)}")

    async with pool.acquire() as conn:
        available = await _pgss_available_columns(conn)
        seen_aliases: set[str] = set()
        select_parts = [
            "left(query, 500) AS query", "queryid::text AS queryid", "calls", "rows",
            "round(total_exec_time::numeric, 2) AS total_ms",
            "round(mean_exec_time::numeric, 2) AS mean_ms",
        ]
        for col, alias, expr in _QUERY_COLUMN_WISHLIST:
            if alias in seen_aliases or col not in available:
                continue
            seen_aliases.add(alias)
            select_parts.append(f"{expr or col} AS {alias}")

        where_parts = []
        if not all_databases:
            where_parts.append("dbid = (SELECT oid FROM pg_database WHERE datname = current_database())")
        if not include_monitoring:
            # Exclude this app's own queries so they don't drown out the
            # application's real workload: periodic snapshot writes, this
            # very status query, and anything else touching the pgmon
            # bookkeeping schema -- including admin actions run *against*
            # it (e.g. an Administration-tab VACUUM aimed at pgmon.snapshots
            # itself). Match broadly on the quoted schema name rather than
            # one exact statement shape, since Postgres quotes identifiers
            # differently depending on how each statement was built (a
            # plain INSERT here leaves the table name unquoted, but VACUUM's
            # own deparsing quotes both schema and table).
            where_parts.append("query NOT ILIKE '%pg_stat_statements%'")
            where_parts.append(f"query NOT ILIKE '%\"{settings.MONITOR_SCHEMA}\"%'")
            # asyncpg (the driver this app -- and possibly other Python
            # clients -- connects with) issues these on every connection
            # acquire/release and first use of a type: connection-state
            # reset boilerplate and a one-time type-introspection query.
            # They're real statements that really ran, but they're driver
            # plumbing, not application workload, and they'd otherwise
            # dominate a sort by `calls` almost immediately.
            for noise in ("RESET ALL", "CLOSE ALL", "UNLISTEN *", "pg_advisory_unlock_all()", "typeinfo_tree"):
                where_parts.append(f"query NOT ILIKE '%{noise}%'")
        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        rows = await conn.fetch(
            f"""
            SELECT {", ".join(select_parts)}
            FROM pg_stat_statements
            {where_clause}
            ORDER BY {_QUERY_ORDER_BY[order_by]} DESC NULLS LAST
            LIMIT {int(limit)}
            """
        )
    return {"available": True, "reason": None, "queries": [dict(r) for r in rows], "order_by": order_by}


_HYPOPG_INSTALL_HINT = (
    "hypopg isn't installed on this Postgres server -- it's a separate extension, not bundled with core "
    "Postgres (see postgres-image/Dockerfile in this repo for how to add it), then CREATE EXTENSION hypopg."
)


async def get_hypopg_status(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        available = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'hypopg')")
        installed = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'hypopg')")
    return {
        "available": bool(available), "installed": bool(installed),
        "hint": None if installed else _HYPOPG_INSTALL_HINT,
    }


async def install_hypopg(pool: asyncpg.Pool) -> dict:
    status = await get_hypopg_status(pool)
    if status["installed"]:
        return {"ok": True, "message": "hypopg is already installed."}
    if not status["available"]:
        raise UnsafeQueryError(_HYPOPG_INSTALL_HINT)
    async with pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS hypopg")
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"CREATE EXTENSION hypopg failed: {e}")
    return {"ok": True, "message": "hypopg installed."}


def _walk_plan_nodes(node: dict):
    yield node
    for child in node.get("Plans", []) or []:
        yield from _walk_plan_nodes(child)


# Heuristic, text-level pattern matching on the query string itself -- not a
# real SQL parser. EXPLAIN (GENERIC_PLAN) looked like the natural tool for
# finding filtered columns (it's designed to plan a parameterized query
# without real parameter values), but asyncpg's extended query protocol has
# no way to send a truly *unbound* parameter -- it always binds an actual
# value, and binding SQL NULL makes Postgres constant-fold `col = NULL` into
# an always-empty result rather than a generic estimate, hiding every
# candidate. So this discovers candidates from the query text directly
# instead, then validates the guess against a real EXPLAIN before trusting
# it (see get_tuning_advice's missing-index check).
_FROM_TABLE_RE = re.compile(r'\bFROM\s+"?(\w+)"?(?:\s*\.\s*"?(\w+)"?)?', re.IGNORECASE)
_FILTER_COL_PLACEHOLDER_RE = re.compile(r'\b"?(\w+)"?\s*=\s*\$(\d+)\b')


async def _representative_value(conn: asyncpg.Connection, schema: str, relation: str, column: str):
    """A real value for `column` chosen from where an index would help
    *most* -- the least-frequent entry in its most-common-values stats, if
    any (a highly skewed column's rare value is exactly the case a plain
    ndistinct-based estimate hides: common enough to show up in ANALYZE's
    stats, rare enough that scanning for it via an index beats a seq scan
    by a lot). Falls back to any single real value from the table (a
    read-only, LIMIT-1 lookup) when there's no MCV list (e.g. a
    high-cardinality column) so this only ever gives up on a column that's
    completely empty."""
    # most_common_vals is the pseudo-type anyarray -- it can't be cast
    # directly to text[] (Postgres has no such cast registered for
    # anyarray), only via its text representation first.
    stat = await conn.fetchrow(
        "SELECT most_common_vals::text::text[] AS vals, most_common_freqs AS freqs "
        "FROM pg_stats WHERE schemaname = $1 AND tablename = $2 AND attname = $3",
        schema, relation, column,
    )
    if stat and stat["vals"]:
        pairs = sorted(zip(stat["vals"], stat["freqs"] or []), key=lambda p: p[1] if p[1] is not None else 0)
        return pairs[0][0]
    qualified = f'"{schema}"."{relation}"'
    row = await conn.fetchrow(f'SELECT "{column}"::text AS v FROM {qualified} WHERE "{column}" IS NOT NULL LIMIT 1')
    return row["v"] if row else None


async def _analyze_query_for_index_candidate(conn: asyncpg.Connection, query_text: str) -> dict | None:
    """Guesses the query's primary table from its first FROM clause, finds
    `column = $N` filters against real columns of that table, and builds a
    fully-literal version of the query with representative values
    substituted in (see _representative_value) -- deliberately single-table
    only; a join's second/third table is out of scope for this heuristic.
    Returns None if there's no FROM, the guessed name isn't a real table,
    or none of its columns turn out to be genuinely filtered."""
    m = _FROM_TABLE_RE.search(query_text)
    if not m:
        return None
    schema, relation = ("public", m.group(1)) if not m.group(2) else (m.group(1), m.group(2))
    is_table = await conn.fetchval(
        "SELECT c.relkind IN ('r', 'p') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2",
        schema, relation,
    )
    if not is_table:
        return None

    columns = []
    substitutions: dict[str, str] = {}
    for col, num in dict(_FILTER_COL_PLACEHOLDER_RE.findall(query_text)).items():
        col_exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2 AND column_name = $3",
            schema, relation, col,
        )
        if not col_exists:
            continue
        try:
            value = await _representative_value(conn, schema, relation, col)
        except asyncpg.PostgresError:
            continue
        if value is None:
            continue
        substitutions[num] = await conn.fetchval("SELECT quote_literal($1)", value)
        columns.append(col)
    if not columns:
        return None

    literal_query = re.sub(r"\$(\d+)", lambda mm: substitutions.get(mm.group(1), "NULL"), query_text)
    return {"schema": schema, "relation": relation, "columns": columns, "literal_query": literal_query}


# Multi-table version of _FROM_TABLE_RE above -- used to guess which
# referenced table a `column = $N` filter belongs to when literalizing an
# arbitrary captured query for View Plan / Tuning Advisor, where (unlike
# _analyze_query_for_index_candidate's single-table heuristic) the filtered
# column could belong to any joined table, not just the first FROM.
_TABLE_REF_RE = re.compile(r'\b(?:FROM|JOIN)\s+"?(\w+)"?(?:\s*\.\s*"?(\w+)"?)?', re.IGNORECASE)


async def _try_literalize_query(conn: asyncpg.Connection, query_text: str, placeholders: list[int]) -> tuple[str, list[int]]:
    """Best-effort substitution of $N placeholders with a real representative
    value (see _representative_value), so a captured pg_stat_statements
    query -- always parameterized -- can actually be run through EXPLAIN.
    There's no other way to do this: asyncpg's extended query protocol has
    no concept of a truly unbound parameter (see the longer explanation on
    _analyze_query_for_index_candidate above), so a real value has to go in
    somewhere before Postgres will plan the query at all.

    Only resolves plain `column = $N` filters, checked against every table
    the query references (not just the first, unlike
    _analyze_query_for_index_candidate's single-table heuristic) --
    anything else ($N inside an IN-list, LIMIT, a function call, ...) is
    left alone and reported back as unresolved."""
    table_refs = [("public", m.group(1)) if not m.group(2) else (m.group(1), m.group(2))
                  for m in _TABLE_REF_RE.finditer(query_text)]

    substitutions: dict[int, str] = {}
    for col, num_str in _FILTER_COL_PLACEHOLDER_RE.findall(query_text):
        num = int(num_str)
        if num not in placeholders or num in substitutions:
            continue
        for schema, relation in table_refs:
            col_exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2 AND column_name = $3",
                schema, relation, col,
            )
            if not col_exists:
                continue
            try:
                value = await _representative_value(conn, schema, relation, col)
            except asyncpg.PostgresError:
                continue
            if value is not None:
                substitutions[num] = await conn.fetchval("SELECT quote_literal($1)", value)
                break

    unresolved = [n for n in placeholders if n not in substitutions]
    literal_query = re.sub(r"\$(\d+)", lambda mm: substitutions.get(int(mm.group(1)), mm.group(0)), query_text)
    return literal_query, unresolved


def _split_single_statement(query: str) -> tuple[str, str]:
    stripped = query.strip()
    if not stripped:
        raise UnsafeQueryError("Query is empty.")
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        raise UnsafeQueryError("Only a single statement is allowed (no semicolons inside the query).")
    first_word = body.lstrip().split(None, 1)[0].upper() if body.lstrip() else ""
    return body, first_word


async def get_query_plan(pool: asyncpg.Pool, query: str, analyze: bool = False) -> dict:
    """EXPLAIN a query -- from the SQL Performance tab's captured queries
    table, or pasted by hand -- without requiring AI. Plan-only (the
    default) never executes anything, even a write statement, so it works
    for any captured query regardless of type; `analyze=True` actually runs
    it for real timing/row-count data, so it's restricted to read-only
    SELECT/WITH the same way the AI Query Optimizer's EXPLAIN ANALYZE
    already is.

    A captured pg_stat_statements query is always parameterized ($1, $2,
    ...) -- see _try_literalize_query for why and how those get resolved
    to real values before EXPLAIN can run at all. When that can't resolve
    every placeholder, this raises rather than guessing, and says exactly
    which ones -- paste the query with real values into the box below
    instead."""
    body, first_word = _split_single_statement(query)
    if analyze and first_word not in ("SELECT", "WITH"):
        raise UnsafeQueryError(
            "EXPLAIN ANALYZE actually executes the statement, so only read-only SELECT/WITH queries can use it "
            "here -- turn it off to see a plan-only estimate for any statement type."
        )

    placeholders = sorted(set(int(n) for n in re.findall(r"\$(\d+)", body)))
    resolved_query = body
    if placeholders:
        async with pool.acquire() as conn:
            resolved_query, unresolved = await _try_literalize_query(conn, body, placeholders)
        if unresolved:
            missing = ", ".join(f"${n}" for n in unresolved)
            raise UnsafeQueryError(
                f"Couldn't automatically resolve parameter(s) {missing} to real values -- this only auto-fills "
                "plain `column = $N` filters. Paste the query into the box below with real values substituted "
                "for the placeholders instead."
            )

    options = "ANALYZE, BUFFERS, FORMAT JSON" if analyze else "BUFFERS, FORMAT JSON"
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Read-only + short statement timeout as defense in depth --
            # analyze is already restricted to SELECT/WITH above, but this
            # also protects against a plan-only EXPLAIN of a write
            # statement somehow still trying to execute.
            await conn.execute("SET LOCAL transaction_read_only = on")
            await conn.execute("SET LOCAL statement_timeout = '10s'")
            try:
                plan_rows = await conn.fetch(f"EXPLAIN ({options}) {resolved_query}")
            except asyncpg.PostgresError as e:
                raise UnsafeQueryError(f"EXPLAIN failed: {e}")
    raw = plan_rows[0][0] if plan_rows else "[]"
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return {
        "query": resolved_query, "original_query": body,
        "substituted": bool(placeholders), "analyzed": analyze, "plan": plan,
    }


async def get_tuning_advice(pool: asyncpg.Pool, query: str, analyze: bool = False) -> dict:
    """A deterministic, no-AI-required rundown of why a specific query
    might be slow and what to do about it -- the same spirit as Oracle's
    SQL Tuning Advisor, built entirely from signals this app already
    computes elsewhere: the real EXPLAIN plan (get_query_plan above), a
    verified hypopg index candidate (simulate-and-measure via hypopg,
    exactly this one query, nothing persisted), and plan-shape red flags
    (large sequential scans, disk-spilling sorts, and
    -- when analyze=True actually ran the query -- planner row-estimates
    that badly missed reality, a sign of stale table statistics).
    Every check either reads existing catalog/plan data or hypothetically
    simulates a change (hypopg); nothing here is ever written for real.
    """
    plan_result = await get_query_plan(pool, query, analyze=analyze)
    plan = plan_result["plan"][0]["Plan"]
    advice = []

    async with pool.acquire() as conn:
        hypopg_status = await get_hypopg_status(pool)
        if hypopg_status["installed"]:
            try:
                # Deliberately the pre-substitution text (with $N intact) --
                # that's what this helper's own column = $N pattern match
                # needs; get_query_plan already resolved it for EXPLAIN.
                cand = await _analyze_query_for_index_candidate(conn, plan_result["original_query"])
            except asyncpg.PostgresError:
                cand = None
            if cand and any(
                n.get("Node Type") == "Seq Scan" and n.get("Relation Name") == cand["relation"]
                for n in _walk_plan_nodes(plan)
            ):
                qualified = f'"{cand["schema"]}"."{cand["relation"]}"'
                cols_sql = ", ".join(f'"{c}"' for c in cand["columns"])
                index_sql = f"CREATE INDEX ON {qualified} ({cols_sql})"
                try:
                    await conn.execute("SELECT hypopg_reset()")
                    await conn.fetchrow("SELECT * FROM hypopg_create_index($1)", index_sql)
                    new_rows = await conn.fetch(f"EXPLAIN (FORMAT JSON) {cand['literal_query']}")
                    new_cost = json.loads(new_rows[0][0])[0]["Plan"].get("Total Cost")
                    baseline_cost = plan.get("Total Cost")
                    if baseline_cost and new_cost is not None and new_cost < baseline_cost * 0.8:
                        advice.append({
                            "severity": "high", "title": "Missing index",
                            "detail": (
                                f"Sequential scan on {cand['schema']}.{cand['relation']} -- a hypothetical index "
                                f"(hypopg, nothing actually created) drops the planner's own cost estimate "
                                f"{round((1 - new_cost / baseline_cost) * 100, 1)}% "
                                f"({round(baseline_cost, 1)} → {round(new_cost, 1)})."
                            ),
                            "suggested_sql": index_sql,
                        })
                except (asyncpg.PostgresError, asyncpg.InterfaceError):
                    pass
                finally:
                    await conn.execute("SELECT hypopg_reset()")

        for node in _walk_plan_nodes(plan):
            node_type = node.get("Node Type")
            plan_rows_est = node.get("Plan Rows")
            rel = node.get("Relation Name")

            if node_type == "Seq Scan" and plan_rows_est and plan_rows_est > 10000:
                detail = f"Estimated to scan ~{int(plan_rows_est):,} rows without an index"
                if node.get("Filter"):
                    detail += f" (filter: {node['Filter']})"
                advice.append({
                    "severity": "medium", "title": f"Large sequential scan on {rel or 'a table'}",
                    "detail": detail + ".", "suggested_sql": None,
                })

            sort_method = (node.get("Sort Method") or "")
            if "disk" in sort_method.lower():
                advice.append({
                    "severity": "medium", "title": "Sort spilled to disk",
                    "detail": (
                        f"{sort_method} used {node.get('Sort Space Used', '?')} KB of disk space -- "
                        "consider raising work_mem for this query/role."
                    ),
                    "suggested_sql": None,
                })

            if analyze and plan_rows_est and node.get("Actual Rows") is not None:
                actual = node["Actual Rows"]
                ratio = (actual / plan_rows_est) if plan_rows_est > 0 else None
                if actual > 100 and ratio is not None and (ratio > 10 or ratio < 0.1):
                    advice.append({
                        "severity": "low", "title": f"Row estimate off for {rel or node_type}",
                        "detail": (
                            f"Planner expected ~{int(plan_rows_est):,} rows, actually got {int(actual):,} -- "
                            "statistics may be stale. Consider running ANALYZE on the underlying table."
                        ),
                        "suggested_sql": None,
                    })

    if not advice:
        advice.append({
            "severity": "info", "title": "No issues found",
            "detail": "No large sequential scans, disk-spilling sorts, or verified missing indexes detected for this plan.",
            "suggested_sql": None,
        })
    return {
        "query": plan_result["query"], "substituted": plan_result["substituted"],
        "analyzed": analyze, "plan": plan_result["plan"], "advice": advice,
    }


async def _fetch_with_lock_timeout(conn: asyncpg.Connection, sql: str, *args, timeout: str = "2s") -> list:
    """Run a query that touches pg_total_relation_size/pg_relation_size (and
    therefore needs to briefly lock arbitrary relations) with a short,
    dedicated lock_timeout. Without this, one table sitting under an
    ACCESS EXCLUSIVE lock elsewhere (a VACUUM FULL, TRUNCATE, bulk LOCK
    TABLE, ...) blocks the *entire* query up to the connection's full
    command_timeout -- and since this same query backs the background
    poller's every-30s snapshot, that one locked table would silently break
    ALL monitoring history (IO rate, wait events, TPS, growth...) for as
    long as the lock is held. Raises asyncpg.exceptions.LockNotAvailableError
    fast instead, so callers can fall back to a size-less query.
    """
    async with conn.transaction():
        await conn.execute(f"SET LOCAL lock_timeout = '{timeout}'")
        return await conn.fetch(sql, *args)


async def get_table_stats(pool: asyncpg.Pool) -> list[dict]:
    sql_with_size = """
        SELECT
            s.schemaname,
            s.relname AS table_name,
            pg_total_relation_size(s.relid) AS total_bytes,
            s.seq_scan, s.idx_scan,
            s.n_live_tup, s.n_dead_tup,
            CASE WHEN s.n_live_tup + s.n_dead_tup > 0
                 THEN round(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
                 ELSE 0 END AS dead_tuple_pct,
            s.last_vacuum, s.last_autovacuum,
            s.last_analyze, s.last_autoanalyze
        FROM pg_stat_user_tables s
        ORDER BY pg_total_relation_size(s.relid) DESC
    """
    async with pool.acquire() as conn:
        try:
            rows = await _fetch_with_lock_timeout(conn, sql_with_size)
        except asyncpg.exceptions.LockNotAvailableError:
            # A table is exclusively locked elsewhere -- still return scan/
            # vacuum/dead-tuple stats for everything (which don't need any
            # lock), just without sizes, rather than losing the whole poll.
            rows = await conn.fetch(
                """
                SELECT
                    s.schemaname,
                    s.relname AS table_name,
                    NULL::bigint AS total_bytes,
                    s.seq_scan, s.idx_scan,
                    s.n_live_tup, s.n_dead_tup,
                    CASE WHEN s.n_live_tup + s.n_dead_tup > 0
                         THEN round(100.0 * s.n_dead_tup / (s.n_live_tup + s.n_dead_tup), 2)
                         ELSE 0 END AS dead_tuple_pct,
                    s.last_vacuum, s.last_autovacuum,
                    s.last_analyze, s.last_autoanalyze
                FROM pg_stat_user_tables s
                ORDER BY s.n_live_tup DESC
                """
            )
    return [dict(r) for r in rows]


async def get_index_stats(pool: asyncpg.Pool) -> list[dict]:
    sql_with_size = """
        SELECT
            schemaname, relname AS table_name, indexrelname AS index_name,
            idx_scan, pg_relation_size(indexrelid) AS index_bytes
        FROM pg_stat_user_indexes
        ORDER BY idx_scan ASC, index_bytes DESC
    """
    async with pool.acquire() as conn:
        try:
            rows = await _fetch_with_lock_timeout(conn, sql_with_size)
        except asyncpg.exceptions.LockNotAvailableError:
            rows = await conn.fetch(
                """
                SELECT
                    schemaname, relname AS table_name, indexrelname AS index_name,
                    idx_scan, NULL::bigint AS index_bytes
                FROM pg_stat_user_indexes
                ORDER BY idx_scan ASC
                """
            )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- schema
_OBJECT_KIND_LABELS = {"r": "table", "p": "table", "m": "materialized_view", "v": "view", "S": "sequence"}


def _quote_ident(name: str) -> str:
    """Proper Postgres identifier quoting (doubles embedded double-quotes),
    for building qualified names from values already confirmed to be real
    existing schema/object/column names -- never from arbitrary user SQL."""
    return '"' + name.replace('"', '""') + '"'


async def get_schemas(pool: asyncpg.Pool) -> list[dict]:
    sql_with_size = """
        SELECT
            n.nspname AS schema_name,
            pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
            count(c.oid) FILTER (WHERE c.relkind IN ('r', 'p'))    AS table_count,
            count(c.oid) FILTER (WHERE c.relkind IN ('v', 'm'))    AS view_count,
            count(c.oid) FILTER (WHERE c.relkind = 'S')            AS sequence_count,
            coalesce(sum(pg_total_relation_size(c.oid))
                     FILTER (WHERE c.relkind IN ('r', 'p')), 0)::bigint AS total_bytes
        FROM pg_catalog.pg_namespace n
        LEFT JOIN pg_catalog.pg_class c
            ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
        WHERE n.nspname NOT LIKE 'pg\\_temp\\_%'
          AND n.nspname NOT LIKE 'pg\\_toast_temp\\_%'
          AND n.nspname <> 'pg_toast'
        GROUP BY n.nspname, n.nspowner
        ORDER BY n.nspname
    """
    async with pool.acquire() as conn:
        try:
            rows = await _fetch_with_lock_timeout(conn, sql_with_size)
        except asyncpg.exceptions.LockNotAvailableError:
            rows = await conn.fetch(
                """
                SELECT
                    n.nspname AS schema_name,
                    pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
                    count(c.oid) FILTER (WHERE c.relkind IN ('r', 'p'))    AS table_count,
                    count(c.oid) FILTER (WHERE c.relkind IN ('v', 'm'))    AS view_count,
                    count(c.oid) FILTER (WHERE c.relkind = 'S')            AS sequence_count,
                    NULL::bigint AS total_bytes
                FROM pg_catalog.pg_namespace n
                LEFT JOIN pg_catalog.pg_class c
                    ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
                WHERE n.nspname NOT LIKE 'pg\\_temp\\_%'
                  AND n.nspname NOT LIKE 'pg\\_toast_temp\\_%'
                  AND n.nspname <> 'pg_toast'
                GROUP BY n.nspname, n.nspowner
                ORDER BY n.nspname
                """
            )
    schemas = [dict(r) for r in rows]
    for s in schemas:
        s["is_system"] = s["schema_name"] in ("pg_catalog", "information_schema") or s["schema_name"].startswith("pg_")
    return schemas


async def get_schema_objects(pool: asyncpg.Pool, schema_name: str) -> list[dict]:
    sql_with_size = """
        SELECT
            c.relname AS name, c.relkind::text AS relkind,
            pg_catalog.pg_get_userbyid(c.relowner) AS owner,
            CASE WHEN c.relkind IN ('r', 'p', 'm') THEN pg_total_relation_size(c.oid) ELSE NULL END AS size_bytes
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
        ORDER BY c.relkind, c.relname
    """
    async with pool.acquire() as conn:
        try:
            rows = await _fetch_with_lock_timeout(conn, sql_with_size, schema_name)
        except asyncpg.exceptions.LockNotAvailableError:
            rows = await conn.fetch(
                """
                SELECT
                    c.relname AS name, c.relkind::text AS relkind,
                    pg_catalog.pg_get_userbyid(c.relowner) AS owner,
                    NULL::bigint AS size_bytes
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $1 AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
                ORDER BY c.relkind, c.relname
                """,
                schema_name,
            )
    objects = [dict(r) for r in rows]
    for o in objects:
        o["kind"] = _OBJECT_KIND_LABELS.get(o.pop("relkind"), "other")
    return objects


async def _get_partitioning_info(conn: asyncpg.Connection, qualified: str) -> dict:
    """For a partitioned table (relkind='p'): its partition key expression
    and the list of partitions actually attached to it right now."""
    partition_key = await conn.fetchval("SELECT pg_get_partkeydef($1::regclass)", qualified)
    children = await conn.fetch(
        """
        SELECT
            n.nspname AS schema, c.relname AS name,
            pg_get_expr(c.relpartbound, c.oid, true) AS bound,
            pg_total_relation_size(c.oid) AS size_bytes,
            NULLIF(c.reltuples::bigint, -1) AS row_estimate,
            c.relkind = 'p' AS is_further_partitioned
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE i.inhparent = $1::regclass
        ORDER BY c.relname
        """,
        qualified,
    )
    return {"partition_key": partition_key, "partitions": [dict(r) for r in children]}


async def _get_partition_parent(conn: asyncpg.Connection, qualified: str) -> dict | None:
    """If this table is itself a partition of another table, its parent's
    name and the bound expression that defines this slice."""
    row = await conn.fetchrow(
        """
        SELECT pn.nspname AS schema, p.relname AS name,
               pg_get_expr(c.relpartbound, c.oid, true) AS bound
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace pn ON pn.oid = p.relnamespace
        WHERE c.oid = $1::regclass AND c.relispartition
        """,
        qualified,
    )
    return dict(row) if row else None


_SAFE_PARTITION_BOUND = re.compile(
    r"^\s*(DEFAULT|FOR VALUES (FROM\s*\(.+\)\s*TO\s*\(.+\)|IN\s*\(.+\)|WITH\s*\(.+\)))\s*$",
    re.IGNORECASE | re.DOTALL,
)


async def create_partition(
    pool: asyncpg.Pool, parent_schema: str, parent_table: str, partition_name: str, bound: str,
) -> None:
    """CREATE TABLE ... PARTITION OF ... <bound>. `bound` is a fragment of
    partitioning syntax (e.g. "FOR VALUES FROM ('2027-01-01') TO
    ('2027-02-01')") -- there's no Postgres-side function to build or
    validate this from separate values the way format('%L', ...) does for
    a plain literal, so it's checked against a structural pattern (must be
    DEFAULT or a well-formed FOR VALUES FROM/IN/WITH clause, nothing else)
    rather than trusted outright. This is a narrower version of the same
    trust boundary the AI Query Optimizer's pasted SQL already accepts
    from an admin -- not a fully parsed/parameterized value."""
    async with pool.acquire() as conn:
        parent_row = await conn.fetchrow(
            """
            SELECT c.relkind::text AS relkind
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = $2
            """,
            parent_schema, parent_table,
        )
        if parent_row is None or parent_row["relkind"] != "p":
            raise UnsafeQueryError(f"{parent_schema}.{parent_table} is not a partitioned table.")
        if not _SAFE_PARTITION_BOUND.match(bound or ""):
            raise UnsafeQueryError(
                "bound must be DEFAULT or a FOR VALUES FROM (...) TO (...) / IN (...) / WITH (...) clause."
            )
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = $2", parent_schema, partition_name,
        )
        if exists:
            raise UnsafeQueryError(f"{parent_schema}.{partition_name} already exists.")
        parent_q = f"{_quote_ident(parent_schema)}.{_quote_ident(parent_table)}"
        partition_q = f"{_quote_ident(parent_schema)}.{_quote_ident(partition_name)}"
        try:
            await conn.execute(f"CREATE TABLE {partition_q} PARTITION OF {parent_q} {bound}")
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"Couldn't create partition: {e}")


async def detach_partition(pool: asyncpg.Pool, parent_schema: str, parent_table: str, partition_name: str) -> None:
    """ALTER TABLE ... DETACH PARTITION -- the partition keeps its data as
    an ordinary standalone table, just no longer part of the parent."""
    async with pool.acquire() as conn:
        parent_row = await conn.fetchrow(
            """
            SELECT c.relkind::text AS relkind
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = $2
            """,
            parent_schema, parent_table,
        )
        if parent_row is None or parent_row["relkind"] != "p":
            raise UnsafeQueryError(f"{parent_schema}.{parent_table} is not a partitioned table.")
        is_child = await conn.fetchval(
            """
            SELECT 1 FROM pg_inherits i
            JOIN pg_class child ON child.oid = i.inhrelid
            JOIN pg_namespace n ON n.oid = child.relnamespace
            WHERE i.inhparent = $1::regclass AND n.nspname = $2 AND child.relname = $3
            """,
            f"{_quote_ident(parent_schema)}.{_quote_ident(parent_table)}", parent_schema, partition_name,
        )
        if not is_child:
            raise UnsafeQueryError(f"{parent_schema}.{partition_name} is not a partition of {parent_schema}.{parent_table}.")
        parent_q = f"{_quote_ident(parent_schema)}.{_quote_ident(parent_table)}"
        partition_q = _quote_ident(partition_name)
        try:
            await conn.execute(f"ALTER TABLE {parent_q} DETACH PARTITION {partition_q}")
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"Couldn't detach partition: {e}")


async def get_object_detail(pool: asyncpg.Pool, schema_name: str, object_name: str) -> dict | None:
    qualified = f"{_quote_ident(schema_name)}.{_quote_ident(object_name)}"
    async with pool.acquire() as conn:
        cls = await conn.fetchrow(
            """
            SELECT c.relkind::text AS relkind, pg_catalog.pg_get_userbyid(c.relowner) AS owner,
                   obj_description(c.oid) AS comment
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = $2
            """,
            schema_name, object_name,
        )
        if cls is None:
            return None

        kind = _OBJECT_KIND_LABELS.get(cls["relkind"], "other")
        result = {
            "schema": schema_name, "name": object_name, "kind": kind,
            "owner": cls["owner"], "comment": cls["comment"],
        }

        if kind in ("table", "materialized_view"):
            columns = await conn.fetch(
                """
                SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                ORDER BY ordinal_position
                """,
                schema_name, object_name,
            )
            try:
                indexes = await _fetch_with_lock_timeout(
                    conn,
                    """
                    SELECT i.indexname, i.indexdef, s.idx_scan,
                           pg_relation_size(s.indexrelid) AS index_bytes
                    FROM pg_indexes i
                    LEFT JOIN pg_stat_user_indexes s
                        ON s.schemaname = i.schemaname AND s.indexrelname = i.indexname
                    WHERE i.schemaname = $1 AND i.tablename = $2
                    ORDER BY i.indexname
                    """,
                    schema_name, object_name,
                )
            except asyncpg.exceptions.LockNotAvailableError:
                indexes = await conn.fetch(
                    """
                    SELECT i.indexname, i.indexdef, s.idx_scan, NULL::bigint AS index_bytes
                    FROM pg_indexes i
                    LEFT JOIN pg_stat_user_indexes s
                        ON s.schemaname = i.schemaname AND s.indexrelname = i.indexname
                    WHERE i.schemaname = $1 AND i.tablename = $2
                    ORDER BY i.indexname
                    """,
                    schema_name, object_name,
                )
            pk_rows = await conn.fetch(
                """
                SELECT a.attname
                FROM pg_index idx
                JOIN pg_attribute a ON a.attrelid = idx.indrelid AND a.attnum = ANY(idx.indkey)
                WHERE idx.indrelid = $1::regclass AND idx.indisprimary
                ORDER BY a.attnum
                """,
                qualified,
            )
            try:
                size_bytes = await _fetch_with_lock_timeout(
                    conn, "SELECT pg_total_relation_size($1::regclass) AS size", qualified
                )
                size_bytes = size_bytes[0]["size"]
            except asyncpg.exceptions.LockNotAvailableError:
                size_bytes = None
            stats = await conn.fetchrow(
                """
                SELECT seq_scan, idx_scan, n_live_tup, n_dead_tup,
                       last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
                FROM pg_stat_user_tables WHERE schemaname = $1 AND relname = $2
                """,
                schema_name, object_name,
            )
            result.update({
                "columns": [dict(c) for c in columns],
                "primary_key": [r["attname"] for r in pk_rows],
                "indexes": [dict(i) for i in indexes],
                "size_bytes": size_bytes,
                "stats": dict(stats) if stats else None,
            })
            if cls["relkind"] == "p":
                result["partitioning"] = await _get_partitioning_info(conn, qualified)
            else:
                parent_of = await _get_partition_parent(conn, qualified)
                if parent_of:
                    result["partition_of"] = parent_of
        elif kind == "view":
            columns = await conn.fetch(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                ORDER BY ordinal_position
                """,
                schema_name, object_name,
            )
            definition = None
            try:
                definition = await conn.fetchval("SELECT pg_get_viewdef($1::regclass, true)", qualified)
            except Exception:
                pass
            result.update({"columns": [dict(c) for c in columns], "definition": definition})
        elif kind == "sequence":
            seq = await conn.fetchrow(
                "SELECT * FROM pg_sequences WHERE schemaname = $1 AND sequencename = $2",
                schema_name, object_name,
            )
            result.update({"sequence": dict(seq) if seq else None})

    return result


def _jsonable_cell(value):
    """SELECT * against an arbitrary table can surface any Postgres type
    (inet, ranges, arrays, bytea, ...); rather than let an exotic one blow
    up JSON serialization, pass through JSON-native types and stringify
    everything else."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    return str(value)


async def get_object_rows(
    pool: asyncpg.Pool,
    schema_name: str,
    object_name: str,
    limit: int = 50,
    offset: int = 0,
    order_by: str | None = None,
    order_dir: str = "asc",
) -> dict:
    """Paginated data preview for a table/view/materialized view -- the
    'Data' tab. Read-only, capped page size, statement timeout; ORDER BY
    column is validated against the object's real column list before being
    interpolated (never taken as raw SQL from the caller)."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    qualified = f"{_quote_ident(schema_name)}.{_quote_ident(object_name)}"

    async with pool.acquire() as conn:
        cls = await conn.fetchrow(
            """
            SELECT c.relkind::text AS relkind, c.oid
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = $2
            """,
            schema_name, object_name,
        )
        if cls is None or _OBJECT_KIND_LABELS.get(cls["relkind"]) not in ("table", "materialized_view", "view"):
            raise UnsafeQueryError(f"No such table or view: {schema_name}.{object_name}")

        column_rows = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position
            """,
            schema_name, object_name,
        )
        column_names = [r["column_name"] for r in column_rows]
        if not column_names:
            return {"columns": [], "rows": [], "row_count_estimate": 0, "limit": limit, "offset": offset}

        order_clause = ""
        if order_by:
            if order_by not in column_names:
                raise UnsafeQueryError(f"Unknown column: {order_by}")
            direction = "DESC" if order_dir.lower() == "desc" else "ASC"
            order_clause = f"ORDER BY {_quote_ident(order_by)} {direction}"
        col_list = ", ".join(_quote_ident(c) for c in column_names)

        async with conn.transaction():
            await conn.execute("SET LOCAL transaction_read_only = on")
            await conn.execute("SET LOCAL statement_timeout = '10s'")
            rows = await conn.fetch(
                f"SELECT {col_list} FROM {qualified} {order_clause} LIMIT $1 OFFSET $2",
                limit, offset,
            )

        row_count_estimate = None
        if _OBJECT_KIND_LABELS.get(cls["relkind"]) in ("table", "materialized_view"):
            row_count_estimate = await conn.fetchval(
                "SELECT NULLIF(reltuples::bigint, -1) FROM pg_class WHERE oid = $1", cls["oid"]
            )

    return {
        "columns": column_names,
        "rows": [[_jsonable_cell(v) for v in dict(r).values()] for r in rows],
        "row_count_estimate": row_count_estimate,
        "limit": limit,
        "offset": offset,
    }


# ------------------------------------------------------------ administration
async def get_autovacuum_info(pool: asyncpg.Pool) -> dict:
    setting_names = [
        "autovacuum", "autovacuum_vacuum_scale_factor", "autovacuum_vacuum_threshold",
        "autovacuum_naptime", "autovacuum_max_workers",
    ]
    async with pool.acquire() as conn:
        setting_rows = await conn.fetch(
            "SELECT name, setting FROM pg_settings WHERE name = ANY($1)", setting_names
        )
        active_workers = await conn.fetch(
            """
            SELECT pid, left(query, 200) AS query,
                   EXTRACT(EPOCH FROM (now() - xact_start))::float AS running_seconds
            FROM pg_stat_activity
            WHERE query LIKE 'autovacuum:%'
            """
        )
        tables = await conn.fetch(
            """
            SELECT schemaname AS schema_name, relname AS table_name, n_live_tup, n_dead_tup,
                   CASE WHEN n_live_tup + n_dead_tup > 0 THEN round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 2) ELSE 0 END AS dead_tuple_pct,
                   last_autovacuum, autovacuum_count, last_autoanalyze, autoanalyze_count
            FROM pg_stat_user_tables
            ORDER BY n_dead_tup DESC
            LIMIT 25
            """
        )
    return {
        "settings": {r["name"]: r["setting"] for r in setting_rows},
        "active_workers": [dict(r) for r in active_workers],
        "tables": [dict(r) for r in tables],
    }


async def get_disk_usage_info(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        db_size = await conn.fetchval("SELECT pg_database_size(current_database())")
        wal_dir_bytes, wal_dir_error = None, None
        try:
            wal_dir_bytes = await conn.fetchval("SELECT sum(size) FROM pg_ls_waldir()")
        except Exception as e:
            wal_dir_error = str(e)
        temp = await conn.fetchrow(
            "SELECT temp_files, temp_bytes FROM pg_stat_database WHERE datname = current_database()"
        )
    return {
        "db_size_bytes": db_size,
        "wal_dir_bytes": wal_dir_bytes,
        "wal_dir_error": wal_dir_error,
        "temp_files": temp["temp_files"] if temp else None,
        "temp_bytes": temp["temp_bytes"] if temp else None,
    }


async def get_growth_rate(pool: asyncpg.Pool, hours: int = 24) -> dict:
    """Database size growth, derived from the oldest and newest poller
    snapshot within the window (not just two samples, so a brief size dip
    -- e.g. a VACUUM FULL -- doesn't swing the estimate as much as it would
    with a 2-point delta)."""
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f"""SELECT captured_at, metrics FROM "{schema}".snapshots
                    WHERE captured_at > now() - ($1 || ' hours')::interval
                    ORDER BY captured_at ASC""",
                str(hours),
            )
        except asyncpg.exceptions.UndefinedTableError:
            rows = []  # read-only standby -- no pgmon schema, see db._make_pool
    unavailable = {"available": False, "bytes_per_hour": None, "mb_per_day": None,
                   "window_hours": hours, "sample_count": len(rows)}
    if len(rows) < 2:
        return unavailable
    first, last = rows[0], rows[-1]
    dt_hours = (last["captured_at"] - first["captured_at"]).total_seconds() / 3600
    size_first = ((first["metrics"] or {}).get("overview") or {}).get("db_size_bytes")
    size_last = ((last["metrics"] or {}).get("overview") or {}).get("db_size_bytes")
    if dt_hours <= 0 or size_first is None or size_last is None:
        return unavailable
    bytes_per_hour = (size_last - size_first) / dt_hours
    return {
        "available": True,
        "bytes_per_hour": round(bytes_per_hour, 1),
        "mb_per_day": round(bytes_per_hour * 24 / (1024 * 1024), 3),
        "window_hours": round(dt_hours, 2),
        "sample_count": len(rows),
        "size_first_bytes": size_first,
        "size_last_bytes": size_last,
    }


async def _wal_cumulative(conn: asyncpg.Connection) -> dict | None:
    """Current cumulative WAL counters (server-wide, not per-database --
    pg_stat_wal has no datname column). Requires Postgres 14+; returns None
    on older servers or if the view is otherwise unavailable."""
    try:
        row = await conn.fetchrow(
            "SELECT wal_records, wal_bytes::float8 AS wal_bytes, stats_reset FROM pg_stat_wal"
        )
        return dict(row) if row else None
    except Exception:
        return None


async def get_wal_rate(pool: asyncpg.Pool) -> dict:
    """WAL bytes/records per second, derived from the two most recent poller
    snapshots (same technique as get_tps)."""
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f'SELECT captured_at, metrics FROM "{schema}".snapshots ORDER BY captured_at DESC LIMIT 2'
            )
        except asyncpg.exceptions.UndefinedTableError:
            rows = []  # read-only standby -- no pgmon schema, see db._make_pool
    unavailable = {"available": False, "bytes_per_sec": None, "records_per_sec": None}
    if len(rows) < 2:
        return unavailable
    newest, older = rows[0], rows[1]
    wal_new = (newest["metrics"] or {}).get("wal")
    wal_old = (older["metrics"] or {}).get("wal")
    dt = (newest["captured_at"] - older["captured_at"]).total_seconds()
    if dt <= 0 or not wal_new or not wal_old:
        return {**unavailable, "reason": "pg_stat_wal not available on this server (requires Postgres 14+)"}
    if wal_new.get("stats_reset") != wal_old.get("stats_reset"):
        return {**unavailable, "reason": "WAL stats were reset between samples"}
    bytes_delta = wal_new["wal_bytes"] - wal_old["wal_bytes"]
    records_delta = wal_new["wal_records"] - wal_old["wal_records"]
    if bytes_delta < 0:
        return {**unavailable, "reason": "WAL counters decreased unexpectedly between samples"}
    return {
        "available": True,
        "bytes_per_sec": round(bytes_delta / dt, 1),
        "records_per_sec": round(records_delta / dt, 2),
    }


async def get_checkpoint_info(pool: asyncpg.Pool) -> dict:
    """Checkpoint frequency/config. Postgres 17+ moved checkpoint stats out
    of pg_stat_bgwriter into pg_stat_checkpointer; this tries the new view
    first and falls back to the old one for older servers."""
    async with pool.acquire() as conn:
        setting_rows = await conn.fetch(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY($1)",
            ["checkpoint_timeout", "max_wal_size", "checkpoint_completion_target"],
        )
        cfg = {r["name"]: {"value": r["setting"], "unit": r["unit"]} for r in setting_rows}

        stats, source = None, None
        try:
            row = await conn.fetchrow(
                """SELECT num_timed, num_requested, restartpoints_timed, restartpoints_req,
                          write_time, sync_time, buffers_written, stats_reset,
                          EXTRACT(EPOCH FROM (now() - stats_reset)) / 3600 AS hours_since_reset
                   FROM pg_stat_checkpointer"""
            )
            if row:
                stats, source = dict(row), "pg_stat_checkpointer"
        except Exception:
            pass
        if stats is None:
            try:
                row = await conn.fetchrow(
                    """SELECT checkpoints_timed AS num_timed, checkpoints_req AS num_requested,
                              checkpoint_write_time AS write_time, checkpoint_sync_time AS sync_time,
                              buffers_checkpoint AS buffers_written, stats_reset,
                              EXTRACT(EPOCH FROM (now() - stats_reset)) / 3600 AS hours_since_reset
                       FROM pg_stat_bgwriter"""
                )
                if row:
                    stats, source = dict(row), "pg_stat_bgwriter"
            except Exception:
                pass

    checkpoints_per_hour = None
    if stats and stats.get("hours_since_reset") and stats["hours_since_reset"] > 0:
        total = (stats.get("num_timed") or 0) + (stats.get("num_requested") or 0)
        checkpoints_per_hour = round(total / stats["hours_since_reset"], 3)

    return {"config": cfg, "stats": stats, "source": source, "checkpoints_per_hour": checkpoints_per_hour}


async def get_index_usage_summary(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT coalesce(sum(seq_scan),0)::bigint AS total_seq_scan, "
            "coalesce(sum(idx_scan),0)::bigint AS total_idx_scan "
            "FROM pg_stat_user_tables"
        )
        try:
            unused = await _fetch_with_lock_timeout(
                conn,
                """SELECT schemaname, relname AS table_name, indexrelname AS index_name,
                          pg_relation_size(indexrelid) AS index_bytes
                   FROM pg_stat_user_indexes
                   WHERE idx_scan = 0
                   ORDER BY pg_relation_size(indexrelid) DESC
                   LIMIT 5""",
            )
        except asyncpg.exceptions.LockNotAvailableError:
            unused = await conn.fetch(
                """SELECT schemaname, relname AS table_name, indexrelname AS index_name,
                          NULL::bigint AS index_bytes
                   FROM pg_stat_user_indexes
                   WHERE idx_scan = 0
                   LIMIT 5"""
            )
    total_seq = row["total_seq_scan"] if row else 0
    total_idx = row["total_idx_scan"] if row else 0
    total = total_seq + total_idx
    return {
        "total_seq_scan": total_seq,
        "total_idx_scan": total_idx,
        "pct_reads_via_index": round(100.0 * total_idx / total, 2) if total > 0 else None,
        "top_unused_indexes": [dict(r) for r in unused],
    }


async def get_administration_info(pool: asyncpg.Pool) -> dict:
    """Everything for the Administration tab, bundled -- also fed to Claude
    for the AI Administration Review."""
    autovacuum = await get_autovacuum_info(pool)
    disk = await get_disk_usage_info(pool)
    growth = await get_growth_rate(pool)
    wal_rate = await get_wal_rate(pool)
    checkpoint = await get_checkpoint_info(pool)
    index_usage = await get_index_usage_summary(pool)
    bloat_tables = sorted(
        [t for t in autovacuum["tables"] if (t["dead_tuple_pct"] or 0) > 0],
        key=lambda t: t["dead_tuple_pct"], reverse=True,
    )[:10]
    return {
        "autovacuum": autovacuum,
        "table_bloat_estimate": bloat_tables,
        "disk_usage": disk,
        "growth_rate": growth,
        "wal_rate": wal_rate,
        "checkpoint": checkpoint,
        "index_usage": index_usage,
    }


_DATE_LIKE_TYPES = {"date", "timestamp without time zone", "timestamp with time zone"}
# Same idea as _FILTER_COL_PLACEHOLDER_RE (index advisor / tuning advisor
# above) but any comparison operator, not just "=" -- a date/timestamp
# column is almost always filtered with a range operator (>, <, BETWEEN),
# not equality, so the equality-only regex those features use would miss
# exactly the evidence this needs.
_FILTER_COL_ANY_OP_RE = re.compile(r'\b"?(\w+)"?\s*(=|<=|>=|<|>)\s*\$\d+\b')


async def _column_filter_evidence(conn: asyncpg.Connection, schema: str, table: str, columns: list[str]) -> dict[str, int]:
    """How many distinct captured queries filter on each of `columns` --
    real usage evidence from pg_stat_statements, when available, to prefer
    a partition key actual queries filter on over one that merely looks
    plausible from its type/name. Best-effort: returns an empty dict (every
    candidate then relies on type/cardinality alone) if pg_stat_statements
    isn't installed, or on any error -- this is corroborating evidence for
    the advisory below, not something its recommendation depends on."""
    counts = {c: 0 for c in columns}
    try:
        installed = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements')"
        )
        if not installed:
            return counts
        rows = await conn.fetch(
            "SELECT query FROM pg_stat_statements WHERE query ILIKE $1 LIMIT 500", f"%{table}%",
        )
    except asyncpg.PostgresError:
        return counts
    for r in rows:
        for col, _op in _FILTER_COL_ANY_OP_RE.findall(r["query"]):
            if col in counts:
                counts[col] += 1
    return counts


async def get_partitioning_advisor(pool: asyncpg.Pool, min_rows: int = 10_000, min_bytes: int = 10 * 1024 * 1024) -> dict:
    """Deterministic (no AI needed) advice on which large, currently
    un-partitioned tables would likely benefit from partitioning, which
    column, and which of Postgres's three techniques fits best:

      RANGE   a date/timestamp column exists -- far and away the most
              common real-world case (time-series-shaped data: orders,
              events, logs, ...). Preferred whenever one is found.
      LIST    no date column, but a column has a small, stable set of
              distinct values (roughly 2-50) that real queries filter on
              by equality -- e.g. region, status, tenant_id.
      HASH    neither of the above -- no natural range or short value
              list, but the table is large/bloated/heavily scanned enough
              that spreading it across partitions would still help
              (smaller per-partition vacuum/index maintenance, finer-
              grained locking), on whichever column real queries filter
              on most, or the primary key as a last resort.

    Every signal used already exists in Postgres's own catalogs/stats
    (pg_class for size, information_schema for column types, pg_stats for
    cardinality, pg_stat_statements for real filter usage when installed)
    -- nothing here is invented. This recommends, it doesn't execute:
    converting an existing table to partitioned needs a real data
    migration (a new partitioned table, copying data across, then
    swapping names) with real downtime/locking considerations that a
    monitoring tool shouldn't silently automate. What this DOES give is
    the actual CREATE TABLE ... PARTITION BY DDL to adapt and run
    yourself, via the same trusted-identifier quoting (_quote_ident) used
    by the Schema tab's own partition management.
    """
    async with pool.acquire() as conn:
        candidates = await _fetch_with_lock_timeout(
            conn,
            """
            SELECT n.nspname AS schema, c.relname AS table_name,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   NULLIF(c.reltuples::bigint, -1) AS row_estimate,
                   coalesce(s.seq_scan, 0) AS seq_scan, coalesce(s.idx_scan, 0) AS idx_scan,
                   coalesce(s.n_dead_tup, 0) AS n_dead_tup, coalesce(s.n_live_tup, 0) AS n_live_tup
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE c.relkind = 'r' AND NOT c.relispartition
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', $3)
              AND n.nspname NOT LIKE 'pg\\_temp\\_%'
              AND (pg_total_relation_size(c.oid) >= $1 OR c.reltuples >= $2)
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT 15
            """,
            min_bytes, min_rows, settings.MONITOR_SCHEMA,
        )

        suggestions = []
        for t in candidates:
            schema, table = t["schema"], t["table_name"]
            columns = await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = $2",
                schema, table,
            )
            date_cols = [c["column_name"] for c in columns if c["data_type"] in _DATE_LIKE_TYPES]
            other_cols = [c["column_name"] for c in columns if c["data_type"] not in _DATE_LIKE_TYPES]

            stats_rows = await conn.fetch(
                "SELECT attname, n_distinct FROM pg_stats WHERE schemaname = $1 AND tablename = $2",
                schema, table,
            )
            n_distinct = {r["attname"]: r["n_distinct"] for r in stats_rows}
            live_rows = max(t["n_live_tup"], 1)

            def distinct_count(col: str) -> float | None:
                nd = n_distinct.get(col)
                if nd is None:
                    return None
                return -nd * live_rows if nd < 0 else nd  # pg_stats: negative = fraction of rows, positive = absolute count

            evidence = await _column_filter_evidence(conn, schema, table, date_cols + other_cols)

            technique = column = reason = None
            if date_cols:
                # Prefer whichever date column real queries actually filter
                # on; fall back to the first if pg_stat_statements has no
                # opinion (still a very likely-correct default -- created_at/
                # updated_at-shaped columns almost always are the right key).
                column = max(date_cols, key=lambda c: evidence.get(c, 0)) if any(evidence.get(c) for c in date_cols) else date_cols[0]
                technique = "RANGE"
                hits = evidence.get(column, 0)
                reason = (
                    f"{column} is a date/timestamp column"
                    + (f", filtered on by {hits} distinct captured quer{'y' if hits == 1 else 'ies'}" if hits else "")
                    + " -- RANGE partitioning (e.g. monthly) is the standard fit for time-series-shaped data like this."
                )
            else:
                list_candidates = [
                    c for c in other_cols
                    if (dc := distinct_count(c)) is not None and 2 <= dc <= 50 and evidence.get(c, 0) > 0
                ]
                if list_candidates:
                    column = max(list_candidates, key=lambda c: evidence.get(c, 0))
                    technique = "LIST"
                    reason = (
                        f"{column} has ~{round(distinct_count(column))} distinct values and is filtered on by "
                        f"{evidence[column]} distinct captured quer{'y' if evidence[column] == 1 else 'ies'} -- "
                        "a small, stable value set like this fits LIST partitioning well."
                    )
                elif other_cols:
                    # Last resort: no natural range or short value list, but
                    # the table's size/bloat/scan pattern still makes it a
                    # partitioning candidate -- hash on whatever real
                    # queries filter on most (falling back to the first
                    # column) purely to get smaller, independently
                    # maintainable/lockable chunks.
                    column = max(other_cols, key=lambda c: evidence.get(c, 0)) if any(evidence.get(c) for c in other_cols) else other_cols[0]
                    technique = "HASH"
                    reason = (
                        f"No date column and no low-cardinality column with a clear filter pattern -- HASH on "
                        f"{column} spreads this large table into independently maintainable chunks (smaller "
                        "vacuum/index work and finer-grained locking per partition) without needing a natural "
                        "range or value list."
                    )

            if not technique:
                continue

            dead_pct = round(100.0 * t["n_dead_tup"] / (t["n_live_tup"] + t["n_dead_tup"]), 1) if (t["n_live_tup"] + t["n_dead_tup"]) > 0 else 0
            qualified_new = f"{_quote_ident(schema)}.{_quote_ident(table + '_partitioned')}"
            qualified_old = f"{_quote_ident(schema)}.{_quote_ident(table)}"
            col_q = _quote_ident(column)
            # Deliberately LIKE ... INCLUDING DEFAULTS/GENERATED only, NOT
            # INCLUDING ALL / INCLUDING INDEXES / INCLUDING CONSTRAINTS:
            # Postgres requires every unique/primary key on a partitioned
            # table to include the partition column, so blindly copying the
            # original table's primary key (almost never `column` itself)
            # fails outright -- verified this the hard way against a real
            # table before writing it this way. Indexes/keys on the new
            # table are deliberately left for the user to (re)design
            # around the partition column instead of guessing one that may
            # not even be valid.
            like_clause = f"(LIKE {qualified_old} INCLUDING DEFAULTS INCLUDING GENERATED)"
            pk_note = (
                "-- Add indexes/constraints on the new table yourself -- LIKE above deliberately\n"
                f"-- skips them: a primary/unique key here must include \"{column}\" (the partition\n"
                "-- column), which the original table's key usually doesn't.\n"
            )
            if technique == "RANGE":
                example_ddl = (
                    f"CREATE TABLE {qualified_new} {like_clause}\n"
                    f"    PARTITION BY RANGE ({col_q});\n"
                    f"{pk_note}\n"
                    f"CREATE TABLE {_quote_ident(schema)}.{_quote_ident(table + '_p_first')} "
                    f"PARTITION OF {qualified_new}\n"
                    f"    FOR VALUES FROM (MINVALUE) TO ('<next-period-start>');\n"
                    "-- repeat per period (month/quarter/year, matching how the data actually grows) --\n"
                    "-- consider pg_partman (Extensions tab) to automate creating future partitions."
                )
            elif technique == "LIST":
                example_ddl = (
                    f"CREATE TABLE {qualified_new} {like_clause}\n"
                    f"    PARTITION BY LIST ({col_q});\n"
                    f"{pk_note}\n"
                    f"CREATE TABLE {_quote_ident(schema)}.{_quote_ident(table + '_p_example')} "
                    f"PARTITION OF {qualified_new}\n"
                    "    FOR VALUES IN ('<value1>', '<value2>');  -- one partition per value, or group several per partition"
                )
            else:
                example_ddl = (
                    f"CREATE TABLE {qualified_new} {like_clause}\n"
                    f"    PARTITION BY HASH ({col_q});\n"
                    f"{pk_note}\n"
                    f"CREATE TABLE {_quote_ident(schema)}.{_quote_ident(table + '_p0')} "
                    f"PARTITION OF {qualified_new}\n"
                    "    FOR VALUES WITH (MODULUS 4, REMAINDER 0);  -- repeat for REMAINDER 1..3"
                )

            suggestions.append({
                "schema": schema, "table": table, "total_bytes": t["total_bytes"], "row_estimate": t["row_estimate"],
                "dead_tuple_pct": dead_pct, "seq_scan": t["seq_scan"], "idx_scan": t["idx_scan"],
                "technique": technique, "column": column, "reason": reason, "example_ddl": example_ddl,
            })

    return {
        "min_rows": min_rows, "min_bytes": min_bytes,
        "candidates_scanned": len(candidates), "suggestions": suggestions,
    }


class PartitioningError(Exception):
    pass


async def check_partitioning_prerequisites(pool: asyncpg.Pool, schema: str, table: str, column: str) -> dict:
    """{"blocking": [...], "notices": [...]}. Blocking reasons mean
    convert_to_partitioned() must not be called -- checked here before any
    DDL runs, and again (implicitly, since it re-queries live catalogs) as
    a race guard right before the actual conversion starts. Empty
    "blocking" means it's safe to proceed with the conservative set
    convert_to_partitioned() actually automates: recreating CHECK
    constraints and outgoing foreign keys verbatim via Postgres's own
    deparsing, and -- the one structural change it makes, called out in
    "notices" rather than treated as a blocker -- widening a primary/
    unique key to include the partition column when it doesn't already,
    since Postgres requires that on any partitioned table. That's the
    standard, well-documented way to partition an existing table by a
    column that isn't already part of its key (a plain serial `id` primary
    key essentially never is), not a guess: it doesn't change what's
    actually unique about the data (an id from a single sequence stays
    globally unique in practice), just the constraint Postgres can
    directly enforce.

    Genuinely refused outright, never auto-handled: another table's
    foreign key pointing IN at this one, a view/materialized view built on
    it, a user-defined trigger, or a row-security policy -- getting any of
    those subtly wrong is a real risk this app won't take on your behalf.
    Secondary (non-unique) indexes are NOT a blocker either -- those are
    just not auto-recreated (too much DDL-text-parsing risk for too little
    benefit; trivial to re-add by hand afterward), and the conversion job
    says which ones existed so nothing is silently lost.
    """
    blocking: list[str] = []
    notices: list[str] = []
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind = 'r' AND NOT c.relispartition",
            schema, table,
        )
        if row is None:
            return {"blocking": [f"{schema}.{table} doesn't exist, or isn't a plain (non-partitioned) table."], "notices": []}
        oid = row["oid"]

        col_exists = await conn.fetchval(
            "SELECT 1 FROM pg_attribute WHERE attrelid = $1 AND attname = $2 AND NOT attisdropped", oid, column,
        )
        if not col_exists:
            return {"blocking": [f"Column {column!r} doesn't exist on {schema}.{table}."], "notices": []}

        incoming_fks = await conn.fetch(
            "SELECT conname, conrelid::regclass::text AS from_table FROM pg_constraint "
            "WHERE confrelid = $1 AND contype = 'f'", oid,
        )
        for fk in incoming_fks:
            blocking.append(f"Foreign key \"{fk['conname']}\" on {fk['from_table']} references this table.")

        triggers = await conn.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = $1 AND NOT tgisinternal", oid,
        )
        for tg in triggers:
            blocking.append(f"Trigger \"{tg['tgname']}\" is defined on this table.")

        dependents = await conn.fetch(
            """
            SELECT DISTINCT dv_ns.nspname AS schema, dv.relname AS name
            FROM pg_depend d
            JOIN pg_rewrite r ON d.objid = r.oid
            JOIN pg_class dv ON r.ev_class = dv.oid
            JOIN pg_namespace dv_ns ON dv_ns.oid = dv.relnamespace
            WHERE d.refobjid = $1 AND dv.relkind IN ('v', 'm') AND dv.oid <> $1
            """, oid,
        )
        for d in dependents:
            blocking.append(f"View \"{d['schema']}.{d['name']}\" depends on this table.")

        policy_count = await conn.fetchval("SELECT count(*) FROM pg_policy WHERE polrelid = $1", oid)
        if policy_count:
            blocking.append(f"{policy_count} row-security polic{'y' if policy_count == 1 else 'ies'} defined on this table.")

        key_rows = await conn.fetch(
            """
            SELECT con.conname, con.contype::text AS contype, array_agg(att.attname ORDER BY u.ord) AS columns
            FROM pg_constraint con
            JOIN unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord) ON true
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = u.attnum
            WHERE con.conrelid = $1 AND con.contype IN ('p', 'u')
            GROUP BY con.conname, con.contype
            """, oid,
        )
        for k in key_rows:
            if column not in k["columns"]:
                kind = "Primary key" if k["contype"] == "p" else "Unique constraint"
                notices.append(
                    f"{kind} \"{k['conname']}\" ({', '.join(k['columns'])}) will be widened to "
                    f"({', '.join(k['columns'])}, {column}) -- Postgres requires the partition column "
                    "to be part of every unique/primary key on a partitioned table."
                )
    return {"blocking": blocking, "notices": notices}


def _month_start(d) -> date:
    d = d.date() if isinstance(d, datetime) else d
    return date(d.year, d.month, 1)


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


async def _compute_partition_defs(conn: asyncpg.Connection, schema: str, table: str, technique: str, column: str) -> list[dict]:
    """Concrete partitions to create, sized from the table's actual data --
    not arbitrary placeholders. Each entry is {name_suffix, clause}, where
    `clause` is a ready-to-use `FOR VALUES ...` / `DEFAULT` fragment."""
    qualified = f"{_quote_ident(schema)}.{_quote_ident(table)}"
    col_q = _quote_ident(column)
    if technique == "RANGE":
        row = await conn.fetchrow(f"SELECT min({col_q}) AS lo, max({col_q}) AS hi FROM {qualified}")
        if row["lo"] is None:
            return [{"name_suffix": "default", "clause": "DEFAULT"}]
        cur = _month_start(row["lo"])
        end = _next_month(_month_start(row["hi"]))  # one extra month past the newest row, for near-future inserts
        defs = []
        for _ in range(241):  # 20 years of months -- sanity cap against a bogus/garbage date range
            if cur > end:
                break
            nxt = _next_month(cur)
            defs.append({
                "name_suffix": cur.strftime("p%Y_%m"),
                "clause": f"FOR VALUES FROM ('{cur.isoformat()}') TO ('{nxt.isoformat()}')",
            })
            cur = nxt
        defs.append({"name_suffix": "default", "clause": "DEFAULT"})  # catches anything outside the computed range
        return defs
    elif technique == "LIST":
        rows = await conn.fetch(f"SELECT DISTINCT {col_q} AS v FROM {qualified} WHERE {col_q} IS NOT NULL LIMIT 100")
        defs = []
        for i, r in enumerate(rows):
            literal = await conn.fetchval("SELECT quote_literal($1::text)", r["v"])
            defs.append({"name_suffix": f"v{i}", "clause": f"FOR VALUES IN ({literal})"})
        defs.append({"name_suffix": "default", "clause": "DEFAULT"})  # any value seen after this snapshot was taken
        return defs
    else:  # HASH
        modulus = 4
        return [{"name_suffix": f"h{i}", "clause": f"FOR VALUES WITH (MODULUS {modulus}, REMAINDER {i})"} for i in range(modulus)]


async def convert_table_to_partitioned(
    pool: asyncpg.Pool, schema: str, table: str, technique: str, column: str, log,
) -> dict:
    """Actually converts schema.table to a partitioned table -- there is no
    native Postgres command for this, so it's: build a new partitioned
    table alongside the old one (indexes/constraints, real partitions
    sized from the actual data), then, inside one transaction with the old
    table locked against all reads and writes for the duration, copy every
    row across and swap names. The old table is renamed aside, never
    dropped, so a bad conversion is always recoverable by renaming it back.

    Call check_partitioning_prerequisites() first and refuse if it returns
    anything -- this trusts that's already been done for the expensive
    dependency checks, but re-verifies the table/column still exist and
    re-derives the key-column check as a cheap final guard against a race.

    `log(str)` is called with progress lines as they happen, for a live
    job log -- see app.partitioning, which is the only caller.
    """
    if technique not in ("RANGE", "LIST", "HASH"):
        raise PartitioningError(f"Unknown technique: {technique!r}")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind = 'r' AND NOT c.relispartition",
            schema, table,
        )
        if row is None:
            raise PartitioningError(f"{schema}.{table} doesn't exist, or isn't a plain (non-partitioned) table.")
        oid = row["oid"]

        ts = time.strftime("%Y%m%d%H%M%S")
        new_name = f"{table}__partitioned_{ts}"
        backup_name = f"{table}__pre_partition_{ts}"
        qualified_old = f"{_quote_ident(schema)}.{_quote_ident(table)}"
        qualified_new = f"{_quote_ident(schema)}.{_quote_ident(new_name)}"
        col_q = _quote_ident(column)

        log(f"Computing partitions for {schema}.{table} (PARTITION BY {technique} ({column}))...")
        partition_defs = await _compute_partition_defs(conn, schema, table, technique, column)
        log(f"Will create {len(partition_defs)} partition(s).")

        # ---- Phase 1: build the new table alongside the old one -- no
        # lock on the old table needed for any of this. ----
        log(f"Creating {new_name} (LIKE {table} INCLUDING DEFAULTS INCLUDING GENERATED) PARTITION BY {technique} ({column})...")
        await conn.execute(
            f"CREATE TABLE {qualified_new} (LIKE {qualified_old} INCLUDING DEFAULTS INCLUDING GENERATED) "
            f"PARTITION BY {technique} ({col_q})"
        )

        key_rows = await conn.fetch(
            """
            SELECT con.conname, con.contype::text AS contype, array_agg(att.attname ORDER BY u.ord) AS columns
            FROM pg_constraint con
            JOIN unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord) ON true
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = u.attnum
            WHERE con.conrelid = $1 AND con.contype IN ('p', 'u')
            GROUP BY con.conname, con.contype
            """, oid,
        )
        for k in key_rows:
            kind = "PRIMARY KEY" if k["contype"] == "p" else "UNIQUE"
            cols = list(k["columns"])
            if column not in cols:
                # The standard, well-documented way to partition an
                # existing table by a column that isn't already part of
                # its key (Postgres requires the partition column to be in
                # every unique/primary key on a partitioned table) --
                # already surfaced as a notice, not a blocker, by
                # check_partitioning_prerequisites.
                log(f"Widening {kind} ({', '.join(cols)}) to ({', '.join(cols)}, {column}) -- required for a partitioned table.")
                cols.append(column)
            else:
                log(f"Recreating {kind} ({', '.join(cols)})...")
            cols_sql = ", ".join(_quote_ident(c) for c in cols)
            new_conname = _quote_ident(f"{k['conname']}_{ts}")
            await conn.execute(f"ALTER TABLE {qualified_new} ADD CONSTRAINT {new_conname} {kind} ({cols_sql})")

        check_defs = await conn.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid = $1 AND contype = 'c'",
            oid,
        )
        for c in check_defs:
            log(f"Recreating CHECK constraint \"{c['conname']}\"...")
            await conn.execute(
                f"ALTER TABLE {qualified_new} ADD CONSTRAINT {_quote_ident(c['conname'] + '_' + ts)} {c['def']}"
            )

        fk_defs = await conn.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid = $1 AND contype = 'f'",
            oid,
        )
        for fk in fk_defs:
            log(f"Recreating foreign key \"{fk['conname']}\"...")
            await conn.execute(
                f"ALTER TABLE {qualified_new} ADD CONSTRAINT {_quote_ident(fk['conname'] + '_' + ts)} {fk['def']}"
            )

        skipped_indexes = await conn.fetch(
            """
            SELECT ic.relname AS index_name
            FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
            WHERE i.indrelid = $1 AND NOT i.indisunique AND NOT i.indisprimary
            """, oid,
        )

        for pdef in partition_defs:
            part_name = _quote_ident(f"{table}_{pdef['name_suffix']}")
            log(f"Creating partition {table}_{pdef['name_suffix']} ({pdef['clause']})...")
            await conn.execute(f"CREATE TABLE {_quote_ident(schema)}.{part_name} PARTITION OF {qualified_new} {pdef['clause']}")

        # ---- Phase 2: the only part that actually blocks the live table
        # -- lock, copy, verify, swap, all inside one transaction so
        # nothing can slip through between the count and the rename. ----
        async with conn.transaction():
            log(f"Acquiring an exclusive lock on {schema}.{table} -- this blocks ALL reads and writes on it until the copy below finishes...")
            await conn.execute(f"LOCK TABLE {qualified_old} IN ACCESS EXCLUSIVE MODE")
            old_count = await conn.fetchval(f"SELECT count(*) FROM {qualified_old}")
            log(f"Copying {old_count:,} row(s)...")
            copy_start = time.time()
            await conn.execute(f"INSERT INTO {qualified_new} OVERRIDING SYSTEM VALUE SELECT * FROM {qualified_old}")
            new_count = await conn.fetchval(f"SELECT count(*) FROM {qualified_new}")
            log(f"Copied {new_count:,} row(s) in {time.time() - copy_start:.1f}s.")
            if new_count != old_count:
                raise PartitioningError(
                    f"Row count mismatch after copy ({old_count} in the original, {new_count} in the new table) "
                    "-- aborting without renaming anything."
                )
            log(f"Renaming {table} -> {backup_name}, {new_name} -> {table}...")
            await conn.execute(f"ALTER TABLE {qualified_old} RENAME TO {_quote_ident(backup_name)}")
            await conn.execute(f"ALTER TABLE {qualified_new} RENAME TO {_quote_ident(table)}")

        log(f"Done. {schema}.{table} is now partitioned by {technique} ({column}); the original data is safely kept at {schema}.{backup_name} -- verify, then drop it yourself once you're satisfied.")
        if skipped_indexes:
            names = ", ".join(r["index_name"] for r in skipped_indexes)
            log(f"NOT automatically recreated (add these yourself if still needed): {names}")

        return {
            "backup_table": backup_name, "row_count": new_count,
            "skipped_indexes": [r["index_name"] for r in skipped_indexes],
        }


async def vacuum_table(
    pool: asyncpg.Pool,
    schema_name: str,
    table_name: str,
    analyze: bool = True,
    freeze: bool = False,
    full: bool = False,
) -> dict:
    """Runs a real VACUUM on one table, chosen from the bloat-estimate
    candidates in the Administration tab. VACUUM cannot run inside a
    transaction block, so this uses a plain execute() on a connection
    checked out directly from the pool (asyncpg doesn't wrap a bare
    execute() in an implicit transaction). FULL takes an ACCESS EXCLUSIVE
    lock -- blocking all reads and writes on the table for its duration --
    so it's opt-in only, never the default."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT c.relkind::text AS relkind
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relname = $2
            """,
            schema_name, table_name,
        )
        if row is None:
            raise UnsafeQueryError(f"Table {schema_name}.{table_name} does not exist.")
        if row["relkind"] not in ("r", "p"):
            raise UnsafeQueryError(f"{schema_name}.{table_name} is not a table.")

        qualified = f"{_quote_ident(schema_name)}.{_quote_ident(table_name)}"
        before = await conn.fetchrow(
            """
            SELECT n_live_tup, n_dead_tup, pg_total_relation_size($1::regclass) AS size_bytes
            FROM pg_stat_user_tables WHERE schemaname = $2 AND relname = $3
            """,
            qualified, schema_name, table_name,
        )

        options = []
        if full:
            options.append("FULL")
        if freeze:
            options.append("FREEZE")
        if analyze:
            options.append("ANALYZE")
        option_sql = f" ({', '.join(options)})" if options else ""
        sql = f"VACUUM{option_sql} {qualified}"

        # VACUUM can run for a while on a large/bloated table -- lift the
        # pooled connection's statement_timeout for just this call, then
        # always restore it so the connection doesn't leak a raised timeout
        # into whatever request reuses it next from the pool.
        # SET does not accept bind parameters in Postgres; prior_timeout is a
        # value Postgres itself just reported (e.g. "15s", "0"), not user
        # input, but it's still quoted as a string literal rather than
        # interpolated bare.
        prior_timeout = await conn.fetchval("SHOW statement_timeout")
        started = time.monotonic()
        try:
            await conn.execute("SET statement_timeout = 0")
            await conn.execute(sql)
        finally:
            await conn.execute(f"SET statement_timeout = '{prior_timeout}'")
        elapsed = time.monotonic() - started

        after = await conn.fetchrow(
            """
            SELECT n_live_tup, n_dead_tup, pg_total_relation_size($1::regclass) AS size_bytes
            FROM pg_stat_user_tables WHERE schemaname = $2 AND relname = $3
            """,
            qualified, schema_name, table_name,
        )

    return {
        "schema_name": schema_name,
        "table_name": table_name,
        "options": {"analyze": analyze, "freeze": freeze, "full": full},
        "elapsed_seconds": round(elapsed, 2),
        "before": dict(before) if before else None,
        "after": dict(after) if after else None,
    }


# --------------------------------------------------------------- performance
async def _io_cumulative(conn: asyncpg.Connection) -> dict | None:
    """Cumulative system-wide IO counters from pg_stat_io (PG16+). None on
    older servers or if the view is otherwise unavailable."""
    try:
        row = await conn.fetchrow(
            """
            SELECT coalesce(sum(reads), 0)::bigint  AS reads,
                   coalesce(sum(hits), 0)::bigint   AS hits,
                   coalesce(sum(writes), 0)::bigint AS writes,
                   (SELECT max(stats_reset) FROM pg_stat_io) AS stats_reset
            FROM pg_stat_io
            """
        )
        return dict(row) if row else None
    except Exception:
        return None


RUNNING_BUCKET = "Running (CPU)"


async def _wait_event_buckets(conn: asyncpg.Connection) -> dict:
    """Session counts grouped by wait_event_type, for sessions that are
    actually ACTIVE (running a query) right now -- Postgres's nearest
    equivalent to Oracle's wait-class breakdown, restricted the way Oracle's
    ASH restricts to active sessions. Idle connections are excluded: they
    always show wait_event_type='Client' (waiting for the app to send the
    next command), which is network idle time, not resource contention --
    including them just drowns out anything actually worth seeing. A session
    with no wait_event_type is genuinely running on CPU, not waiting on
    anything, bucketed as 'Running (CPU)'."""
    rows = await conn.fetch(
        f"""
        SELECT coalesce(wait_event_type, '{RUNNING_BUCKET}') AS bucket, count(*) AS n
        FROM pg_stat_activity
        WHERE datname = current_database() AND pid <> pg_backend_pid() AND state = 'active'
        GROUP BY bucket
        """
    )
    return {r["bucket"]: r["n"] for r in rows}


async def get_io_rate(pool: asyncpg.Pool) -> dict:
    """Current IO throughput (reads/writes/hits per sec), derived from the
    two most recent poller snapshots -- same technique as get_tps/get_wal_rate."""
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f'SELECT captured_at, metrics FROM "{schema}".snapshots ORDER BY captured_at DESC LIMIT 2'
            )
        except asyncpg.exceptions.UndefinedTableError:
            rows = []  # read-only standby -- no pgmon schema, see db._make_pool
    unavailable = {"available": False, "reads_per_sec": None, "hits_per_sec": None, "writes_per_sec": None}
    if len(rows) < 2:
        return unavailable
    newest, older = rows[0], rows[1]
    io_new, io_old = (newest["metrics"] or {}).get("io"), (older["metrics"] or {}).get("io")
    dt = (newest["captured_at"] - older["captured_at"]).total_seconds()
    if dt <= 0 or not io_new or not io_old:
        return {**unavailable, "reason": "pg_stat_io not available on this server (requires Postgres 16+)"}
    if io_new.get("stats_reset") != io_old.get("stats_reset"):
        return {**unavailable, "reason": "IO stats were reset between samples"}
    reads_delta = io_new["reads"] - io_old["reads"]
    hits_delta = io_new["hits"] - io_old["hits"]
    writes_delta = io_new["writes"] - io_old["writes"]
    if min(reads_delta, hits_delta, writes_delta) < 0:
        return {**unavailable, "reason": "IO counters decreased unexpectedly between samples"}
    return {
        "available": True,
        "reads_per_sec": round(reads_delta / dt, 2),
        "hits_per_sec": round(hits_delta / dt, 2),
        "writes_per_sec": round(writes_delta / dt, 2),
    }


async def _fetch_snapshot_rows(
    pool: asyncpg.Pool, hours: float, start: str | None = None, end: str | None = None
) -> list:
    """Snapshot rows for a history window -- either an explicit [start, end]
    (a user-picked custom range) or the last `hours` hours (a relative
    preset like the Real-Time/1H/6H/24H buttons). asyncpg needs a real
    datetime for a timestamptz parameter, not a string, even with an
    explicit ::timestamptz cast -- so ISO strings from the client are
    parsed here (raises ValueError on malformed input, caught by the
    caller and turned into a 400)."""
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        try:
            if start and end:
                start_dt = datetime.fromisoformat(start)
                end_dt = datetime.fromisoformat(end)
                return await conn.fetch(
                    f"""SELECT captured_at, metrics FROM "{schema}".snapshots
                        WHERE captured_at >= $1 AND captured_at <= $2
                        ORDER BY captured_at ASC""",
                    start_dt, end_dt,
                )
            return await conn.fetch(
                f"""SELECT captured_at, metrics FROM "{schema}".snapshots
                    WHERE captured_at > now() - ($1 || ' hours')::interval
                    ORDER BY captured_at ASC""",
                str(hours),
            )
        except asyncpg.exceptions.UndefinedTableError:
            return []  # read-only standby -- no pgmon schema, see db._make_pool


async def get_io_history(
    pool: asyncpg.Pool, hours: float = 1, start: str | None = None, end: str | None = None
) -> list[dict]:
    """Time series of IO rates, one point per consecutive pair of poller
    snapshots in the window (each point is that interval's rate, not a
    cumulative value)."""
    rows = await _fetch_snapshot_rows(pool, hours, start, end)
    result = []
    prev = None
    for r in rows:
        io = (r["metrics"] or {}).get("io")
        if prev is not None and io and prev["io"] and io.get("stats_reset") == prev["io"].get("stats_reset"):
            dt = (r["captured_at"] - prev["captured_at"]).total_seconds()
            if dt > 0:
                reads_delta = io["reads"] - prev["io"]["reads"]
                hits_delta = io["hits"] - prev["io"]["hits"]
                writes_delta = io["writes"] - prev["io"]["writes"]
                if min(reads_delta, hits_delta, writes_delta) >= 0:
                    result.append({
                        "captured_at": r["captured_at"],
                        "reads_per_sec": round(reads_delta / dt, 2),
                        "hits_per_sec": round(hits_delta / dt, 2),
                        "writes_per_sec": round(writes_delta / dt, 2),
                    })
        prev = {"captured_at": r["captured_at"], "io": io}
    return result


async def get_wait_events(pool: asyncpg.Pool) -> dict:
    # buckets (per wait_event_type) and detail (per wait_event_type +
    # wait_event) both come from this ONE query rather than two separate
    # ones -- pg_stat_activity is a live, constantly-changing view, and two
    # sequential snapshots microseconds apart (this app's own poller
    # connections flicker in and out between them) can disagree, which
    # showed up as the summary count not matching the detail table's own
    # row counts. Deriving buckets from the already-fetched detail rows in
    # Python guarantees they always add up, by construction.
    async with pool.acquire() as conn:
        detail_rows = await conn.fetch(
            f"""
            SELECT
                coalesce(wait_event_type, '{RUNNING_BUCKET}') AS wait_event_type,
                coalesce(wait_event, '(none -- actively executing)') AS wait_event,
                count(*) AS n,
                max(EXTRACT(EPOCH FROM (now() - state_change)))::float AS max_seconds,
                (array_agg(left(query, 200) ORDER BY state_change ASC))[1] AS sample_query
            FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid() AND state = 'active'
            GROUP BY 1, 2
            ORDER BY n DESC, max_seconds DESC
            """
        )
    buckets: dict[str, int] = {}
    for r in detail_rows:
        buckets[r["wait_event_type"]] = buckets.get(r["wait_event_type"], 0) + r["n"]
    return {
        "buckets": buckets,
        "total": sum(buckets.values()),
        "detail": [dict(r) for r in detail_rows],
    }


async def get_wait_event_history(
    pool: asyncpg.Pool, hours: float = 1, start: str | None = None, end: str | None = None
) -> list[dict]:
    rows = await _fetch_snapshot_rows(pool, hours, start, end)
    return [{"captured_at": r["captured_at"], "buckets": (r["metrics"] or {}).get("wait_events") or {}} for r in rows]


async def get_memory_info(pool: asyncpg.Pool) -> dict:
    setting_names = [
        "shared_buffers", "effective_cache_size", "work_mem",
        "maintenance_work_mem", "wal_buffers", "temp_buffers",
    ]
    async with pool.acquire() as conn:
        setting_rows = await conn.fetch(
            "SELECT name, setting, unit FROM pg_settings WHERE name = ANY($1)", setting_names
        )
        db_stat = await conn.fetchrow(
            "SELECT blks_hit, blks_read, temp_files, temp_bytes FROM pg_stat_database WHERE datname = current_database()"
        )
        buffercache = None
        buffercache_error = None
        try:
            row = await conn.fetchrow(
                """
                SELECT count(*)::bigint AS total_buffers,
                       count(*) FILTER (WHERE relfilenode IS NOT NULL)::bigint AS used_buffers
                FROM pg_buffercache
                """
            )
            buffercache = dict(row) if row else None
        except Exception as e:
            buffercache_error = (
                f"{e} (requires CREATE EXTENSION pg_buffercache, a contrib module)"
            )

    cache_hit_ratio = None
    if db_stat and (db_stat["blks_hit"] + db_stat["blks_read"]) > 0:
        cache_hit_ratio = round(100.0 * db_stat["blks_hit"] / (db_stat["blks_hit"] + db_stat["blks_read"]), 2)

    return {
        "settings": {r["name"]: {"value": r["setting"], "unit": r["unit"]} for r in setting_rows},
        "cache_hit_ratio_pct": cache_hit_ratio,
        "temp_files": db_stat["temp_files"] if db_stat else None,
        "temp_bytes": db_stat["temp_bytes"] if db_stat else None,
        "buffercache": buffercache,
        "buffercache_error": buffercache_error,
    }


async def get_performance_overview(pool: asyncpg.Pool) -> dict:
    """Everything for the Performance Hub tab, bundled -- also fed to
    Claude for the AI Performance Review."""
    io_rate = await get_io_rate(pool)
    wait_events = await get_wait_events(pool)
    memory = await get_memory_info(pool)
    return {"io_rate": io_rate, "wait_events": wait_events, "memory": memory}


async def build_snapshot(pool: asyncpg.Pool) -> dict:
    """Compact metrics bundle used both for history storage and AI context."""
    overview = await get_overview(pool)
    connections = await get_connections(pool)
    tables = await get_table_stats(pool)
    locks = await get_locks(pool)
    top_queries = await get_top_queries(pool, limit=10)
    async with pool.acquire() as conn:
        wal = await _wal_cumulative(conn)
        io = await _io_cumulative(conn)
        wait_events = await _wait_event_buckets(conn)

    long_running = [c for c in connections if c["state"] == "active" and (c["query_seconds"] or 0) > 5]

    return {
        "overview": overview,
        "wal": wal,
        "io": io,
        "wait_events": wait_events,
        "connection_count": len(connections),
        "long_running_count": len(long_running),
        "long_running_queries": long_running[:5],
        "lock_wait_count": len(locks),
        "top_tables_by_size": tables[:5],
        "tables_high_dead_tuples": [t for t in tables if t["dead_tuple_pct"] and t["dead_tuple_pct"] > 20][:5],
        "top_queries": top_queries.get("queries", [])[:5],
    }


async def save_snapshot(pool: asyncpg.Pool, metrics: dict) -> None:
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        await conn.execute(
            f'INSERT INTO "{schema}".snapshots (metrics) VALUES ($1::jsonb)',
            metrics,
        )


async def get_history(pool: asyncpg.Pool, hours: int = 6) -> list[dict]:
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f"""
                SELECT captured_at, metrics
                FROM "{schema}".snapshots
                WHERE captured_at > now() - ($1 || ' hours')::interval
                ORDER BY captured_at ASC
                """,
                str(hours),
            )
        except asyncpg.exceptions.UndefinedTableError:
            # A read-only standby never got its own pgmon schema bootstrapped
            # (see db._make_pool) -- no history to show, not an error.
            return []
    return [{"captured_at": r["captured_at"], "metrics": r["metrics"]} for r in rows]


_SAFE_EXTENSION_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


def _validate_catalog_extension(name: str) -> str:
    """Every extension name this module will ever run CREATE/DROP EXTENSION
    against must come from extension_catalog.CATALOG, not raw user input --
    this is the actual enforcement point, not just a naming-convention
    check, since the name is interpolated into DDL below."""
    if not _SAFE_EXTENSION_NAME.match(name or "") or extension_catalog.get(name) is None:
        raise UnsafeQueryError(f"{name!r} is not in the extension catalog.")
    return name


async def get_extension_catalog_status(pool: asyncpg.Pool) -> list[dict]:
    """Live availability/installed status for every extension in the
    catalog, against this specific registered database -- the same catalog
    entry can be available on one server and not another (e.g. hypopg/
    pg_repack only exist on a Postgres image that had them added)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, default_version, installed_version FROM pg_available_extensions"
        )
    live = {r["name"]: r for r in rows}
    result = []
    for entry in extension_catalog.CATALOG:
        row = live.get(entry["name"])
        installed_version = row["installed_version"] if row else None
        default_version = row["default_version"] if row else None
        result.append({
            **entry,
            "available": row is not None,
            "installed": bool(installed_version),
            "installed_version": installed_version,
            "default_version": default_version,
            "update_available": bool(installed_version and default_version and installed_version != default_version),
        })
    return result


async def enable_extension(pool: asyncpg.Pool, name: str) -> None:
    _validate_catalog_extension(name)
    if name == "pg_stat_statements":
        # A plain CREATE EXTENSION here would "succeed" (the SQL objects get
        # created) even when shared_preload_libraries doesn't include it --
        # the module just silently can't collect anything, and every query
        # against the view then errors with "pg_stat_statements must be
        # loaded via shared_preload_libraries". install_pg_stat_statements
        # checks that precondition first, so the Extensions tab reports the
        # same accurate success/failure as the SQL Performance tab's own
        # install button, instead of a misleading "enabled".
        await install_pg_stat_statements(pool)
        return
    async with pool.acquire() as conn:
        available = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = $1)", name
        )
        if not available:
            raise UnsafeQueryError(
                f"{name!r} isn't available on this server -- its control file doesn't exist here, "
                "so CREATE EXTENSION can't succeed regardless of privileges. It needs to be added to "
                "this specific Postgres server's image first (see postgres-image/Dockerfile for the "
                "pattern this app already uses for hypopg/pg_repack)."
            )
        try:
            await conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{name}"')
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"CREATE EXTENSION {name} failed: {e}")


async def update_extension(pool: asyncpg.Pool, name: str) -> dict:
    """ALTER EXTENSION ... UPDATE -- upgrades an *already-enabled* extension
    in this database to the newest version whose SQL files already exist
    on this Postgres server (pg_available_extensions.default_version), no
    target version specified (a bare UPDATE already means "to the
    default/latest available" per Postgres). This is NOT fetching
    anything from the internet -- that's not something a SQL connection
    can do at all, full stop: CREATE/ALTER EXTENSION can only activate a
    version whose files are already sitting on that specific server's
    filesystem, baked in at image-build time. This reaches whatever's
    already there, nothing more."""
    _validate_catalog_extension(name)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT installed_version, default_version FROM pg_available_extensions WHERE name = $1", name
        )
        if row is None or not row["installed_version"]:
            raise UnsafeQueryError(f"{name!r} isn't currently enabled on this database -- nothing to update.")
        if row["installed_version"] == row["default_version"]:
            return {"updated": False, "from_version": row["installed_version"], "to_version": row["default_version"]}
        try:
            await conn.execute(f'ALTER EXTENSION "{name}" UPDATE')
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"ALTER EXTENSION {name} UPDATE failed: {e}")
        new_version = await conn.fetchval(
            "SELECT installed_version FROM pg_available_extensions WHERE name = $1", name
        )
        return {"updated": True, "from_version": row["installed_version"], "to_version": new_version}


async def disable_extension(pool: asyncpg.Pool, name: str) -> None:
    _validate_catalog_extension(name)
    async with pool.acquire() as conn:
        try:
            await conn.execute(f'DROP EXTENSION IF EXISTS "{name}"')
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(
                f"DROP EXTENSION {name} failed: {e} (if other objects depend on it -- an index using "
                f"pg_trgm, a column using hstore, etc. -- they need to be removed first)"
            )


async def get_security_info(pool: asyncpg.Pool) -> dict:
    # Full role listing (attributes, memberships, active connections) lives
    # in app.roles -- reused here so the Security tab's Roles table gets the
    # same richer data that backs the Security tab's role-management UI.
    roles = await roles_mod.list_roles(pool)

    async with pool.acquire() as conn:
        ssl_setting = await conn.fetchval("SHOW ssl")
        password_encryption = await conn.fetchval("SHOW password_encryption")
        ssl_conns = await conn.fetch(
            """
            SELECT s.ssl, count(*) AS n
            FROM pg_stat_ssl s JOIN pg_stat_activity a ON a.pid = s.pid
            WHERE a.datname = current_database()
            GROUP BY s.ssl
            """
        )
        extensions = await conn.fetch("SELECT extname, extversion FROM pg_extension ORDER BY extname")

        hba_rules, hba_error = None, None
        try:
            hba_rows = await conn.fetch(
                """
                SELECT line_number, type, database, user_name, address, auth_method, error
                FROM pg_hba_file_rules ORDER BY line_number
                """
            )
            hba_rules = [dict(r) for r in hba_rows]
        except Exception as e:
            hba_error = (
                f"{e} (pg_hba_file_rules requires superuser / pg_read_all_settings, "
                "or may be restricted by your hosting provider)"
            )

    return {
        "ssl_enabled": ssl_setting == "on",
        "password_encryption": password_encryption,
        "roles": roles,
        "role_count": len(roles),
        "superuser_count": sum(1 for r in roles if r["rolsuper"]),
        "ssl_connection_breakdown": [dict(r) for r in ssl_conns],
        "extensions": [dict(r) for r in extensions],
        "hba_rules": hba_rules,
        "hba_rules_error": hba_error,
    }


# scram-sha-256/md5/cert only -- trust and password are exactly what this
# app's own get_security_info/alerts.py already flags as an "Insecure
# pg_hba.conf authentication rule" (see the check right above in alerts.py),
# so this add-a-rule feature refuses to let itself be used to create one.
_HBA_ALLOWED_TYPES = ("host", "hostssl", "local")
_HBA_ALLOWED_AUTH_METHODS = ("scram-sha-256", "md5", "cert")


def _hba_field(value: str, field_name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise UnsafeQueryError(f"{field_name} is required.")
    if any(c.isspace() for c in value) or "'" in value or "#" in value:
        raise UnsafeQueryError(f"{field_name} can't contain whitespace, quotes, or '#'.")
    return value


async def add_hba_rule(
    pool: asyncpg.Pool, conn_type: str, database: str, user_name: str, address: str, auth_method: str,
) -> dict:
    """Appends one line to this server's pg_hba.conf and reloads it -- e.g.
    the "host replication <user> <cidr> scram-sha-256" rule a fresh Postgres
    instance needs before pg_basebackup (physical backups, and the
    replica-registration flow) can connect at all; Postgres refuses those
    with "no pg_hba.conf entry for replication connection" until one exists,
    and there's no SQL-level equivalent of ALTER SYSTEM for pg_hba rules.

    Uses `COPY ... TO PROGRAM` (a core superuser capability, no extension
    needed -- pgmon already connects as the Postgres superuser) to append
    the line server-side, then pg_reload_conf() to apply it immediately,
    no restart required. Always appends at the end: pg_hba.conf is
    evaluated top-to-bottom and first-match-wins, so a brand new rule here
    only ever adds coverage, never silently overrides an earlier one.
    Editing/removing an *existing* line is delete_hba_rule/edit_hba_rule
    below -- a targeted single-line operation, not a rewrite of the whole
    file (which is the thing worth avoiding; see delete_hba_rule's
    docstring for why a surgical line-level change doesn't carry that
    same risk).
    """
    conn_type = _hba_field(conn_type, "Type")
    if conn_type not in _HBA_ALLOWED_TYPES:
        raise UnsafeQueryError(f"Type must be one of: {', '.join(_HBA_ALLOWED_TYPES)}.")
    auth_method = _hba_field(auth_method, "Auth method")
    if auth_method not in _HBA_ALLOWED_AUTH_METHODS:
        raise UnsafeQueryError(
            f"Auth method must be one of: {', '.join(_HBA_ALLOWED_AUTH_METHODS)} -- "
            "'trust' and 'password' are refused here since they're exactly what this app's "
            "own Security tab flags as an insecure pg_hba rule."
        )
    database = _hba_field(database or "all", "Database")
    user_name = _hba_field(user_name or "all", "User")

    # Space-, not tab-, separated: pg_hba.conf's parser accepts either as a
    # field separator, but COPY's text output format (used below to append
    # this line) backslash-escapes literal tabs -- its own column delimiter
    # -- corrupting the line into one field containing literal "\t" text.
    if conn_type == "local":
        line = f"{conn_type} {database} {user_name} {auth_method}"
    else:
        address = _hba_field(address, "Address")
        line = f"{conn_type} {database} {user_name} {address} {auth_method}"

    async with pool.acquire() as conn:
        hba_file = await conn.fetchval("SHOW hba_file")
        escaped_line = line.replace("'", "''")  # belt-and-suspenders; _hba_field already rejects quotes
        try:
            await conn.execute(f"COPY (SELECT '{escaped_line}') TO PROGRAM 'cat >> {hba_file}'")
            await conn.execute("SELECT pg_reload_conf()")
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"Failed to update pg_hba.conf: {e}")
        # Confirm it actually parsed -- Postgres logs a rule error rather than
        # failing the reload outright, so "reload succeeded" alone doesn't
        # mean the new line is actually in effect. Since this always appends
        # at the end of the file, the newly-added rule is simply whichever
        # line now has the highest line_number -- no need to match it back
        # by field (address in particular round-trips oddly: it's split off
        # from the netmask into its own column, so a CIDR like "x/16" won't
        # string-compare equal to what pg_hba_file_rules reports back).
        added = await conn.fetchrow(
            "SELECT line_number, error FROM pg_hba_file_rules ORDER BY line_number DESC LIMIT 1"
        )
    if added is None:
        return {"ok": False, "line": line, "error": "Rule was appended but pg_hba_file_rules came back empty after reload."}
    return {"ok": added["error"] is None, "line": line, "line_number": added["line_number"], "error": added["error"]}


def _check_hba_rule_unchanged(current: asyncpg.Record, expected: dict, line_number: int) -> None:
    """Guards against editing/deleting the wrong rule if pg_hba.conf
    changed between when the UI loaded it and when the action was
    submitted (another admin's concurrent edit, or line numbers having
    shifted) -- re-checked server-side immediately before the file is
    touched, not trusted from the client alone."""
    current_db = sorted(current["database"] or [])
    current_user = sorted(current["user_name"] or [])
    expected_db = sorted(expected.get("database") or [])
    expected_user = sorted(expected.get("user_name") or [])
    if (
        current["type"] != expected.get("type")
        or current_db != expected_db
        or current_user != expected_user
        or (current["address"] or None) != (expected.get("address") or None)
        or current["auth_method"] != expected.get("auth_method")
    ):
        raise UnsafeQueryError(
            f"The rule at line {line_number} has changed since it was loaded -- refresh the Security tab "
            "and try again, to avoid editing/deleting the wrong rule."
        )


async def delete_hba_rule(pool: asyncpg.Pool, line_number: int, expected: dict) -> dict:
    """Removes exactly one line from pg_hba.conf by line number, then
    reloads. A targeted `sed -i 'Nd'` against a line pg_hba_file_rules has
    already parsed and numbered -- not a read-the-whole-file-and-rewrite-it,
    which is what add_hba_rule's own docstring originally avoided (getting
    a full rewrite wrong is how you corrupt the file). Deleting exactly
    one already-identified line carries the same risk profile as the
    append path already in production use above, not the risk profile
    that avoided a rewrite.

    Safe even if the result is briefly invalid: pg_reload_conf() against a
    syntactically broken pg_hba.conf logs an error and keeps the
    *previous* configuration in effect rather than crashing Postgres or
    dropping existing connections -- that's what makes doing this via
    reload (never a restart) safe to offer at all. It does NOT protect
    against legitimately locking out *future* connections that matched
    the rule being removed (including this app's own, if that's the one
    deleted) -- the caller is expected to confirm that risk with the
    operator before calling this, not this function's job to guess at.
    """
    if not isinstance(line_number, int) or line_number < 1:
        raise UnsafeQueryError("Invalid line number.")
    async with pool.acquire() as conn:
        hba_file = await conn.fetchval("SHOW hba_file")
        current = await conn.fetchrow(
            "SELECT type, database, user_name, address, auth_method FROM pg_hba_file_rules WHERE line_number = $1",
            line_number,
        )
        if current is None:
            raise UnsafeQueryError(f"No pg_hba.conf rule at line {line_number} -- it may have already been removed.")
        _check_hba_rule_unchanged(current, expected, line_number)
        try:
            await conn.execute(f"COPY (SELECT 1) TO PROGRAM 'sed -i {line_number}d {hba_file}'")
            await conn.execute("SELECT pg_reload_conf()")
        except asyncpg.PostgresError as e:
            raise UnsafeQueryError(f"Failed to update pg_hba.conf: {e}")
    return {"ok": True, "deleted_line_number": line_number}


async def edit_hba_rule(
    pool: asyncpg.Pool, line_number: int, expected: dict,
    conn_type: str, database: str, user_name: str, address: str, auth_method: str,
) -> dict:
    """Changes one existing rule's fields. Implemented as add-the-new-line
    (reusing add_hba_rule's own validation and append-then-verify path)
    followed by delete-the-old-line (delete_hba_rule above), deliberately
    in that order: a failure partway through then leaves a harmless
    *extra* rule rather than a *gap* in coverage -- losing a rule nobody
    meant to remove is worse than a redundant one sitting around. There's
    a narrow window between the two steps where both old and new rules
    are in effect; harmless, since pg_hba.conf is first-match-wins and
    this only adds permissiveness during that window, never removes it.
    """
    added = await add_hba_rule(pool, conn_type, database, user_name, address, auth_method)
    if not added["ok"]:
        raise UnsafeQueryError(f"New rule didn't parse -- left the existing one unchanged: {added.get('error')}")
    deleted = await delete_hba_rule(pool, line_number, expected)
    return {"ok": True, "added_line_number": added["line_number"], "deleted_line_number": deleted["deleted_line_number"]}


async def ensure_replication_hba_rule(pool: asyncpg.Pool, rolname: str) -> str:
    """Best-effort prerequisite for physical/incremental backups (and thus
    for WAL mining, PITR, etc.): pg_basebackup needs an explicit
    'replication' pg_hba entry -- the 'all' keyword does not cover it per
    Postgres's own docs, regardless of what else is already permitted.
    Checks for an existing rule covering this role first (so registering
    the same server twice, or one that's already set up, doesn't pile up
    duplicate lines), and matches the auth method to the server's own
    password_encryption so it actually verifies against however this
    role's password is already hashed. Never raises -- adding this is a
    convenience on top of a successful "add database", not a reason to
    fail it."""
    try:
        async with pool.acquire() as conn:
            covered = await conn.fetchval(
                "SELECT count(*) > 0 FROM pg_hba_file_rules WHERE type IN ('host', 'hostssl') "
                "AND error IS NULL AND 'replication' = ANY(database) "
                "AND ('all' = ANY(user_name) OR $1 = ANY(user_name))",
                rolname,
            )
            if covered:
                return f"A pg_hba rule already allows {rolname!r} to open replication connections."
            encryption = await conn.fetchval("SHOW password_encryption")
        auth_method = "scram-sha-256" if encryption == "scram-sha-256" else "md5"
        await add_hba_rule(pool, "host", "replication", rolname, "0.0.0.0/0", auth_method)
        return f"Added a pg_hba rule allowing {rolname!r} to open replication connections ({auth_method})."
    except Exception as e:
        return f"Couldn't add a replication pg_hba rule automatically: {e}"


async def get_backup_info(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        archive_mode = await conn.fetchval("SHOW archive_mode")
        wal_level = await conn.fetchval("SHOW wal_level")
        archive_command = await conn.fetchval("SHOW archive_command")

        checkpoint, checkpoint_error = None, None
        try:
            row = await conn.fetchrow("SELECT * FROM pg_control_checkpoint()")
            checkpoint = dict(row) if row else None
        except Exception as e:
            checkpoint_error = str(e)

        archiver = None
        try:
            row = await conn.fetchrow("SELECT * FROM pg_stat_archiver")
            archiver = dict(row) if row else None
        except Exception:
            pass

        # byte lag (how far behind, in WAL bytes) alongside the time-based
        # replay_lag Postgres already tracks -- the two disagree in exactly
        # the case worth flagging: a replica that's caught up on wall-clock
        # time because it's been idle, but is actually many bytes behind
        # because it stopped applying WAL (replay_lag stays at its last
        # measured value between actual replay events, not a live clock).
        replicas = await conn.fetch(
            """
            SELECT
                client_addr, application_name, state, sync_state,
                sent_lsn, write_lsn, flush_lsn, replay_lsn,
                pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes,
                write_lag, flush_lag, replay_lag
            FROM pg_stat_replication
            """
        )

    return {
        "archive_mode": archive_mode,
        "wal_level": wal_level,
        "archive_command": archive_command or None,
        "checkpoint": checkpoint,
        "checkpoint_error": checkpoint_error,
        "archiver": archiver,
        "replicas": [dict(r) for r in replicas],
    }


class UnsafeQueryError(Exception):
    pass


def _assert_readonly_single_statement(query: str) -> str:
    stripped = query.strip()
    if not stripped:
        raise UnsafeQueryError("Query is empty.")
    # Allow exactly one optional trailing semicolon; reject multi-statement input outright.
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        raise UnsafeQueryError("Only a single statement is allowed (no semicolons inside the query).")
    first_word = body.lstrip().split(None, 1)[0].upper() if body.lstrip() else ""
    if first_word not in ("SELECT", "WITH"):
        raise UnsafeQueryError(
            "Only read-only SELECT / WITH queries can be run through EXPLAIN ANALYZE here."
        )
    return body


async def run_explain(pool: asyncpg.Pool, query: str) -> dict:
    """EXPLAIN (ANALYZE, BUFFERS) a read-only query. ANALYZE actually executes
    the query, so we hard-restrict this to single SELECT/WITH statements."""
    safe_query = _assert_readonly_single_statement(query)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Read-only + short statement timeout as defense in depth beyond the
            # SELECT/WITH guard above (e.g. against expensive runaway scans).
            await conn.execute("SET LOCAL transaction_read_only = on")
            await conn.execute("SET LOCAL statement_timeout = '10s'")
            plan_rows = await conn.fetch(
                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {safe_query}"
            )
    raw = plan_rows[0][0] if plan_rows else "[]"
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return {"query": safe_query, "plan": plan}


async def prune_history(pool: asyncpg.Pool, retention_hours: int) -> None:
    schema = settings.MONITOR_SCHEMA
    async with pool.acquire() as conn:
        await conn.execute(
            f"""DELETE FROM "{schema}".snapshots
                WHERE captured_at < now() - ($1 || ' hours')::interval""",
            str(retention_hours),
        )
