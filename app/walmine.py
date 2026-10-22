"""Mines archived WAL for which transactions touched which tables, via
`pg_waldump` -- the Backup & Recovery tab's "Mine WAL for transactions"
tool.

This is metadata-level, not value-level: pg_waldump decodes WAL into
operations (INSERT/UPDATE/HOT_UPDATE/DELETE/...), the transaction ID that
made them, the LSN, and which table (via the relfilenode it touched) --
never the actual old/new column values. That's not a missing feature
here, it's inherent to the WAL format: genuinely reconstructing row
contents needs logical decoding (wal2json/pgoutput) via a replication
slot created *before* the transactions happened, which can't be
retrofitted onto WAL already written. Postgres has no equivalent of
Oracle LogMiner's full redo reconstruction.

Two sources:
  - A physical/incremental backup's own bundled pg_wal.tar.gz -- entirely
    local (it's already sitting in BACKUPS_DIR), no cluster access
    needed, but only covers that backup's own narrow start/end window.
  - The live primary's WAL archive (same PHYSICAL_RESTORE_WAL_ARCHIVE_PATH
    PITR already reads -- see app/backup.py's _copy_wal_archive, which
    this mirrors the read half of) -- broader history, but needs that
    env var configured and a `kubectl cp` off the live pod.

Jobs are tracked via app.backup's own job store (_new_job/_log/get_job)
so the existing `/api/backup/jobs/{id}` polling endpoint serves these too,
rather than standing up a second one.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tarfile
import tempfile
import time

from app import backup as backup_mod
from app.backup import BackupError, K8S_NAMESPACE, K8S_POD_NAME, WAL_ARCHIVE_PATH, is_pitr_configured
from app.db import ConnInfo

RMGR_NAMES = (
    "XLOG", "Transaction", "Storage", "CLOG", "Database", "Tablespace", "MultiXact",
    "RelMap", "Standby", "Heap2", "Heap", "Btree", "Hash", "Gin", "Gist", "Sequence",
    "SPGist", "BRIN", "CommitTs", "ReplicationOrigin", "Generic", "LogicalMessage",
)  # confirmed against this image's own `pg_waldump --rmgr=list`

MAX_RECORDS = 2000
MAX_RESOLVE = 200  # distinct relations to look up by name; beyond this, show the raw oid triple instead
_SEGMENT_NAME_RE = re.compile(r"^[0-9A-Fa-f]{24}$")

_LINE_RE = re.compile(
    r"^rmgr:\s*(?P<rmgr>\S+)\s+len \(rec/tot\):\s*\d+/\s*\d+,\s*tx:\s*(?P<xid>\d+),\s*"
    r"lsn:\s*(?P<lsn>\S+),\s*prev\s*\S+,\s*desc:\s*(?P<desc>.*)$"
)
_BLKREF_RE = re.compile(r"rel\s+(\d+)/(\d+)/(\d+)")


def _validate_xid(xid: str | None) -> str | None:
    if not xid:
        return None
    xid = str(xid).strip()
    if not xid.isdigit():
        raise BackupError("Transaction ID must be a positive integer.")
    return xid


def _validate_rmgr(rmgr: str | None) -> str | None:
    if not rmgr:
        return None
    if rmgr not in RMGR_NAMES:
        raise BackupError(f"Unknown resource manager: {rmgr!r}.")
    return rmgr


def _parse_waldump_output(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        desc = m.group("desc")
        operation = desc.split(",", 1)[0].split()[0] if desc else m.group("rmgr")
        triples = sorted({f"{t}/{d}/{r}" for t, d, r in _BLKREF_RE.findall(line)})
        records.append({
            "lsn": m.group("lsn"), "xid": int(m.group("xid")), "rmgr": m.group("rmgr"),
            "operation": operation, "desc": desc, "relations": triples,
        })
    return records


async def _run_pg_waldump(wal_dir: str, args: list[str], segment_limit: int | None = None) -> str:
    segs = sorted(f for f in os.listdir(wal_dir) if _SEGMENT_NAME_RE.match(f))
    if not segs:
        raise BackupError("No WAL segment files found there.")
    if segment_limit:
        segs = segs[-segment_limit:]
    proc = await asyncio.create_subprocess_exec(
        "pg_waldump", "-p", wal_dir, *args, segs[0], segs[-1],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackupError("pg_waldump timed out after 120s.")
    text = out.decode(errors="replace")
    # A non-zero exit is normal here, not just on real failure: pg_waldump
    # hits "error: error in WAL record" the moment it reaches a segment's
    # not-yet-written tail (expected for the currently-active segment) --
    # only treat it as a hard failure if it produced no output at all.
    if proc.returncode != 0 and not text.strip():
        raise BackupError(f"pg_waldump failed: {err.decode(errors='replace').strip()}")
    return text


async def _resolve_relations(conn_info: ConnInfo, triples: set[str]) -> dict[str, str]:
    if not triples:
        return {}
    import asyncpg
    conn = await asyncpg.connect(dsn=conn_info.dsn, timeout=8)
    try:
        my_dbid = await conn.fetchval("SELECT oid FROM pg_database WHERE datname = current_database()")
        out: dict[str, str] = {}
        for i, triple in enumerate(sorted(triples)):
            tso, dbo, relfile = triple.split("/")
            if int(dbo) != my_dbid:
                out[triple] = f"(other database, oid {dbo})"
                continue
            if i >= MAX_RESOLVE:
                out[triple] = triple
                continue
            try:
                row = await conn.fetchrow(
                    "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.oid = pg_filenode_relation($1, $2)",
                    int(tso), int(relfile),
                )
            except asyncpg.PostgresError:
                row = None
            out[triple] = f"{row['nspname']}.{row['relname']}" if row else f"(filenode {relfile}, dropped or system)"
        return out
    finally:
        await conn.close()


async def _finish(text: str, conn_info: ConnInfo, table_contains: str | None) -> dict:
    records = _parse_waldump_output(text)
    all_triples = {t for r in records for t in r["relations"]}
    resolved = await _resolve_relations(conn_info, all_triples)
    for r in records:
        r["tables"] = sorted({resolved.get(t, t) for t in r["relations"]}) or ["(none)"]
        del r["relations"]
    if table_contains:
        needle = table_contains.strip().lower()
        records = [r for r in records if any(needle in t.lower() for t in r["tables"])]
    records.sort(key=lambda r: r["lsn"])
    truncated = len(records) > MAX_RECORDS
    return {"records": records[:MAX_RECORDS], "truncated": truncated, "total_matched": len(records)}


async def mine_backup_wal(
    db_id: str, filename: str, conn_info: ConnInfo,
    xid: str | None, rmgr: str | None, table_contains: str | None,
) -> dict:
    path = backup_mod.backup_path(db_id, filename)
    meta = backup_mod._read_meta(path)
    if meta.get("type") not in ("physical", "incremental"):
        raise BackupError(
            f"{filename} is a {meta.get('type') or 'logical'} backup -- WAL mining only applies to "
            "physical/incremental backups (logical backups don't carry WAL at all)."
        )
    wal_tar = os.path.join(path, "pg_wal.tar.gz")
    if not os.path.exists(wal_tar):
        raise BackupError(f"{filename} has no pg_wal.tar.gz -- nothing to mine.")

    tmp_dir = tempfile.mkdtemp(prefix="pgmon-walmine-")
    try:
        def _extract():
            with tarfile.open(wal_tar, "r:gz") as tf:
                tf.extractall(tmp_dir, filter="data")
        await asyncio.to_thread(_extract)

        args = []
        if xid:
            args += ["-x", xid]
        if rmgr:
            args += ["-r", rmgr]
        text = await _run_pg_waldump(tmp_dir, args)
        return await _finish(text, conn_info, table_contains)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def mine_live_wal_archive(
    conn_info: ConnInfo, segment_limit: int,
    xid: str | None, rmgr: str | None, table_contains: str | None,
) -> dict:
    if not is_pitr_configured():
        raise BackupError(
            "Mining the live WAL archive isn't configured on this deployment "
            "(PHYSICAL_RESTORE_WAL_ARCHIVE_PATH is unset) -- mine a physical/incremental backup's own "
            "WAL instead."
        )
    segment_limit = max(1, min(int(segment_limit or 10), 100))

    tmp_dir = tempfile.mkdtemp(prefix="pgmon-walmine-")
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "cp", f"{K8S_NAMESPACE}/{K8S_POD_NAME}:{WAL_ARCHIVE_PATH}", tmp_dir,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BackupError("Copying the WAL archive off the primary timed out after 180s.")
        if proc.returncode != 0:
            raise BackupError(f"Failed to copy the WAL archive off the primary: {err.decode(errors='replace').strip()}")

        args = []
        if xid:
            args += ["-x", xid]
        if rmgr:
            args += ["-r", rmgr]
        # Copies the whole archive every time (kubectl cp has no server-side
        # filtering) -- same limitation PITR's own _copy_wal_archive already
        # has -- but only *scans* the most recent segment_limit of them, to
        # bound how long pg_waldump itself takes.
        text = await _run_pg_waldump(tmp_dir, args, segment_limit=segment_limit)
        return await _finish(text, conn_info, table_contains)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _run_mine_job(job: dict, coro) -> None:
    try:
        result = await coro
        job["result"] = result
        note = " (truncated to the first " + str(MAX_RECORDS) + ")" if result["truncated"] else ""
        backup_mod._log(job, f"Found {result['total_matched']} matching record(s){note}.")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        backup_mod._log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()


async def start_mine_backup_wal(
    db_id: str, filename: str, conn_info: ConnInfo,
    xid: str | None = None, rmgr: str | None = None, table_contains: str | None = None,
) -> str:
    xid = _validate_xid(xid)
    rmgr = _validate_rmgr(rmgr)
    job = backup_mod._new_job("mine-wal")
    asyncio.create_task(_run_mine_job(job, mine_backup_wal(db_id, filename, conn_info, xid, rmgr, table_contains)))
    return job["id"]


async def start_mine_live_wal_archive(
    conn_info: ConnInfo, segment_limit: int = 10,
    xid: str | None = None, rmgr: str | None = None, table_contains: str | None = None,
) -> str:
    xid = _validate_xid(xid)
    rmgr = _validate_rmgr(rmgr)
    job = backup_mod._new_job("mine-wal")
    asyncio.create_task(
        _run_mine_job(job, mine_live_wal_archive(conn_info, segment_limit, xid, rmgr, table_contains))
    )
    return job["id"]
