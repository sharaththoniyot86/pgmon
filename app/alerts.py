"""Rule-based health alerts for the Alerts tab.

Deterministic threshold checks over the same live metrics already
gathered elsewhere in this app (pg_stat_activity, pg_stat_user_tables,
pg_hba_file_rules, pg_stat_archiver, ...) -- not an AI call, so this is
always available (no ANTHROPIC_API_KEY needed) and its output is
reproducible run to run. The AI health/security/backup/performance
reviews elsewhere in the app give a narrative second opinion on the same
kind of data; this tab is the fast, always-on, no-API-key first line --
same relationship as app/tuning.py has to the AI features.

Severities:
  error   -- needs attention now (exhausted resources, disabled safety
             nets, insecure auth).
  warning -- worth a look soon (trending toward a problem).
  info    -- worth knowing, not necessarily wrong (e.g. SSL off on a
             local/dev instance is often an intentional choice).
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncpg

from app import backup_schedule, metrics

# Long-running / idle-in-transaction session thresholds, in seconds.
_QUERY_WARN_SECONDS = 300
_QUERY_ERROR_SECONDS = 1800
_IDLE_TXN_WARN_SECONDS = 300
_IDLE_TXN_ERROR_SECONDS = 1800

_CONN_USAGE_WARN = 0.75
_CONN_USAGE_ERROR = 0.90
_CACHE_HIT_WARN = 90
_CACHE_HIT_ERROR = 80
_DEAD_TUPLE_WARN_PCT = 20
_DEAD_TUPLE_ERROR_PCT = 40
_BLOCKED_SESSIONS_ERROR = 5
_CHECKPOINT_MIN_SAMPLE = 10
_CHECKPOINT_FORCED_RATIO_WARN = 0.5

# Replication slot WAL retention. A slot legitimately holds some WAL while
# its consumer catches up, so size alone isn't the signal -- an *inactive*
# slot holding anything meaningful is, since nothing will ever release it.
_SLOT_RETAINED_WARN_BYTES = 1 * 1024 ** 3    # 1 GB
_SLOT_RETAINED_ERROR_BYTES = 10 * 1024 ** 3  # 10 GB
# Standby lag. Bytes rather than seconds as the primary signal: Postgres's
# own *_lag intervals stay frozen at their last measured value while a
# standby is idle, so a standby that stopped applying WAL entirely can show
# a reassuring small time lag while falling arbitrarily far behind.
_REPLICA_LAG_WARN_BYTES = 100 * 1024 ** 2    # 100 MB
_REPLICA_LAG_ERROR_BYTES = 1 * 1024 ** 3     # 1 GB


def _fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def _alert(severity: str, title: str, detail: str, category: str) -> dict:
    return {"severity": severity, "title": title, "detail": detail, "category": category}


async def get_alerts(pool: asyncpg.Pool, db_id: str | None = None) -> list[dict]:
    alerts: list[dict] = []

    # ---------------------------------------------------- pending restarts
    # Surfaces exactly what the "Edit..." modal's own per-setting warning
    # already knows (pg_settings.pending_restart) -- e.g. after applying an
    # Optimize-database recommendation for a postmaster-context parameter
    # like shared_buffers, the new value is written to postgresql.auto.conf
    # immediately but the running server keeps the old value until it's
    # actually restarted. Without this, that's easy to miss.
    settings_data = await metrics.get_settings(pool)
    pending = [s["name"] for s in settings_data["settings"] if s.get("pending_restart")]
    if pending:
        alerts.append(_alert("warning", "Server restart required to apply changed settings",
            f"{len(pending)} parameter(s) have a new value waiting in postgresql.auto.conf but still need a full "
            f"Postgres restart to take effect: {', '.join(pending)}.", "configuration"))

    # ---------------------------------------------------------- connections
    overview = await metrics.get_overview(pool)
    conns = overview.get("connections") or {}
    total_conn, max_conn = conns.get("total") or 0, overview.get("max_connections") or 0
    if max_conn:
        ratio = total_conn / max_conn
        if ratio >= _CONN_USAGE_ERROR:
            alerts.append(_alert("error", "Connection pool nearly exhausted",
                f"{total_conn} of {max_conn} connections in use ({round(ratio * 100)}%).", "connections"))
        elif ratio >= _CONN_USAGE_WARN:
            alerts.append(_alert("warning", "Connection usage is high",
                f"{total_conn} of {max_conn} connections in use ({round(ratio * 100)}%).", "connections"))

    cache_hit = overview.get("cache_hit_ratio_pct")
    if cache_hit is not None:
        if cache_hit < _CACHE_HIT_ERROR:
            alerts.append(_alert("error", "Cache hit ratio is low",
                f"Only {cache_hit}% of reads are served from cache -- shared_buffers may be too small for this workload.", "performance"))
        elif cache_hit < _CACHE_HIT_WARN:
            alerts.append(_alert("warning", "Cache hit ratio could be better",
                f"{cache_hit}% of reads are served from cache.", "performance"))

    db_stat = overview.get("db_stat") or {}
    if (db_stat.get("deadlocks") or 0) > 0:
        alerts.append(_alert("warning", "Deadlocks detected",
            f"{db_stat['deadlocks']} deadlock(s) recorded since stats were last reset.", "connections"))
    if (db_stat.get("temp_files") or 0) > 0:
        temp_mb = round((db_stat.get("temp_bytes") or 0) / 1024 / 1024, 1)
        alerts.append(_alert("info", "Queries are spilling to temp files",
            f"{db_stat['temp_files']} temp file(s) created ({temp_mb}MB) -- work_mem may be too small for some sorts/hashes.", "performance"))

    for c in await metrics.get_connections(pool):
        state = c.get("state") or ""
        query_s, idle_s = c.get("query_seconds") or 0, c.get("state_seconds") or 0
        query_text = c.get("query") or "(no query text)"
        if state == "active" and query_s > _QUERY_ERROR_SECONDS:
            alerts.append(_alert("error", f"Long-running query (PID {c['pid']})",
                f"Running for {round(query_s / 60)} min: {query_text}", "connections"))
        elif state == "active" and query_s > _QUERY_WARN_SECONDS:
            alerts.append(_alert("warning", f"Long-running query (PID {c['pid']})",
                f"Running for {round(query_s)}s: {query_text}", "connections"))
        elif state == "idle in transaction" and idle_s > _IDLE_TXN_ERROR_SECONDS:
            alerts.append(_alert("error", f"Idle-in-transaction session (PID {c['pid']})",
                f"Idle in transaction for {round(idle_s / 60)} min -- holds locks and blocks vacuum from reclaiming space.", "connections"))
        elif state == "idle in transaction" and idle_s > _IDLE_TXN_WARN_SECONDS:
            alerts.append(_alert("warning", f"Idle-in-transaction session (PID {c['pid']})",
                f"Idle in transaction for {round(idle_s)}s.", "connections"))

    # ---------------------------------------------------------------- locks
    locks = await metrics.get_locks(pool)
    if locks:
        blocked_pids = {lk["blocked_pid"] for lk in locks}
        severity = "error" if len(blocked_pids) > _BLOCKED_SESSIONS_ERROR else "warning"
        alerts.append(_alert(severity, "Sessions blocked on locks",
            f"{len(blocked_pids)} session(s) currently waiting on a lock held by another session.", "locks"))

    # ---------------------------------------------------------- maintenance
    av = await metrics.get_autovacuum_info(pool)
    if (av.get("settings") or {}).get("autovacuum") == "off":
        alerts.append(_alert("error", "Autovacuum is disabled",
            "Dead tuples will accumulate without bound until a manual VACUUM is run.", "maintenance"))
    for t in av.get("tables") or []:
        pct = t.get("dead_tuple_pct") or 0
        label = f"{t['schema_name']}.{t['table_name']}"
        if pct >= _DEAD_TUPLE_ERROR_PCT:
            alerts.append(_alert("error", f"{label} needs vacuuming",
                f"{pct}% dead tuples ({t['n_dead_tup']} of {t['n_live_tup'] + t['n_dead_tup']} rows).", "maintenance"))
        elif pct >= _DEAD_TUPLE_WARN_PCT:
            alerts.append(_alert("warning", f"{label} has notable bloat",
                f"{pct}% dead tuples.", "maintenance"))

    # ------------------------------------------------- XID/MultiXact wraparound
    # Deliberately ahead of the other maintenance checks in severity terms:
    # everything else here degrades performance, this one eventually stops
    # the database accepting writes at all, and the recovery (an offline
    # VACUUM over the oldest table) gets slower the longer it's left.
    try:
        wrap = await metrics.get_wraparound_info(pool)
    except Exception:
        wrap = None
    if wrap:
        used_pct = wrap["xid_used_pct"]
        remaining = wrap["xid_remaining"]
        if used_pct >= metrics._XID_ERROR_FRACTION * 100:
            alerts.append(_alert("error", "Transaction ID wraparound is close",
                f"The oldest unfrozen transaction ID is {used_pct}% of the way to the {wrap['xid_limit']:,} "
                f"wraparound limit ({remaining:,} left). Postgres stops accepting writes entirely when this "
                "runs out. Find what's holding the horizon back (long-running transaction, abandoned "
                "replication slot, stale prepared transaction, or a table with autovacuum disabled) and "
                "VACUUM FREEZE the oldest tables now.", "maintenance"))
        elif used_pct >= metrics._XID_WARN_FRACTION * 100:
            alerts.append(_alert("warning", "Transaction ID age is high",
                f"The oldest unfrozen transaction ID is {used_pct}% of the way to the wraparound limit "
                f"({remaining:,} transactions left). Autovacuum's freeze pass should be bringing this down -- "
                "if it isn't, something is pinning the horizon.", "maintenance"))

        mxid_pct = wrap["mxid_used_pct"]
        if mxid_pct >= metrics._XID_ERROR_FRACTION * 100:
            alerts.append(_alert("error", "MultiXact ID wraparound is close",
                f"MultiXact age is {mxid_pct}% of its own separate wraparound limit. This counter is "
                "independent of the transaction ID one and shuts down writes the same way when exhausted.",
                "maintenance"))
        elif mxid_pct >= metrics._XID_WARN_FRACTION * 100:
            alerts.append(_alert("warning", "MultiXact ID age is high",
                f"MultiXact age is {mxid_pct}% of its separate wraparound limit.", "maintenance"))

        # Only worth naming the specific table once the overall age is
        # actually worth acting on -- on a healthy server the oldest table
        # is always *some* table, and naming it every poll is noise.
        if used_pct >= metrics._XID_WARN_FRACTION * 100:
            for t in (wrap.get("tables") or [])[:3]:
                if (t.get("xid_age") or 0) < metrics._XID_WARN_FRACTION * metrics.XID_WRAPAROUND_LIMIT:
                    continue
                disabled = str(t.get("autovacuum_enabled") or "on").lower() in ("off", "false")
                alerts.append(_alert("warning" if not disabled else "error",
                    f"{t['schema_name']}.{t['table_name']} is holding back the freeze horizon",
                    f"Its oldest unfrozen transaction ID is {t['xid_age']:,} transactions old"
                    + (" and autovacuum is DISABLED on this table, so nothing will ever freeze it automatically."
                       if disabled else ". VACUUM FREEZE this table to let the horizon advance."),
                    "maintenance"))

    # ------------------------------------------------------------ replication
    try:
        repl = await metrics.get_replication_info(pool)
    except Exception:
        repl = None
    if repl:
        for slot in repl.get("slots") or []:
            name = slot.get("slot_name")
            retained = slot.get("retained_bytes")
            wal_status = (slot.get("wal_status") or "").lower()
            if wal_status == "lost":
                alerts.append(_alert("error", f"Replication slot '{name}' has lost required WAL",
                    "Postgres has already discarded WAL this slot still needed. Whatever consumes this slot "
                    "(standby or logical subscriber) can no longer catch up and must be rebuilt; the slot "
                    "itself should be dropped.", "replication"))
            elif not slot.get("active"):
                severity = "error" if (retained or 0) >= _SLOT_RETAINED_ERROR_BYTES else "warning"
                alerts.append(_alert(severity, f"Replication slot '{name}' is inactive",
                    f"No consumer is connected, and it is pinning {_fmt_bytes(retained)} of WAL that Postgres "
                    "cannot recycle. An abandoned slot grows pg_wal without bound until the filesystem fills "
                    "and the instance goes down -- drop the slot if whatever created it is gone for good.",
                    "replication"))
            elif (retained or 0) >= _SLOT_RETAINED_ERROR_BYTES:
                alerts.append(_alert("warning", f"Replication slot '{name}' is retaining a lot of WAL",
                    f"{_fmt_bytes(retained)} of WAL is being held for this slot -- its consumer is connected "
                    "but falling behind.", "replication"))

        for s in repl.get("standbys") or []:
            label = s.get("application_name") or s.get("client_addr") or "standby"
            lag_bytes = s.get("replay_lag_bytes") or 0
            if lag_bytes >= _REPLICA_LAG_ERROR_BYTES:
                alerts.append(_alert("error", f"Standby '{label}' is far behind",
                    f"{_fmt_bytes(lag_bytes)} of WAL sent but not yet replayed. Failing over now would lose "
                    "everything in that gap.", "replication"))
            elif lag_bytes >= _REPLICA_LAG_WARN_BYTES:
                alerts.append(_alert("warning", f"Standby '{label}' is lagging",
                    f"{_fmt_bytes(lag_bytes)} of WAL sent but not yet replayed.", "replication"))
            if (s.get("state") or "") not in ("streaming", ""):
                alerts.append(_alert("warning", f"Standby '{label}' is not streaming",
                    f"Replication state is '{s.get('state')}' rather than 'streaming'.", "replication"))

    ckpt = await metrics.get_checkpoint_info(pool)
    stats = ckpt.get("stats") or {}
    timed, requested = stats.get("num_timed") or 0, stats.get("num_requested") or 0
    total_ckpt = timed + requested
    if total_ckpt >= _CHECKPOINT_MIN_SAMPLE and requested / total_ckpt > _CHECKPOINT_FORCED_RATIO_WARN:
        alerts.append(_alert("warning", "Checkpoints are happening too often",
            f"{requested} of {total_ckpt} checkpoints were forced early rather than on schedule -- "
            "consider raising max_wal_size.", "maintenance"))

    # -------------------------------------------------------------- security
    sec = await metrics.get_security_info(pool)
    if not sec.get("ssl_enabled"):
        alerts.append(_alert("info", "SSL is not enabled",
            "Connections to this database are not encrypted in transit.", "security"))
    pw_enc = sec.get("password_encryption")
    if pw_enc and pw_enc != "scram-sha-256":
        alerts.append(_alert("warning", "Weak password encryption method",
            f"password_encryption is '{pw_enc}' -- scram-sha-256 is the current recommended method.", "security"))
    for rule in sec.get("hba_rules") or []:
        if rule.get("auth_method") in ("trust", "password"):
            # database/user_name come back as Postgres text[] (asyncpg decodes
            # them as Python lists) -- join for display, don't interpolate
            # the list repr directly (was showing as literal "['all']").
            db_names = ",".join(rule.get("database") or []) or "?"
            user_names = ",".join(rule.get("user_name") or []) or "?"
            alerts.append(_alert("error", "Insecure pg_hba.conf authentication rule",
                f"Line {rule.get('line_number')}: '{rule['auth_method']}' auth for "
                f"{db_names}/{user_names} from {rule.get('address') or 'local'} "
                f"({'unencrypted password' if rule['auth_method'] == 'password' else 'no password check at all'}).",
                "security"))

    # ---------------------------------------------------------------- backup
    backup = await metrics.get_backup_info(pool)
    if backup.get("archive_mode") not in ("on", "always"):
        alerts.append(_alert("info", "WAL archiving is not enabled",
            "Point-in-time recovery isn't possible without WAL archiving configured -- fine for dev, "
            "worth fixing before this is a production database.", "backup"))
    archiver = backup.get("archiver") or {}
    if (archiver.get("failed_count") or 0) > 0:
        alerts.append(_alert("error", "WAL archiving has failures",
            f"{archiver['failed_count']} failed archive attempt(s); last failure: {archiver.get('last_failed_wal') or 'unknown'}.",
            "backup"))

    # ---------------------------------------------------- backup schedules
    # Checks pgmon's OWN schedule-tracking state (local SQLite, not
    # anything queryable on the monitored server) -- a scheduled backup
    # that fails, or simply stops running at all (pgmon down, a schedule
    # silently misconfigured), has nothing on the server itself to show
    # for it, unlike every other check in this file.
    if db_id is not None:
        now = datetime.now(timezone.utc)
        for sched in await backup_schedule.list_schedules(db_id):
            if not sched["enabled"]:
                continue
            label = f"{sched['backup_type']} ({sched['frequency']})"
            if sched["last_status"] == "error":
                alerts.append(_alert("error", "Scheduled backup failed last run",
                    f"The {label} backup schedule's most recent run failed "
                    f"({sched['last_run_at'] or 'unknown time'}). Check the Backup & Recovery tab for the job log.",
                    "backup"))
                continue
            interval_hours = backup_schedule.FREQUENCIES_HOURS.get(sched["frequency"])
            reference = sched["last_run_at"] or sched["created_at"]
            if not interval_hours or not reference:
                continue
            try:
                reference_dt = datetime.fromisoformat(reference)
            except ValueError:
                continue
            overdue_hours = (now - reference_dt).total_seconds() / 3600
            if overdue_hours > interval_hours * 2:
                verb = "has never run" if not sched["last_run_at"] else "is overdue"
                alerts.append(_alert("warning", f"Scheduled backup {verb}",
                    f"The {label} backup schedule {verb} -- expected roughly every {interval_hours}h, it's now "
                    f"been {overdue_hours:.0f}h. A one-off gap could just mean pgmon was down/restarting; if "
                    "not, check the schedule is still enabled and the scheduler is running.",
                    "backup"))

    return alerts
