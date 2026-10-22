"""On-demand backup & recovery for the Backup tab.

Three backup types, tracked via a small `.meta.json` sidecar next to each
backup artifact (a single file for logical, a directory for physical/
incremental -- neither format carries its own explicit type marker, so
this app needs to remember it separately):

  logical      pg_dump -Fc of the current database -- the whole thing by
               default, or scoped to selected schemas (-n) or selected
               tables (-t) via scope_type/scope (see _validate_scope,
               _run_logical_backup). Portable across Postgres versions/
               architectures. Restore is a one-click `pg_restore --clean
               --if-exists` against the live database (see start_restore)
               -- safe because it only touches SQL objects, not
               server-level files, and only ever restores whatever the
               backup actually contains (a scoped backup restores just
               that scope, with no extra step needed).

  physical     pg_basebackup of the WHOLE server (every database, roles,
               config) -- a physical backup necessarily operates at the
               cluster level, not a single database, since Postgres's
               data files are shared across all databases in the
               instance. Requires wal_level >= replica and a role with
               REPLICATION privilege (or superuser).

  incremental  pg_basebackup --incremental=<manifest>, built against a
               prior physical/incremental backup's manifest (PG17+
               feature). Requires summarize_wal=on continuously since
               that base backup was taken -- see _ensure_summarize_wal.

Restoring physical/incremental backups means stopping Postgres,
replacing its entire data directory, and restarting -- fundamentally
different from logical restore, which only touches SQL objects over a
normal connection. That can't be done from inside this container (the
target server's data directory isn't something a Postgres client
connection can touch), so it's automated instead via `kubectl`,
requiring this app's own ServiceAccount be granted narrow,
StatefulSet-specific RBAC (see k8s/ in the repo) -- a real increase in
what this app can do, only worth it because the alternative (a human
manually running these same steps under pressure during an actual
incident) is itself risky. It's opt-in: unset PHYSICAL_RESTORE_* env
vars mean physical/incremental backups can still be taken and
downloaded, just not restored through the UI (see
RESTORE_INSTRUCTIONS/is_physical_restore_configured for the manual path).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import time
import uuid

from app import audit

logger = logging.getLogger("pgmon.backup")

from app import roles as roles_mod
from app.db import ConnInfo

BACKUPS_DIR = os.environ.get("BACKUPS_DIR", "/data/backups")
_SAFE_DB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_PGDUMP_CUSTOM_MAGIC = b"PGDMP"
_PHYSICAL_REQUIRED_MEMBERS = ("backup_manifest", "base.tar.gz")


class _SingleConnPool:
    """Adapts one asyncpg.Connection to the pool.acquire() shape app.roles
    expects, for the one-off role provisioning a restore may do -- this
    borrows its already-validated CREATE/DROP ROLE SQL (proper
    quote_ident/quote_literal use, self-connection guard, etc.) instead of
    duplicating that here."""
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False

BACKUP_TYPES = ("logical", "physical", "incremental")

RESTORE_INSTRUCTIONS = (
    "Physical/incremental backups restore the whole server, not just this database. To restore "
    "manually: 1) stop Postgres, 2) empty its data directory, 3) extract this backup's base.tar.gz "
    "into it (for an incremental chain, combine it with its base backup first using "
    "pg_combinebackup), 4) start Postgres. If PHYSICAL_RESTORE_* env vars are configured, pgmon can "
    "automate this instead -- see below."
)

# Physical/incremental restore needs to know exactly which StatefulSet/PVC
# to stop and swap the data directory of -- unlike every other feature in
# this app, which only ever needs a Postgres wire-protocol connection, this
# one needs Kubernetes-level access to the *deployment* of one specific
# server. Left unset, physical/incremental backups can still be taken and
# downloaded, just not restored through the UI -- see RESTORE_INSTRUCTIONS.
K8S_NAMESPACE = os.environ.get("PHYSICAL_RESTORE_NAMESPACE", "")
K8S_STATEFULSET = os.environ.get("PHYSICAL_RESTORE_STATEFULSET", "")
K8S_PVC = os.environ.get("PHYSICAL_RESTORE_PVC", "")
K8S_POD_NAME = os.environ.get("PHYSICAL_RESTORE_POD_NAME", "") or (f"{K8S_STATEFULSET}-0" if K8S_STATEFULSET else "")
K8S_MOUNT_PATH = os.environ.get("PHYSICAL_RESTORE_MOUNT_PATH", "/bitnami/postgresql")
RESTORE_WORKER_IMAGE = os.environ.get("PHYSICAL_RESTORE_WORKER_IMAGE", "pg-ai-monitor:latest")

# "Restore to a new instance": a separate, temporary Postgres pod + its own
# storage, populated from the backup -- unlike in-place restore, this never
# touches the live server or its PVC at all, so it needs none of the
# scale-down/scale-up dance, just a node to schedule on (reusing the live
# pod's own node, for simplicity -- a brand-new PVC has no affinity of its
# own to satisfy). Gated on the same config as in-place restore since both
# need K8S_NAMESPACE and a real cluster to create resources in.
TEMP_INSTANCE_LABEL = "pg-ai-monitor-temp-instance"
TEMP_PVC_SIZE = os.environ.get("PHYSICAL_RESTORE_TEMP_PVC_SIZE", "2Gi")
TEMP_STORAGE_CLASS = os.environ.get("PHYSICAL_RESTORE_STORAGE_CLASS", "local-path")
TEMP_INSTANCE_IMAGE = os.environ.get("PHYSICAL_RESTORE_TEMP_INSTANCE_IMAGE", "bitnami/postgresql:latest")

# Point-in-time recovery: replays archived WAL (see the primary's
# archive_mode/archive_command) past a physical/incremental backup's own
# end, up to an arbitrary timestamp. The archive lives on a ReadWriteOnce
# PVC already mounted on the live primary, so it can't also be mounted on
# the restore worker/temp-instance pod at the same time -- instead its
# contents are copied out via `kubectl cp` (through this machine, the same
# way backup files themselves already get copied into a worker pod) and
# dropped in as plain files a `restore_command` reads from. See
# _copy_wal_archive.
WAL_ARCHIVE_PATH = os.environ.get("PHYSICAL_RESTORE_WAL_ARCHIVE_PATH", "")

_SAFE_TARGET_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}(:?\d{2})?|Z)?$"
)


def is_physical_restore_configured() -> bool:
    return bool(K8S_NAMESPACE and K8S_STATEFULSET and K8S_PVC and K8S_POD_NAME)


def is_pitr_configured() -> bool:
    return is_physical_restore_configured() and bool(WAL_ARCHIVE_PATH)


async def reconcile_on_startup() -> None:
    """Runs once at app startup (see main.py's lifespan). Restore job state
    lives only in the in-process `_jobs` dict (see _new_job below) -- if
    the pgmon pod itself is killed mid in-place-restore (OOM, node
    eviction, a rolling deploy landing at the wrong moment), that state is
    gone, but two things can still be left behind in the cluster:

      1. The restore worker pod (_worker_pod_manifest) -- always meant to
         be short-lived and deleted by the job that created it. If
         nothing survived to do that, it's safe to remove unconditionally
         on the next startup: nothing can still "own" it.
      2. The target StatefulSet possibly still scaled to 0 (scaled down
         before the restore, never scaled back up). This one we do NOT
         touch automatically -- a StatefulSet at 0 replicas looks
         identical whether it's a crashed restore or an operator's
         deliberate maintenance window, and guessing wrong by
         auto-scaling a production database back up unasked is worse than
         a loud warning. This only makes sure a human finds out fast
         (log + audit entry) instead of days later.
    """
    if not is_physical_restore_configured():
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "-n", K8S_NAMESPACE, "get", "pods",
            "-l", "app=pg-ai-monitor-restore-worker", "-o", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            data = json.loads(out or b"{}")
            orphans = [item["metadata"]["name"] for item in data.get("items", [])]
            for name in orphans:
                logger.warning(
                    "startup reconcile: removing orphaned restore worker pod %s/%s "
                    "(left behind by a restore that didn't finish cleanly)", K8S_NAMESPACE, name,
                )
                await asyncio.create_subprocess_exec(
                    "kubectl", "-n", K8S_NAMESPACE, "delete", "pod", name,
                    "--wait=false", "--ignore-not-found=true",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
            if orphans:
                await audit.log(
                    "system", "backup.reconcile.orphaned_worker_cleanup",
                    detail=f"Removed {len(orphans)} orphaned restore worker pod(s) in {K8S_NAMESPACE}: "
                           f"{', '.join(orphans)}",
                )
    except Exception:
        logger.exception("startup reconcile: failed to check for orphaned restore worker pods")

    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "-n", K8S_NAMESPACE, "get", "statefulset", K8S_STATEFULSET, "-o", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            data = json.loads(out or b"{}")
            if data.get("spec", {}).get("replicas") == 0:
                msg = (
                    f"{K8S_STATEFULSET} in {K8S_NAMESPACE} is at 0 replicas. If this wasn't "
                    "deliberate (e.g. a maintenance window), it likely means pgmon was interrupted "
                    "mid physical-restore before it could scale back up -- verify, then "
                    f"`kubectl -n {K8S_NAMESPACE} scale statefulset {K8S_STATEFULSET} --replicas=1` if so."
                )
                logger.warning("startup reconcile: %s", msg)
                await audit.log("system", "backup.reconcile.statefulset_at_zero", detail=msg)
    except Exception:
        logger.exception("startup reconcile: failed to check %s replica count", K8S_STATEFULSET)


def _validate_target_time(target_time: str) -> str:
    if not target_time or not _SAFE_TARGET_TIME.match(target_time):
        raise BackupError(
            "target_time must look like an ISO timestamp, e.g. '2026-09-15 18:00:00+00'."
        )
    return target_time


class BackupError(Exception):
    pass


# --------------------------------------------------------- recovery targets
# Generalizes PITR beyond a plain timestamp -- every alternative recovery_target_*
# GUC Postgres itself supports (confirmed against this image's own
# `postgres --describe-config`), plus the action/timeline/inclusive knobs
# around it. Server defaults, for reference: recovery_target_action=pause,
# recovery_target_inclusive=on, recovery_target_timeline=latest -- pgmon
# has always overridden the first to 'promote' for convenience on a
# disposable temp instance; 'pause' is now an explicit opt-in for anyone
# who wants to inspect before committing to promotion.
RECOVERY_TARGET_TYPES = ("time", "lsn", "xid", "name")
RECOVERY_TARGET_ACTIONS = ("promote", "pause")

_SAFE_LSN = re.compile(r"^[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}$")
_SAFE_XID = re.compile(r"^\d{1,20}$")
_SAFE_RESTORE_POINT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
_SAFE_TIMELINE = re.compile(r"^(latest|current|\d+)$")


def _validate_recovery_target(raw: dict | None) -> dict | None:
    if not raw:
        return None
    rt = dict(raw)
    ttype = rt.get("type")
    if ttype not in RECOVERY_TARGET_TYPES:
        raise BackupError(f"Unknown recovery target type: {ttype!r}")
    value = (rt.get("value") or "").strip()
    if not value:
        raise BackupError("Specify a recovery target value.")
    if ttype == "time":
        value = _validate_target_time(value)
    elif ttype == "lsn":
        if not _SAFE_LSN.match(value):
            raise BackupError("A recovery target LSN must look like '16/B374D848'.")
    elif ttype == "xid":
        if not _SAFE_XID.match(value):
            raise BackupError("A recovery target transaction ID must be a positive integer.")
    elif ttype == "name":
        if not _SAFE_RESTORE_POINT_NAME.match(value):
            raise BackupError(
                "A recovery target name must match a restore point actually created on the source "
                "server via pg_create_restore_point()."
            )
    rt["value"] = value

    action = rt.get("action") or "promote"
    if action not in RECOVERY_TARGET_ACTIONS:
        raise BackupError(f"Unknown recovery target action: {action!r}")
    rt["action"] = action

    timeline = str(rt.get("timeline") or "latest")
    if not _SAFE_TIMELINE.match(timeline):
        raise BackupError("recovery target timeline must be 'latest', 'current', or a timeline ID.")
    rt["timeline"] = timeline

    rt["inclusive"] = bool(rt.get("inclusive", True))
    return rt


def _db_dir(db_id: str) -> str:
    if not _SAFE_DB_ID.match(db_id):
        raise BackupError("Invalid database id.")
    path = os.path.join(BACKUPS_DIR, db_id)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    if not name or not _SAFE_NAME.match(name) or name in (".", ".."):
        raise BackupError("Invalid backup name.")
    return name


def _entry_path(db_id: str, name: str) -> str:
    return os.path.join(_db_dir(db_id), _safe_name(name))


def _meta_path(entry_path: str) -> str:
    return entry_path.rstrip("/").rstrip(os.sep) + ".meta.json"


def _write_meta(entry_path: str, meta: dict) -> None:
    with open(_meta_path(entry_path), "w") as f:
        json.dump(meta, f)


def _read_meta(entry_path: str) -> dict:
    try:
        with open(_meta_path(entry_path)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500MB
_MIN_FREE_PERCENT = 5.0


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _disk_space_status() -> tuple[bool, str]:
    """Checks free space on the volume backing BACKUPS_DIR. Returns
    (critically_low, message) -- a backup that runs out of disk mid-write
    doesn't fail loudly, it just leaves a truncated file sitting in the
    list looking like a normal backup (see verify_backup for the other
    half of catching that), so this checks *before* spending the time to
    find out the hard way, and again *after* in case the backup that just
    finished is what tipped things over for the next one."""
    try:
        usage = shutil.disk_usage(BACKUPS_DIR)
    except OSError as e:
        return False, f"Couldn't check free disk space for {BACKUPS_DIR}: {e}"
    free_pct = (usage.free / usage.total * 100) if usage.total else 100.0
    msg = f"{_fmt_bytes(usage.free)} free of {_fmt_bytes(usage.total)} ({free_pct:.1f}%) on the backup volume."
    low = usage.free < _MIN_FREE_BYTES or free_pct < _MIN_FREE_PERCENT
    return low, msg


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def list_backups(db_id: str) -> list[dict]:
    path = _db_dir(db_id)
    out = []
    for name in os.listdir(path):
        if name.endswith(".meta.json"):
            continue
        full = os.path.join(path, name)
        is_dir = os.path.isdir(full)
        meta = _read_meta(full)
        out.append({
            "filename": name,
            "is_dir": is_dir,
            "type": meta.get("type", "logical"),
            "based_on": meta.get("based_on"),
            "scope_type": meta.get("scope_type", "whole"),
            "scope": meta.get("scope") or [],
            "size_bytes": _dir_size(full) if is_dir else os.path.getsize(full),
            "created_at": meta.get("created_at") or os.path.getmtime(full),
            "verified_at": meta.get("verified_at"),
            "verified_ok": meta.get("verified_ok"),
        })
    out.sort(key=lambda b: b["created_at"], reverse=True)
    return out


def backup_path(db_id: str, filename: str) -> str:
    path = _entry_path(db_id, filename)
    if not os.path.exists(path):
        raise BackupError("No such backup.")
    return path


_TOC_TABLE_LINE_RE = re.compile(r"^\d+;\s+\d+\s+\d+\s+TABLE\s+(\S+)\s+(\S+)\s+\S+\s*$")


async def list_backup_tables(db_id: str, filename: str) -> list[str]:
    """Every table actually present in a logical backup file, as
    "schema.table" strings -- read from pg_restore's own table of contents
    (`pg_restore --list`), not a live catalog query, since what matters for
    restoring is what the backup itself contains (which may no longer match
    the live database -- tables get added/dropped/renamed between when a
    backup was taken and when it's restored). Powers the restore scope
    picker (restore selected schemas/tables instead of the whole backup).
    """
    path = backup_path(db_id, filename)
    meta = _read_meta(path)
    if meta.get("type") not in (None, "logical"):
        raise BackupError(f"{filename} is a {meta.get('type')} backup -- there's no table-of-contents to list.")
    proc = await asyncio.create_subprocess_exec(
        "pg_restore", "--list", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise BackupError(f"Couldn't read {filename}'s contents: {err.decode(errors='replace').strip()}")
    tables = []
    for line in out.decode(errors="replace").splitlines():
        if " TABLE DATA " in line:
            continue  # a per-table *data* entry, not the table definition itself -- skip to avoid double-counting
        m = _TOC_TABLE_LINE_RE.match(line)
        if m:
            tables.append(f"{m.group(1)}.{m.group(2)}")
    return tables


def manifest_path(db_id: str, filename: str) -> str:
    """Path to a physical/incremental backup's manifest, used as the
    `--incremental=` base for a later incremental backup."""
    path = os.path.join(backup_path(db_id, filename), "backup_manifest")
    if not os.path.exists(path):
        raise BackupError(f"{filename} has no backup_manifest -- it can't be used as an incremental base.")
    return path


async def prepare_download(db_id: str, filename: str) -> tuple[str, bool]:
    """Returns (path_to_send, is_temporary). A logical backup is already a
    single file; a physical/incremental one is a directory, tarred into a
    temp file on demand (the caller is expected to delete it afterward if
    is_temporary is True)."""
    path = backup_path(db_id, filename)
    if not os.path.isdir(path):
        return path, False
    tmp_path = path.rstrip("/") + f".{uuid.uuid4().hex[:8]}.tar.gz"

    def _make_tar():
        with tarfile.open(tmp_path, "w:gz") as tf:
            tf.add(path, arcname=filename)

    await asyncio.to_thread(_make_tar)
    return tmp_path, True


def prune_old_backups(db_id: str, retention_days: int) -> list[str]:
    """Deletes backups older than retention_days, except: (1) never the
    single most recent backup, no matter how old (a schedule paused for a
    while shouldn't be able to prune its way to zero backups), and (2)
    never one that a newer, kept incremental backup still chains back to
    (see _resolve_chain) -- deleting a base out from under a surviving
    incremental would silently make that incremental unrestorable."""
    backups = list_backups(db_id)  # newest-first, per list_backups
    if len(backups) <= 1:
        return []
    cutoff = time.time() - retention_days * 86400
    keep_filenames = {backups[0]["filename"]}  # newest always survives
    by_filename = {b["filename"]: b for b in backups}

    # Anything newer than cutoff survives outright, and pins its whole
    # based_on ancestry so an old base backup a kept incremental still
    # depends on isn't deleted out from under it.
    for b in backups:
        if b["created_at"] >= cutoff:
            keep_filenames.add(b["filename"])
            current = b.get("based_on")
            while current and current in by_filename and current not in keep_filenames:
                keep_filenames.add(current)
                current = by_filename[current].get("based_on")

    deleted = []
    for b in backups:
        if b["filename"] not in keep_filenames:
            delete_backup(db_id, b["filename"])
            deleted.append(b["filename"])
    return deleted


def delete_backup(db_id: str, filename: str) -> None:
    path = _entry_path(db_id, filename)
    if not os.path.exists(path):
        raise BackupError("No such backup.")
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    meta = _meta_path(path)
    if os.path.exists(meta):
        os.remove(meta)


# ----------------------------------------------------------- verification
async def _run_verify_job(job: dict, db_id: str, filename: str) -> None:
    try:
        path = backup_path(db_id, filename)
        meta = _read_meta(path)
        if meta.get("type") not in ("physical", "incremental"):
            raise BackupError(
                f"{filename} is a {meta.get('type') or 'logical'} backup -- there's no backup_manifest to "
                "verify against (pg_dump doesn't produce one)."
            )
        _log(job, f"=== pg_verifybackup: checking {filename} against its own recorded checksums ===")
        # -F t: our physical/incremental backups are always stored in tar
        # format (see _run_physical_backup's -Ft). -n/--no-parse-wal: WAL
        # parsing needs extracted segment files, not the tar.gz this app
        # stores -- skip it rather than extracting just for this; -F t
        # already checksums every file, which is what actually catches
        # corruption.
        rc = await _run(job, ["pg_verifybackup", "-F", "t", "-n", "-P", path], os.environ, timeout=1800)
        ok = rc == 0
        meta["verified_at"] = time.time()
        meta["verified_ok"] = ok
        _write_meta(path, meta)
        if not ok:
            raise BackupError(f"{filename} failed verification -- see log for details. The backup may be corrupt.")
        _log(job, "Backup verified OK -- every file matches its recorded checksum.")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()


async def start_verify_backup(db_id: str, filename: str) -> str:
    job = _new_job("verify")
    asyncio.create_task(_run_verify_job(job, db_id, filename))
    return job["id"]


# ------------------------------------------------------- import from elsewhere
def _import_stem(upload_filename: str, strip_suffixes: tuple[str, ...]) -> str:
    base = os.path.basename(upload_filename or "")
    lower = base.lower()
    for suf in strip_suffixes:
        if lower.endswith(suf):
            base = base[: -len(suf)]
            break
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", base).strip("_")
    return stem or "import"


async def import_logical_backup(db_id: str, upload_filename: str, src_file) -> dict:
    """Adopts a pg_dump custom-format (-Fc) file -- typically brought over
    from another server -- into this database's backup store, so it shows
    up in the Backup & Recovery tab and can be restored like any backup
    pgmon took itself. Checked via pg_dump's own magic bytes rather than
    the filename/extension: pg_restore (what Restore uses) can only read
    the custom/directory/tar dump formats, never a plain-text/.sql dump,
    and an extension is just self-reported, not a guarantee.
    """
    magic = await asyncio.to_thread(src_file.read, len(_PGDUMP_CUSTOM_MAGIC))
    if magic != _PGDUMP_CUSTOM_MAGIC:
        raise BackupError(
            "Not a pg_dump custom-format file. Re-create it with `pg_dump -Fc ...` -- "
            "plain-text/.sql dumps can't be restored via pg_restore."
        )
    await asyncio.to_thread(src_file.seek, 0)

    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    stem = _import_stem(upload_filename, (".dump", ".sql", ".bak", ".backup"))
    filename = f"{ts}_imported_{stem}_{uuid.uuid4().hex[:6]}.dump"
    path = _entry_path(db_id, filename)

    def _write():
        with open(path, "wb") as dst:
            shutil.copyfileobj(src_file, dst)

    try:
        await asyncio.to_thread(_write)
    except OSError as e:
        if os.path.exists(path):
            os.remove(path)
        raise BackupError(f"Couldn't save the uploaded file: {e}")

    _write_meta(path, {
        "type": "logical", "created_at": time.time(), "dbname": None,
        "scope_type": "whole", "scope": [], "imported": True,
    })
    return {"filename": filename}


def _find_physical_root(extract_dir: str) -> str:
    """The extracted archive's backup root: either extract_dir itself (if
    it directly contains backup_manifest/base.tar.gz), or a single nested
    directory that does -- matches both a raw pg_basebackup directory
    re-tarred by hand, and pgmon's own Download button output for a
    physical backup (which wraps the directory one level deep, see
    prepare_download)."""
    def _has_required(d: str) -> bool:
        return all(os.path.exists(os.path.join(d, m)) for m in _PHYSICAL_REQUIRED_MEMBERS)

    if _has_required(extract_dir):
        return extract_dir
    entries = [e for e in os.listdir(extract_dir) if not e.startswith(".")]
    if len(entries) == 1:
        nested = os.path.join(extract_dir, entries[0])
        if os.path.isdir(nested) and _has_required(nested):
            return nested
    raise BackupError(
        "Archive doesn't look like a physical backup -- expected backup_manifest and "
        "base.tar.gz (what `pg_basebackup -Ft -z` produces), either at the archive's "
        "top level or inside a single top-level folder."
    )


async def import_physical_backup(db_id: str, upload_filename: str, src_file) -> dict:
    """Adopts a pg_basebackup-style archive (.tar.gz containing
    backup_manifest + base.tar.gz, optionally pg_wal.tar.gz) brought over
    from another server. Always imported as a standalone 'physical'
    backup, never 'incremental' -- verifying that an imported backup
    actually chains to some prior base this app never took itself isn't
    something pgmon can do, so that pairing (pg_combinebackup) has to be
    handled manually if it's really an incremental."""
    tmp_dir = tempfile.mkdtemp(prefix="pgmon-import-")
    try:
        tmp_tar = os.path.join(tmp_dir, "upload.tar.gz")

        def _save():
            with open(tmp_tar, "wb") as dst:
                shutil.copyfileobj(src_file, dst)
        await asyncio.to_thread(_save)

        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        def _extract():
            with tarfile.open(tmp_tar, "r:gz") as tf:
                tf.extractall(extract_dir, filter="data")
        try:
            await asyncio.to_thread(_extract)
        except tarfile.TarError as e:
            raise BackupError(f"Not a valid .tar.gz archive: {e}")

        root = _find_physical_root(extract_dir)

        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        stem = _import_stem(upload_filename, (".tar.gz", ".tgz"))
        dirname = f"{ts}_imported_{stem}_{uuid.uuid4().hex[:6]}_physical"
        dest = _entry_path(db_id, dirname)

        await asyncio.to_thread(shutil.move, root, dest)

        _write_meta(dest, {
            "type": "physical", "created_at": time.time(), "dbname": None,
            "based_on": None, "imported": True,
        })
        return {"filename": dirname}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------- job store
_jobs: dict[str, dict] = {}


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def _new_job(kind: str) -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id, "kind": kind, "status": "running",
        "started_at": time.time(), "finished_at": None, "error": None,
        "log": [], "filename": None,
    }
    _jobs[job_id] = job
    return job


def _log(job: dict, text: str) -> None:
    for line in text.splitlines():
        if line.strip():
            job["log"].append(line)
    if len(job["log"]) > 500:
        job["log"] = job["log"][-500:]


async def _run(job: dict, args: list[str], env: dict, timeout: float = 300) -> int:
    # uvicorn[standard] runs on uvloop, whose subprocess implementation
    # requires a real dict for `env` -- os.environ itself is a _Environ
    # proxy object and gets rejected ("Expected dict, got _Environ"),
    # unlike under plain asyncio's event loop where either works. Every
    # caller passes os.environ (occasionally merged with an extra var) --
    # normalizing once here means no call site has to remember this.
    proc = await asyncio.create_subprocess_exec(
        *args, env=dict(env), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None

    async def _drain():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            _log(job, line.decode(errors="replace"))
        return await proc.wait()

    # `kubectl cp` in particular can hang indefinitely rather than exit
    # non-zero when the remote side fails mid-stream (its local tar
    # process blocks writing into a pipe the remote end already closed) --
    # physical restore already leaves Postgres offline while this runs, so
    # every subprocess here gets a hard ceiling rather than trusting it to
    # always exit on its own. On timeout the process is killed outright
    # and this raises, letting the caller's own error handling (which
    # tries to scale Postgres back up) still run.
    try:
        return await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        _log(job, f"TIMEOUT after {timeout}s waiting for: {' '.join(args)}")
        proc.kill()
        await proc.wait()
        raise BackupError(f"Command timed out after {timeout}s: {' '.join(args)}")


async def _ensure_summarize_wal(job: dict, conn_info: ConnInfo) -> None:
    """Incremental backups need continuous WAL summaries since the base
    backup was taken (PG17+ pg_basebackup --incremental). Turning this on
    is a plain reload (sighup context, no restart) -- see the Alerts tab's
    pending-restart check, which would flag it otherwise."""
    import asyncpg
    conn = await asyncpg.connect(dsn=conn_info.dsn, timeout=8)
    try:
        # summarize_wal (and --incremental itself) is a PG17+ feature --
        # querying it on an older server fails with a raw, confusing
        # "unrecognized configuration parameter" straight from Postgres
        # rather than this. Checked explicitly, not inferred from that
        # error, so the message is clear the first time rather than only
        # after someone's already hit the cryptic one.
        version_num = int(await conn.fetchval("SHOW server_version_num"))
        if version_num < 170000:
            major = version_num // 10000
            raise BackupError(
                f"Incremental backups need Postgres 17 or newer on the target server "
                f"(summarize_wal was introduced in PG17) -- this server is running {major}. "
                "Take a plain physical (or logical) backup instead."
            )
        current = await conn.fetchval("SHOW summarize_wal")
        if current == "on":
            return
        _log(job, "summarize_wal is off -- enabling it now (reload only, no restart needed)...")
        await conn.execute("ALTER SYSTEM SET summarize_wal = 'on'")
        await conn.execute("SELECT pg_reload_conf()")
        _log(job, "summarize_wal enabled. Note: an incremental backup needs WAL summaries covering "
                   "the entire period since its base backup -- if that base was taken before this, "
                   "the incremental will fail until a new base backup is taken with this setting already on.")
    finally:
        await conn.close()


LOGICAL_SCOPE_TYPES = ("whole", "schemas", "tables")


async def _validate_scope(conn_info: ConnInfo, scope_type: str, scope: list[str]) -> None:
    """Checks every requested schema/table actually exists before pg_dump
    ever runs, so a typo or a stale UI selection fails with a clear message
    instead of either a cryptic pg_dump error or (worse) pg_dump silently
    dumping nothing because its pattern matched zero objects."""
    if scope_type == "whole":
        return
    if not scope:
        raise BackupError(f"Select at least one {'schema' if scope_type == 'schemas' else 'table'} to back up.")
    import asyncpg
    conn = await asyncpg.connect(dsn=conn_info.dsn, timeout=8)
    try:
        if scope_type == "schemas":
            for schema in scope:
                exists = await conn.fetchval(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", schema
                )
                if not exists:
                    raise BackupError(f"No such schema: {schema!r}")
        elif scope_type == "tables":
            for qualified in scope:
                if "." not in qualified:
                    raise BackupError(f"{qualified!r} must be schema-qualified, e.g. 'public.my_table'.")
                schema, table = qualified.split(".", 1)
                exists = await conn.fetchval(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = $1 AND table_name = $2",
                    schema, table,
                )
                if not exists:
                    raise BackupError(f"No such table: {qualified!r}")
        else:
            raise BackupError(f"Unknown scope_type: {scope_type!r}")
    finally:
        await conn.close()


async def _run_logical_backup(
    job: dict, conn_info: ConnInfo, db_id: str, scope_type: str = "whole", scope: list[str] | None = None,
) -> None:
    scope = scope or []
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    filename = f"{ts}_{conn_info.dbname}.dump"
    path = os.path.join(_db_dir(db_id), filename)
    env = dict(os.environ, PGPASSWORD=conn_info.password)

    await _validate_scope(conn_info, scope_type, scope)

    scope_args = []
    if scope_type == "schemas":
        for schema in scope:
            scope_args += ["-n", f'"{schema}"']
    elif scope_type == "tables":
        for qualified in scope:
            schema, table = qualified.split(".", 1)
            scope_args += ["-t", f'"{schema}"."{table}"']

    scope_label = {
        "whole": "the whole database",
        "schemas": f"schema(s): {', '.join(scope)}",
        "tables": f"table(s): {', '.join(scope)}",
    }[scope_type]
    _log(job, f"=== pg_dump: backing up {conn_info.dbname} ({scope_label}) ===")
    rc = await _run(job, [
        "pg_dump", "-h", conn_info.host, "-p", str(conn_info.port), "-U", conn_info.username,
        "-d", conn_info.dbname, "-Fc", "-f", path, "--no-owner", "--no-privileges", *scope_args,
    ], env)
    if rc != 0:
        if os.path.exists(path):
            os.remove(path)
        raise BackupError("pg_dump failed -- see log for details.")
    size = os.path.getsize(path)
    _write_meta(path, {
        "type": "logical", "created_at": time.time(), "dbname": conn_info.dbname,
        "scope_type": scope_type, "scope": scope,
    })
    _log(job, f"Backup complete: {filename} ({size} bytes)")
    job["filename"] = filename


async def _run_physical_backup(job: dict, conn_info: ConnInfo, db_id: str, incremental_base: str | None) -> None:
    kind_label = "incremental" if incremental_base else "physical"
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dirname = f"{ts}_{conn_info.dbname}_{kind_label}"
    path = os.path.join(_db_dir(db_id), dirname)
    env = dict(os.environ, PGPASSWORD=conn_info.password)

    based_on = None
    args = [
        "pg_basebackup", "-h", conn_info.host, "-p", str(conn_info.port), "-U", conn_info.username,
        "-D", path, "-Ft", "-z", "-X", "stream", "--checkpoint=fast", "-P",
    ]
    if incremental_base:
        await _ensure_summarize_wal(job, conn_info)
        based_on = incremental_base
        args += [f"--incremental={manifest_path(db_id, incremental_base)}"]

    _log(job, f"=== pg_basebackup: {kind_label} backup of the whole server (via {conn_info.dbname}'s connection) ===")
    if kind_label == "physical":
        _log(job, "Note: this captures every database on the server, not just this one -- that's inherent to how physical backups work.")
    rc = await _run(job, args, env)
    if rc != 0:
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        raise BackupError("pg_basebackup failed -- see log for details.")
    size = await asyncio.to_thread(_dir_size, path)
    _write_meta(path, {
        "type": kind_label, "created_at": time.time(), "dbname": conn_info.dbname, "based_on": based_on,
    })
    _log(job, f"Backup complete: {dirname} ({size} bytes)")
    job["filename"] = dirname


async def _run_backup_job(
    job: dict, conn_info: ConnInfo, db_id: str, backup_type: str, incremental_base: str | None,
    scope_type: str, scope: list[str],
) -> None:
    try:
        low, msg = _disk_space_status()
        if low:
            raise BackupError(f"Refusing to start: backup volume is critically low on space -- {msg}")
        _log(job, f"Backup volume: {msg}")

        if backup_type == "logical":
            await _run_logical_backup(job, conn_info, db_id, scope_type, scope)
        elif backup_type in ("physical", "incremental"):
            await _run_physical_backup(job, conn_info, db_id, incremental_base if backup_type == "incremental" else None)
        else:
            raise BackupError(f"Unknown backup type: {backup_type!r}")

        low, msg = _disk_space_status()
        if low:
            _log(job, f"WARNING: backup volume is now critically low on space -- {msg} "
                       "The next backup may fail to complete; free up space or lower retention soon.")
            await audit.log("system", "backup.disk_space_low", detail=msg, db_id=db_id)
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()


async def start_backup(
    db_id: str, conn_info: ConnInfo, backup_type: str = "logical", incremental_base: str | None = None,
    scope_type: str = "whole", scope: list[str] | None = None,
) -> str:
    if backup_type not in BACKUP_TYPES:
        raise BackupError(f"Unknown backup type: {backup_type!r}")
    if backup_type == "incremental" and not incremental_base:
        raise BackupError("Incremental backups need a prior physical/incremental backup to build on.")
    scope = scope or []
    if scope_type not in LOGICAL_SCOPE_TYPES:
        raise BackupError(f"Unknown scope_type: {scope_type!r}")
    if backup_type != "logical" and scope_type != "whole":
        raise BackupError(
            "Selecting schemas/tables only applies to logical backups -- physical/incremental backups "
            "always capture the whole server, that's inherent to how pg_basebackup works."
        )
    job = _new_job("backup")
    asyncio.create_task(_run_backup_job(job, conn_info, db_id, backup_type, incremental_base, scope_type, scope))
    return job["id"]


# ------------------------------------------------------ import roles script
async def _run_import_roles_job(job: dict, conn_info: ConnInfo, sql_path: str, tmp_dir: str) -> None:
    try:
        env = dict(os.environ, PGPASSWORD=conn_info.password)
        _log(job, "=== psql: applying roles script ===")
        rc = await _run(job, [
            "psql", "-h", conn_info.host, "-p", str(conn_info.port), "-U", conn_info.username,
            "-d", conn_info.dbname, "-v", "ON_ERROR_STOP=0", "-f", sql_path,
        ], env)
        if rc != 0:
            raise BackupError(f"psql exited with code {rc} -- see log for details.")
        _log(job, "Roles script applied -- review the log above for any individual statement errors "
                   "(e.g. a role that already existed, which ON_ERROR_STOP=0 lets the rest of the "
                   "script continue past).")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def start_import_roles(conn_info: ConnInfo, src_file) -> str:
    """Applies a `pg_dumpall --roles-only` (or `--globals-only`) script
    brought over from another server -- roles are cluster-global, not
    per-database, so this runs once against the cluster via whichever
    registered database's connection happens to be handy, the same way
    CREATE DATABASE does in _create_database above."""
    tmp_dir = tempfile.mkdtemp(prefix="pgmon-roles-")
    sql_path = os.path.join(tmp_dir, "roles.sql")

    def _save():
        with open(sql_path, "wb") as dst:
            shutil.copyfileobj(src_file, dst)
    await asyncio.to_thread(_save)

    job = _new_job("import-roles")
    asyncio.create_task(_run_import_roles_job(job, conn_info, sql_path, tmp_dir))
    return job["id"]


_SAFE_DBNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def _create_database(conn_info: ConnInfo, new_dbname: str, job: dict) -> None:
    """Creates `new_dbname` on the same server as conn_info -- used for a
    "restore into a new database" logical restore, so the original
    database is never touched at all. Connects to the *existing* database
    to issue CREATE DATABASE (any already-connectable database works for
    this; Postgres only disallows dropping/renaming the one you're
    connected to, not creating siblings)."""
    import asyncpg
    if not _SAFE_DBNAME.match(new_dbname):
        raise BackupError(
            f"{new_dbname!r} isn't a valid database name -- use letters, numbers, and underscores, "
            "starting with a letter or underscore."
        )
    conn = await asyncpg.connect(dsn=conn_info.dsn, timeout=8)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", new_dbname)
        if exists:
            raise BackupError(f"Database {new_dbname!r} already exists -- pick a different name.")
        quoted = await conn.fetchval("SELECT quote_ident($1)", new_dbname)
        _log(job, f"=== Creating new database {new_dbname} ===")
        await conn.execute(f"CREATE DATABASE {quoted}")
    finally:
        await conn.close()


RESTORE_CONTENT_TYPES = ("all", "schema", "data")
RESTORE_SECTIONS = ("pre-data", "data", "post-data")
OWNER_PROVISION_ACTIONS = ("none", "create_if_missing", "recreate")


def _validate_restore_options(raw: dict | None) -> dict:
    """Normalizes and validates the pg_restore-level options a restore can
    ask for -- see the "Restore options" section of the Restore modal.
    Mirrors real pg_restore flags one-to-one (checked against `pg_restore
    --help` on the exact Postgres version this image ships) rather than
    inventing a simplified subset, so the mutual-exclusivity rules below
    are pg_restore's own, not this app's."""
    o = dict(raw or {})
    o["content"] = o.get("content") or "all"
    if o["content"] not in RESTORE_CONTENT_TYPES:
        raise BackupError(f"Unknown content option: {o['content']!r}")
    o["exclude_schemas"] = [str(s) for s in (o.get("exclude_schemas") or [])]
    for flag in (
        "exit_on_error", "single_transaction", "disable_triggers", "no_comments",
        "no_security_labels", "no_publications", "no_subscriptions",
        "no_table_access_method", "no_tablespaces", "preserve_ownership",
        "use_set_session_authorization",
    ):
        o[flag] = bool(o.get(flag, False))

    jobs = o.get("jobs")
    if jobs in ("", None):
        o["jobs"] = None
    else:
        try:
            jobs = int(jobs)
        except (TypeError, ValueError):
            raise BackupError("jobs must be a whole number.")
        if jobs < 1:
            raise BackupError("jobs must be at least 1.")
        o["jobs"] = jobs

    tsize = o.get("transaction_size")
    if tsize in ("", None):
        o["transaction_size"] = None
    else:
        try:
            tsize = int(tsize)
        except (TypeError, ValueError):
            raise BackupError("transaction_size must be a whole number.")
        if tsize < 1:
            raise BackupError("transaction_size must be at least 1.")
        o["transaction_size"] = tsize

    section = o.get("section") or None
    if section and section not in RESTORE_SECTIONS:
        raise BackupError(f"Unknown section: {section!r}")
    o["section"] = section

    if o["jobs"] and o["jobs"] > 1 and o["single_transaction"]:
        raise BackupError("Parallel restore (more than 1 job) can't be combined with a single transaction.")
    if o["transaction_size"] and o["single_transaction"]:
        raise BackupError("transaction_size can't be combined with a single transaction.")
    if section and o["content"] != "all":
        raise BackupError("A specific section can't be combined with a schema-only or data-only restore -- pick one.")
    if o["use_set_session_authorization"] and not o["preserve_ownership"]:
        raise BackupError("use_set_session_authorization only applies when ownership is preserved.")
    if o["disable_triggers"] and o["content"] != "data":
        o["disable_triggers"] = False  # harmless no-op per pg_restore's own docs; drop it rather than pass it through

    reassign = o.get("reassign_owner") or None
    if reassign:
        if o["preserve_ownership"]:
            raise BackupError(
                "Reassigning ownership to a role and preserving original ownership from the dump "
                "can't be combined -- pick one."
            )
        reassign = dict(reassign)
        reassign["rolname"] = (reassign.get("rolname") or "").strip()
        reassign["provision"] = reassign.get("provision") or "none"
        if reassign["provision"] not in OWNER_PROVISION_ACTIONS:
            raise BackupError(f"Unknown provision option: {reassign['provision']!r}")
        if not reassign["rolname"]:
            raise BackupError("Specify a role name to reassign ownership to.")
    o["reassign_owner"] = reassign
    return o


async def _reassign_after_restore(conn_info: ConnInfo, restore_dbname: str, reassign: dict, job: dict) -> None:
    """Runs after a successful restore that used the default --no-owner
    (everything ends up owned by the connecting role): moves ownership of
    everything just restored to a chosen role, optionally creating or
    recreating that role first -- the practical "drop and create a new
    user account while restoring" flow, since a per-database dump never
    carries CREATE ROLE itself (roles are cluster-global, not per-db)."""
    import asyncpg
    rolname = reassign["rolname"]
    provision = reassign["provision"]
    target_conn_info = ConnInfo(
        host=conn_info.host, port=conn_info.port, dbname=restore_dbname,
        username=conn_info.username, password=conn_info.password,
    )
    conn = await asyncpg.connect(dsn=target_conn_info.dsn, timeout=8)
    try:
        pool_shim = _SingleConnPool(conn)
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", rolname)
        try:
            if provision == "recreate" and exists:
                _log(job, f"=== Dropping existing role {rolname!r} before recreating ===")
                for line in await roles_mod.drop_role(pool_shim, rolname, reassign_to=conn_info.username):
                    _log(job, line)
                exists = False
            if provision in ("create_if_missing", "recreate") and not exists:
                _log(job, f"=== Creating role {rolname!r} ===")
                await roles_mod.create_role(pool_shim, {
                    "rolname": rolname, "password": reassign.get("password") or None, "can_login": True,
                })
            elif not exists:
                raise BackupError(f"Role {rolname!r} doesn't exist -- choose 'create if missing' or 'recreate'.")
        except roles_mod.RoleError as e:
            raise BackupError(str(e))

        qname = await conn.fetchval("SELECT quote_ident($1)", rolname)
        _log(job, f"=== Reassigning ownership of every restored object to {rolname!r} ===")
        try:
            # Not REASSIGN OWNED BY: that command refuses outright the
            # instant the FROM role has ANY pinned shared dependency, which
            # is true of the cluster's bootstrap superuser (conventionally
            # named "postgres") regardless of what else it owns in this
            # database -- and restoring as that role is the common case,
            # not an edge case. Per-object ALTER ... OWNER TO has no such
            # restriction, so walk the catalog for what the restore (via
            # --no-owner) actually left owned by the connecting role instead.
            schema_rows = await conn.fetch(
                "SELECT nspname FROM pg_namespace WHERE nspowner = "
                "(SELECT oid FROM pg_roles WHERE rolname = $1) AND nspname NOT IN "
                "('pg_catalog', 'information_schema', 'pg_toast')",
                conn_info.username,
            )
            for row in schema_rows:
                qschema = await conn.fetchval("SELECT quote_ident($1)", row["nspname"])
                await conn.execute(f"ALTER SCHEMA {qschema} OWNER TO {qname}")

            # relkind -> the ALTER keyword it takes; covers the object kinds
            # a typical application dump actually has ownership on. Doesn't
            # cover functions/types/etc -- those stay owned by the
            # connecting role if the dump had any. Tables/views/matviews/
            # foreign tables first, sequences after and re-queried rather
            # than selected up front: a serial/identity column's sequence
            # is "owned by" (in the ALTER SEQUENCE ... OWNED BY sense, not
            # the ownership sense) its table, and Postgres refuses to ALTER
            # OWNER on one of those directly ("is linked to table ...") --
            # reassigning the table first auto-cascades its linked
            # sequence's ownership too, so re-checking after only catches
            # genuinely standalone sequences.
            async def _reassign_relkinds(kinds: str) -> int:
                rows = await conn.fetch(
                    "SELECT n.nspname, c.relname, c.relkind::text AS relkind FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = $1) "
                    "AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
                    f"AND c.relkind IN ({kinds})",
                    conn_info.username,
                )
                for r in rows:
                    kind = relkind_sql[r["relkind"]]
                    qschema = await conn.fetchval("SELECT quote_ident($1)", r["nspname"])
                    qrel = await conn.fetchval("SELECT quote_ident($1)", r["relname"])
                    await conn.execute(f"ALTER {kind} {qschema}.{qrel} OWNER TO {qname}")
                return len(rows)

            relkind_sql = {
                "r": "TABLE", "p": "TABLE", "v": "VIEW", "m": "MATERIALIZED VIEW",
                "S": "SEQUENCE", "f": "FOREIGN TABLE",
            }
            n_rel = await _reassign_relkinds("'r', 'p', 'v', 'm', 'f'")
            n_rel += await _reassign_relkinds("'S'")
        except asyncpg.PostgresError as e:
            raise BackupError(f"Ownership reassignment failed: {e}")
        _log(job, f"Ownership of {len(schema_rows)} schema(s) and {n_rel} table(s)/view(s)/sequence(s) "
                   f"reassigned to {rolname!r}.")
    finally:
        await conn.close()


async def _validate_restore_scope(db_id: str, filename: str, scope_type: str, scope: list[str]) -> None:
    if scope_type == "whole":
        return
    if not scope:
        raise BackupError(f"Select at least one {'schema' if scope_type == 'schemas' else 'table'} to restore.")
    available = await list_backup_tables(db_id, filename)
    available_schemas = {t.split(".", 1)[0] for t in available}
    if scope_type == "schemas":
        for schema in scope:
            if schema not in available_schemas:
                raise BackupError(f"{filename} doesn't contain any tables in schema {schema!r}.")
    elif scope_type == "tables":
        for qualified in scope:
            if qualified not in available:
                raise BackupError(f"{filename} doesn't contain table {qualified!r}.")
    else:
        raise BackupError(f"Unknown scope_type: {scope_type!r}")


async def _run_restore_job(
    job: dict, conn_info: ConnInfo, db_id: str, filename: str, target_dbname: str | None,
    scope_type: str = "whole", scope: list[str] | None = None, options: dict | None = None,
) -> None:
    scope = scope or []
    try:
        options = _validate_restore_options(options)
        path = backup_path(db_id, filename)
        meta = _read_meta(path)
        if meta.get("type") not in (None, "logical"):
            raise BackupError(
                f"{filename} is a {meta.get('type')} backup -- pgmon doesn't automate restoring those. "
                + RESTORE_INSTRUCTIONS
            )
        await _validate_restore_scope(db_id, filename, scope_type, scope)

        if target_dbname:
            await _create_database(conn_info, target_dbname, job)
            restore_dbname = target_dbname
        else:
            restore_dbname = conn_info.dbname

        if target_dbname and scope_type == "tables":
            # pg_restore -t only restores the named table itself -- the
            # schema it lives in has to already exist in the target for
            # CREATE TABLE to succeed. A brand-new database only has
            # "public" by default, so a non-public schema needs creating
            # first; a same-database restore doesn't have this problem
            # since that schema is already there (it's where the backup
            # came from).
            needed_schemas = sorted({q.split(".", 1)[0] for q in scope} - {"public"})
            if needed_schemas:
                _log(job, f"Creating schema(s) {', '.join(needed_schemas)} in {target_dbname} before restoring into them...")
                import asyncpg
                new_db_conn_info = ConnInfo(
                    host=conn_info.host, port=conn_info.port, dbname=target_dbname,
                    username=conn_info.username, password=conn_info.password,
                )
                conn = await asyncpg.connect(dsn=new_db_conn_info.dsn, timeout=8)
                try:
                    for schema in needed_schemas:
                        quoted = await conn.fetchval("SELECT quote_ident($1)", schema)
                        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted}")
                finally:
                    await conn.close()

        scope_label = {
            "whole": "the whole backup",
            "schemas": f"schema(s): {', '.join(scope)}",
            "tables": f"table(s): {', '.join(scope)}",
        }[scope_type]
        env = dict(os.environ, PGPASSWORD=conn_info.password)
        _log(job, f"=== pg_restore: restoring {filename} into {restore_dbname} ({scope_label}) ===")

        base_args = [
            "pg_restore", "-h", conn_info.host, "-p", str(conn_info.port), "-U", conn_info.username,
            "-d", restore_dbname,
        ]
        if options["preserve_ownership"]:
            if options["use_set_session_authorization"]:
                base_args.append("--use-set-session-authorization")
        else:
            base_args.append("--no-owner")
        base_args.append("--no-privileges")

        if options["content"] == "schema":
            base_args.append("--schema-only")
        elif options["content"] == "data":
            base_args.append("--data-only")
            if options["disable_triggers"]:
                base_args.append("--disable-triggers")

        if not target_dbname and options["content"] != "data":
            # --clean only makes sense (and is only safe) against the
            # existing database, which may already have objects to drop
            # before recreating from the dump -- a freshly created target
            # database is empty, so there's nothing to clean, and a
            # data-only restore isn't recreating schema objects at all.
            base_args += ["--clean", "--if-exists"]

        for excluded_schema in options["exclude_schemas"]:
            base_args += ["-N", excluded_schema]
        if options["exit_on_error"]:
            base_args.append("--exit-on-error")
        if options["single_transaction"]:
            base_args.append("--single-transaction")
        if options["jobs"] and options["jobs"] > 1:
            base_args += ["--jobs", str(options["jobs"])]
        if options["no_comments"]:
            base_args.append("--no-comments")
        if options["no_security_labels"]:
            base_args.append("--no-security-labels")
        if options["no_publications"]:
            base_args.append("--no-publications")
        if options["no_subscriptions"]:
            base_args.append("--no-subscriptions")
        if options["no_table_access_method"]:
            base_args.append("--no-table-access-method")
        if options["no_tablespaces"]:
            base_args.append("--no-tablespaces")
        if options["section"]:
            base_args += ["--section", options["section"]]
        if options["transaction_size"]:
            base_args += ["--transaction-size", str(options["transaction_size"])]

        if scope_type == "tables":
            # One pg_restore invocation per table, each with its own single
            # "-n schema -t table" pair -- NOT one invocation with every
            # pair accumulated together. pg_restore's -n/-t filters combine
            # as (any -n schema) AND (any -t table), not per-pair
            # intersection, so a combined call risks a false-positive match
            # if two different selected schemas happen to each have a
            # table with the same name. Also NOT a single schema-qualified
            # "-t schema.table" pattern per table (unlike pg_dump's -t,
            # which does support that): empirically, pg_restore's -t plainly
            # doesn't match a dotted or quoted-qualified pattern in this
            # Postgres version ("implied no-schema restore", zero objects
            # restored, no error) -- only a bare table name paired with -n
            # actually works.
            runs = [(f"{schema}.{table}", ["-n", schema, "-t", table])
                    for qualified in scope for schema, table in [qualified.split(".", 1)]]
        else:
            scope_args = [a for schema in scope for a in ("-n", schema)] if scope_type == "schemas" else []
            runs = [(scope_label, scope_args)]

        worst_rc = 0
        for label, scope_args in runs:
            if len(runs) > 1:
                _log(job, f"--- {label} ---")
            rc = await _run(job, [*base_args, *scope_args, path], env)
            worst_rc = max(worst_rc, rc)
        rc = worst_rc
        # pg_restore exits 1 for "completed with warnings" (e.g. --if-exists
        # still logging a notice for an object that wasn't there yet on a
        # first-ever restore) as well as for genuine failures -- there's no
        # clean way to tell those apart from the exit code alone, so surface
        # the full log either way and let the operator judge; only treat a
        # crash/unrecognized exit as a hard failure.
        if rc not in (0, 1):
            raise BackupError(f"pg_restore exited with code {rc} -- see log for details.")
        _log(job, "Restore finished." if rc == 0 else "Restore finished with warnings -- review the log above.")
        if rc == 1 and any('unrecognized configuration parameter "transaction_timeout"' in line for line in job["log"]):
            # Confirmed benign, not guessed: pg_restore itself (not the dump
            # file) always sends a few fixed session-setup SET statements
            # on every restore, based on its own compiled version rather
            # than anything about the dump or the target -- this image
            # deliberately bundles a pg_dump/pg_restore newer than almost
            # any target (see Dockerfile), so restoring into a target older
            # than PG17 (which introduced transaction_timeout) always trips
            # this one specific, harmless statement failure. Surfaced as
            # its own note rather than silently dropped, since an
            # unexplained "ERROR: unrecognized configuration parameter" in
            # the log reasonably looks like a failure otherwise -- verified
            # directly (restored data confirmed intact afterward) that it
            # isn't one.
            _log(job, "Note: the \"transaction_timeout\" error above is expected and harmless when restoring "
                      "into a Postgres server older than version 17 -- it's pg_restore's own fixed connection "
                      "setup, unrelated to your data, which restores normally despite it.")
        if target_dbname:
            _log(job, f"Restored into new database {target_dbname!r} -- the original database was never touched.")

        if options["reassign_owner"]:
            await _reassign_after_restore(conn_info, restore_dbname, options["reassign_owner"], job)

        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()


async def start_restore(
    db_id: str, conn_info: ConnInfo, filename: str, target_dbname: str | None = None,
    scope_type: str = "whole", scope: list[str] | None = None, options: dict | None = None,
) -> str:
    if scope_type not in LOGICAL_SCOPE_TYPES:
        raise BackupError(f"Unknown scope_type: {scope_type!r}")
    options = _validate_restore_options(options)  # validate up front so a bad payload fails before the job even starts
    job = _new_job("restore")
    asyncio.create_task(_run_restore_job(job, conn_info, db_id, filename, target_dbname, scope_type, scope or [], options))
    return job["id"]


# ------------------------------------------------- physical/incremental restore
def _kubectl(*args: str) -> list[str]:
    return ["kubectl", "-n", K8S_NAMESPACE, *args]


async def _wait_pod_gone(job: dict, pod_name: str, timeout: int = 90) -> None:
    _log(job, f"Waiting for {pod_name} to fully terminate...")
    await _run(job, _kubectl("wait", "--for=delete", f"pod/{pod_name}", f"--timeout={timeout}s"), os.environ)
    # A pod that's already gone by the time `wait` starts exits non-zero
    # with "not found" rather than 0 -- that's success too, so check state
    # directly rather than trusting rc alone.
    check = await asyncio.create_subprocess_exec(
        *_kubectl("get", "pod", pod_name), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    still_exists = await check.wait() == 0
    if still_exists:
        raise BackupError(f"{pod_name} did not terminate within {timeout}s.")


async def _pod_exists(pod_name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        *_kubectl("get", "pod", pod_name), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


async def _wait_pod_ready(job: dict, pod_name: str, timeout: int = 120) -> None:
    # Right after a StatefulSet scale-up, the pod object itself may not
    # exist yet for a moment (the controller hasn't created it) --
    # `kubectl wait --for=condition=Ready` on a pod that doesn't exist
    # fails immediately with NotFound rather than waiting for it to
    # appear, unlike --for=delete (where "not found" trivially means
    # success). Poll for existence first so that race doesn't surface as
    # a false failure.
    _log(job, f"Waiting for {pod_name} to be created...")
    deadline = asyncio.get_event_loop().time() + min(30, timeout)
    while not await _pod_exists(pod_name):
        if asyncio.get_event_loop().time() > deadline:
            raise BackupError(f"{pod_name} was not created in time.")
        await asyncio.sleep(1)

    _log(job, f"Waiting for {pod_name} to become ready...")
    rc = await _run(job, _kubectl("wait", "--for=condition=Ready", f"pod/{pod_name}", f"--timeout={timeout}s"), os.environ)
    if rc != 0:
        raise BackupError(f"{pod_name} did not become ready within {timeout}s.")


def _resolve_chain(db_id: str, filename: str) -> list[dict]:
    """Walks `based_on` links back from `filename` to the full physical
    backup it ultimately descends from, returning the chain oldest-first
    (so pg_combinebackup gets its arguments in the right order)."""
    chain = []
    seen = set()
    current = filename
    while current:
        if current in seen:
            raise BackupError(f"Backup chain has a cycle at {current!r} -- refusing to restore.")
        seen.add(current)
        path = _entry_path(db_id, current)
        if not os.path.isdir(path):
            raise BackupError(f"{current} is missing or not a physical/incremental backup -- the restore chain is broken.")
        meta = _read_meta(path)
        backup_type = meta.get("type")
        if backup_type not in ("physical", "incremental"):
            raise BackupError(f"{current} is a {backup_type or 'unknown'} backup -- only physical/incremental backups can be chained.")
        chain.append({"filename": current, "type": backup_type})
        current = meta.get("based_on")
    chain.reverse()
    if chain[0]["type"] != "physical":
        raise BackupError("Restore chain doesn't end in a full physical backup -- can't restore.")
    return chain


async def _pod_node(job: dict, pod_name: str) -> str | None:
    """The node `pod_name` is currently running on -- captured from the
    live Postgres pod *before* scaling it down, and used to pin the
    restore worker pod to the same node.

    Local storage classes (local-path, hostPath-backed, etc) bind a PV to
    exactly one node via a hard nodeAffinity; a worker pod scheduled
    anywhere else gets stuck Pending forever trying to mount it, with no
    clear error surfaced by `kubectl wait`'s timeout alone. Since the
    Postgres pod is already successfully mounting this same PVC right
    here, its node is guaranteed valid -- reading it needs only the
    `pods` read access already granted, unlike reading the PV/PVC's own
    nodeAffinity directly, which would need extra, broader (cluster-
    scoped) RBAC just for this one check."""
    proc = await asyncio.create_subprocess_exec(
        *_kubectl("get", "pod", pod_name, "-o", "jsonpath={.spec.nodeName}"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    node = out.decode().strip()
    if node:
        _log(job, f"{pod_name} is running on {node} -- pinning the restore worker pod there too.")
    return node or None


def _worker_pod_manifest(worker_name: str, node_name: str | None, pvc_name: str | None = None) -> str:
    # Runs as root (unlike this app's own appuser) specifically so it can
    # chown the extracted files to Postgres's own uid:gid before handing
    # the data directory back -- a short-lived, single-purpose pod, not
    # something left running. Shared between in-place restore (pvc_name
    # defaults to the live server's own PVC) and new-instance restore
    # (pvc_name is a freshly created PVC nothing else references yet).
    node_selector = f"""
  nodeSelector:
    kubernetes.io/hostname: {node_name}""" if node_name else ""
    return f"""
apiVersion: v1
kind: Pod
metadata:
  name: {worker_name}
  namespace: {K8S_NAMESPACE}
  labels:
    app: pg-ai-monitor-restore-worker
spec:
  restartPolicy: Never{node_selector}
  securityContext:
    runAsUser: 0
    runAsGroup: 0
  containers:
  - name: restore
    image: {RESTORE_WORKER_IMAGE}
    imagePullPolicy: IfNotPresent
    command: ["sleep", "3600"]
    volumeMounts:
    - name: pgdata
      mountPath: {K8S_MOUNT_PATH}
  volumes:
  - name: pgdata
    persistentVolumeClaim:
      claimName: {pvc_name or K8S_PVC}
"""


async def _kubectl_apply(job: dict, manifest: str, timeout: float = 60) -> None:
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "apply", "-f", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(manifest.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackupError(f"kubectl apply timed out after {timeout}s.")
    _log(job, out.decode(errors="replace"))
    if proc.returncode != 0:
        raise BackupError("Failed to create the restore worker pod -- see log for details.")


async def _mkdir_in_pod(job: dict, pod_name: str, path: str, timeout: float = 30) -> None:
    rc = await _run(job, _kubectl("exec", pod_name, "--", "mkdir", "-p", path), os.environ, timeout=timeout)
    if rc != 0:
        raise BackupError(f"Failed to create {path} inside the restore worker pod.")


async def _exec_in_pod(job: dict, pod_name: str, script: str, timeout: float = 300) -> None:
    # Every restore/combine step (extraction, pg_combinebackup, chown) runs
    # as this one script -- see the _run() docstring for why a hard
    # timeout matters here specifically: Postgres is offline the whole
    # time this runs.
    proc = await asyncio.create_subprocess_exec(
        *_kubectl("exec", "-i", pod_name, "--", "sh"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(script.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    async def _drain():
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            _log(job, line.decode(errors="replace"))
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        _log(job, f"TIMEOUT after {timeout}s waiting for the restore script inside the worker pod.")
        proc.kill()
        await proc.wait()
        raise BackupError(f"Restore script timed out after {timeout}s inside the worker pod.")
    if rc != 0:
        raise BackupError("Restore script failed inside the worker pod -- see log for details.")


async def prune_wal_archive_before(cutoff_epoch: float) -> int:
    """Deletes archived WAL files whose mtime is older than cutoff_epoch,
    from the primary's own archive volume. Called by app/scheduler.py with
    a cutoff derived from the oldest physical/incremental backup still
    being kept -- day-granularity (`find -mtime`), not exact LSN math,
    deliberately: guessing wrong toward "delete too much" silently breaks a
    kept backup's restorability, while guessing wrong toward "keep too
    much" only costs disk. Returns the number of files deleted (best
    effort -- 0 if WAL_ARCHIVE_PATH/physical-restore isn't configured, or
    on any kubectl failure, rather than raising into a background loop)."""
    if not WAL_ARCHIVE_PATH or not is_physical_restore_configured():
        return 0
    age_days = int((time.time() - cutoff_epoch) // 86400)
    if age_days < 1:
        return 0
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "-n", K8S_NAMESPACE, "exec", K8S_POD_NAME, "--",
        "find", WAL_ARCHIVE_PATH, "-type", "f", "-mtime", f"+{age_days}", "-print", "-delete",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return 0
    return len([line for line in out.decode(errors="replace").splitlines() if line.strip()])


async def _copy_wal_archive(job: dict, worker_name: str) -> None:
    """Copies every archived WAL segment off the live primary's archive
    volume, through this machine, into the restore worker pod -- avoiding
    ever mounting that (ReadWriteOnce) PVC on two pods at once. Landed
    under {mount}/wal_restore/ inside the worker; a PITR restore's
    restore_command searches it with `find` rather than assuming a fixed
    nesting depth, since `kubectl cp` of a directory nests one level under
    the source's own basename and that's not worth relying on exactly."""
    if not WAL_ARCHIVE_PATH:
        raise BackupError("Point-in-time recovery isn't configured (PHYSICAL_RESTORE_WAL_ARCHIVE_PATH is unset).")
    tmp_local = tempfile.mkdtemp(prefix="pgmon-wal-")
    try:
        _log(job, "=== Copying archived WAL from the live primary (for point-in-time recovery) ===")
        rc = await _run(job, ["kubectl", "cp", f"{K8S_NAMESPACE}/{K8S_POD_NAME}:{WAL_ARCHIVE_PATH}", tmp_local],
                         os.environ, timeout=180)
        copied = os.listdir(tmp_local) if os.path.isdir(tmp_local) else []
        if rc != 0 or not copied:
            raise BackupError("Failed to copy the WAL archive off the primary (or it's empty).")
        _log(job, f"Copied {len(copied)} archived WAL file(s) locally, forwarding into the restore worker...")
        await _mkdir_in_pod(job, worker_name, f"{K8S_MOUNT_PATH}/wal_restore")
        rc = await _run(job, ["kubectl", "cp", tmp_local, f"{K8S_NAMESPACE}/{worker_name}:{K8S_MOUNT_PATH}/wal_restore"],
                         os.environ, timeout=180)
        if rc != 0:
            raise BackupError("Failed to copy the WAL archive into the restore worker.")
    finally:
        shutil.rmtree(tmp_local, ignore_errors=True)


def _validate_tablespace_mapping(raw: dict | None) -> dict[str, str]:
    """oid -> absolute destination path on the restore worker pod. Only
    exercised when a backup's own tablespace_map file (written by
    pg_basebackup only when the source server actually had non-default
    tablespaces) is present after extraction -- every tablespace-free
    backup (the only kind anywhere in this project's own clusters) never
    touches this code at all. There's no sane default destination on an
    ephemeral worker pod for a path that belonged to the *source*
    server's filesystem, so a mapping is required per oid found, not
    optional -- see the shell logic in _build_restore_script."""
    mapping = raw or {}
    out: dict[str, str] = {}
    for oid, path in mapping.items():
        oid = str(oid)
        if not oid.isdigit():
            raise BackupError(f"Invalid tablespace oid: {oid!r} (must be numeric).")
        path = str(path or "").strip()
        if not path.startswith("/") or "'" in path or '"' in path or "\n" in path:
            raise BackupError(f"Invalid destination path for tablespace oid {oid}: {path!r}.")
        out[oid] = path
    return out


def _build_restore_script(
    chain: list[dict], recovery_target: dict | None = None, tablespace_mapping: dict[str, str] | None = None,
) -> str:
    tablespace_mapping = tablespace_mapping or {}
    ts_case_body = "\n".join(
        f'      {oid}) new_path="{path}" ;;' for oid, path in tablespace_mapping.items()
    ) or "      *) : ;;"

    extract_lines = []
    combine_dirs = []
    for i, entry in enumerate(chain):
        # `kubectl cp <local-dir> pod:/restore-work/{i}` lands the backup's
        # contents nested one level deeper, under a directory named for
        # the source's own basename -- i.e. /restore-work/{i}/<filename>/... --
        # not flattened directly into /restore-work/{i}/, regardless of a
        # trailing slash on the source (verified empirically; kubectl cp's
        # docs don't actually promise flattening).
        src = f'/restore-work/{i}/{entry["filename"]}'
        d = f"/restore-work/{i}"
        extract_lines.append(f"""
mkdir -p "{d}/extracted"
tar -xzf "{src}/base.tar.gz" -C "{d}/extracted"
if [ -f "{src}/pg_wal.tar.gz" ]; then
  mkdir -p "{d}/extracted/pg_wal"
  tar -xzf "{src}/pg_wal.tar.gz" -C "{d}/extracted/pg_wal"
fi
if [ -f "{d}/extracted/tablespace_map" ]; then
  while read -r ts_oid ts_orig_path; do
    [ -z "$ts_oid" ] && continue
    ts_tar="{src}/$ts_oid.tar.gz"
    if [ ! -f "$ts_tar" ]; then
      echo "Backup references tablespace oid $ts_oid but $ts_tar is missing from it -- can't restore it." >&2
      exit 1
    fi
    new_path=""
    case "$ts_oid" in
{ts_case_body}
    esac
    if [ -z "$new_path" ]; then
      echo "Backup uses tablespace oid $ts_oid (originally at $ts_orig_path) but no destination mapping was given for it." >&2
      exit 1
    fi
    mkdir -p "$new_path"
    tar -xzf "$ts_tar" -C "$new_path"
    sed -i "s|^$ts_oid[[:space:]].*|$ts_oid $new_path|" "{d}/extracted/tablespace_map"
  done < "{d}/extracted/tablespace_map"
fi""")
        combine_dirs.append(f'"{d}/extracted"')

    data_dir = f"{K8S_MOUNT_PATH}/data"
    new_dir = f"{K8S_MOUNT_PATH}/data_new"
    if len(chain) == 1:
        assemble = f'rm -rf "{new_dir}"\nmv "/restore-work/0/extracted" "{new_dir}"'
    else:
        assemble = f'rm -rf "{new_dir}"\npg_combinebackup {" ".join(combine_dirs)} -o "{new_dir}"'

    recovery_block = ""
    if recovery_target:
        # `find`, not a fixed path, because kubectl cp's exact nesting depth
        # for the copied archive directory isn't worth relying on (see
        # _copy_wal_archive). Quoted heredoc delimiter (<<'PGMONEOF') so the
        # shell doesn't try to expand $(...)/"%p" itself while *writing*
        # this file -- it needs to reach postgresql.auto.conf as literal
        # text for Postgres to execute later, at actual recovery time.
        target_line = {
            "time": f"recovery_target_time = '{recovery_target['value']}'",
            "lsn": f"recovery_target_lsn = '{recovery_target['value']}'",
            "xid": f"recovery_target_xid = '{recovery_target['value']}'",
            "name": f"recovery_target_name = '{recovery_target['value']}'",
        }[recovery_target["type"]]
        recovery_block = f"""
cat >> "{data_dir}/postgresql.auto.conf" <<'PGMONEOF'
restore_command = 'f=$(find {K8S_MOUNT_PATH}/wal_restore -name "%f" 2>/dev/null | head -1); test -n "$f" && cp "$f" "%p"'
{target_line}
recovery_target_inclusive = '{"true" if recovery_target["inclusive"] else "false"}'
recovery_target_action = '{recovery_target["action"]}'
recovery_target_timeline = '{recovery_target["timeline"]}'
PGMONEOF
touch "{data_dir}/recovery.signal"
"""

    return f"""set -e
{"".join(extract_lines)}
{assemble}
chown -R 1001:1001 "{new_dir}"
chmod 700 "{new_dir}"
rm -rf "{data_dir}"
mv "{new_dir}" "{data_dir}"
{recovery_block}
rm -rf /restore-work
echo RESTORE_SCRIPT_OK
"""


async def _run_physical_restore_job(job: dict, db_id: str, filename: str, tablespace_mapping: dict[str, str] | None = None) -> None:
    try:
        if not is_physical_restore_configured():
            raise BackupError(
                "Physical/incremental restore isn't configured on this deployment "
                "(PHYSICAL_RESTORE_NAMESPACE/STATEFULSET/PVC env vars are unset). " + RESTORE_INSTRUCTIONS
            )
        chain = _resolve_chain(db_id, filename)
        _log(job, "Restore chain (oldest first): " + " -> ".join(c["filename"] for c in chain))
        worker_node = await _pod_node(job, K8S_POD_NAME)

        _log(job, f"=== Scaling down {K8S_STATEFULSET} in {K8S_NAMESPACE} ===")
        rc = await _run(job, _kubectl("scale", "statefulset", K8S_STATEFULSET, "--replicas=0"), os.environ)
        if rc != 0:
            raise BackupError("Failed to scale down the Postgres StatefulSet.")
        await _wait_pod_gone(job, K8S_POD_NAME)

        worker_name = f"pg-ai-monitor-restore-{uuid.uuid4().hex[:8]}"
        try:
            _log(job, f"=== Starting restore worker pod {worker_name} ===")
            await _kubectl_apply(job, _worker_pod_manifest(worker_name, worker_node))
            await _wait_pod_ready(job, worker_name)

            for i, entry in enumerate(chain):
                src = _entry_path(db_id, entry["filename"])
                remote_dir = f"/restore-work/{i}"
                dest = f"{K8S_NAMESPACE}/{worker_name}:{remote_dir}"
                # `kubectl cp` extracts into the destination via a remote
                # tar -x -- that fails immediately (source of an earlier
                # hang: the local tar side didn't handle the remote
                # rejection cleanly) unless the directory already exists.
                await _mkdir_in_pod(job, worker_name, remote_dir)
                _log(job, f"Copying {entry['filename']} into the worker pod...")
                rc = await _run(job, ["kubectl", "cp", src, dest], os.environ, timeout=120)
                if rc != 0:
                    raise BackupError(f"Failed to copy {entry['filename']} into the restore worker.")

            _log(job, "=== Extracting and assembling the data directory ===")
            await _exec_in_pod(job, worker_name, _build_restore_script(chain, tablespace_mapping=tablespace_mapping))
        finally:
            _log(job, f"Removing restore worker pod {worker_name}...")
            await _run(job, _kubectl("delete", "pod", worker_name, "--wait=true", "--timeout=60s", "--ignore-not-found=true"), os.environ)

        _log(job, f"=== Scaling {K8S_STATEFULSET} back up ===")
        rc = await _run(job, _kubectl("scale", "statefulset", K8S_STATEFULSET, "--replicas=1"), os.environ)
        if rc != 0:
            raise BackupError(
                "Restore data was written, but scaling Postgres back up failed -- "
                f"scale {K8S_STATEFULSET} to 1 replica manually in namespace {K8S_NAMESPACE}."
            )
        await _wait_pod_ready(job, K8S_POD_NAME)
        _log(job, "Restore complete -- Postgres is back up.")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
        # Best-effort: don't leave the database scaled to 0 after a failed
        # restore attempt if it's just the later steps that failed.
        if is_physical_restore_configured():
            _log(job, f"Attempting to scale {K8S_STATEFULSET} back up after the error above...")
            await _run(job, _kubectl("scale", "statefulset", K8S_STATEFULSET, "--replicas=1"), os.environ)
    finally:
        job["finished_at"] = time.time()


async def start_physical_restore(db_id: str, filename: str, tablespace_mapping: dict[str, str] | None = None) -> str:
    tablespace_mapping = _validate_tablespace_mapping(tablespace_mapping)
    job = _new_job("physical-restore")
    asyncio.create_task(_run_physical_restore_job(job, db_id, filename, tablespace_mapping))
    return job["id"]


# --------------------------------------------- restore to a new temp instance
def _safe_k8s_name(name: str) -> str:
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,60}[a-z0-9]$", name):
        raise BackupError(f"Invalid instance name: {name!r}")
    return name


async def list_temp_instances() -> list[dict]:
    if not K8S_NAMESPACE:
        return []
    proc = await asyncio.create_subprocess_exec(
        "kubectl", "-n", K8S_NAMESPACE, "get", "pods", "-l", f"app={TEMP_INSTANCE_LABEL}", "-o", "json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError:
        return []
    result = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        labels = item["metadata"].get("labels", {})
        conditions = item.get("status", {}).get("conditions") or []
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        result.append({
            "name": name,
            "backup_filename": labels.get("backup-filename", "unknown"),
            "created_at": item["metadata"].get("creationTimestamp"),
            "phase": item.get("status", {}).get("phase"),
            "ready": ready,
            "host": f"{name}.{K8S_NAMESPACE}.svc.cluster.local",
            "port": 5432,
        })
    return result


async def delete_temp_instance(name: str) -> None:
    _safe_k8s_name(name)
    for kind, res_name in (("service", name), ("pod", name), ("pvc", f"{name}-data")):
        proc = await asyncio.create_subprocess_exec(
            "kubectl", "-n", K8S_NAMESPACE, "delete", kind, res_name, "--ignore-not-found=true", "--wait=false",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


def _temp_pvc_manifest(name: str) -> str:
    return f"""
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {name}-data
  namespace: {K8S_NAMESPACE}
  labels:
    app: {TEMP_INSTANCE_LABEL}
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: {TEMP_STORAGE_CLASS}
  resources:
    requests:
      storage: {TEMP_PVC_SIZE}
"""


def _temp_instance_manifest(name: str, node_name: str | None, backup_filename: str, pitr: bool = False) -> str:
    # Uses the Bitnami image's own entrypoint (no command/args override) --
    # NOT direct invocation of the postgres binary. Found the hard way:
    # Bitnami keeps postgresql.conf and pg_hba.conf *outside* the data
    # directory (/opt/bitnami/postgresql/conf/, generated fresh at
    # container start), never inside PGDATA -- so pg_basebackup's
    # base.tar.gz, which only contains what's actually inside PGDATA, never
    # captured postgresql.conf at all, and starting the bare postgres
    # binary against the restored directory fails outright looking for it.
    # The entrypoint already handles "PGDATA has an existing valid
    # cluster" correctly (it's exactly what happens on every normal
    # restart of the real server), regenerating that external config
    # itself -- so letting it run, rather than bypassing it, is both
    # simpler and matches a path Bitnami actually tests. POSTGRESQL_PASSWORD
    # is required by the entrypoint's own startup validation but is not
    # applied to an already-initialized cluster (the restored password
    # from the backup is what's actually in effect).
    node_selector = f"""
  nodeSelector:
    kubernetes.io/hostname: {node_name}""" if node_name else ""
    return f"""
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {K8S_NAMESPACE}
  labels:
    app: {TEMP_INSTANCE_LABEL}
    instance: {name}
    backup-filename: {backup_filename}
spec:
  restartPolicy: Never{node_selector}
  securityContext:
    fsGroup: 1001
  containers:
  - name: postgresql
    image: {TEMP_INSTANCE_IMAGE}
    imagePullPolicy: IfNotPresent
    securityContext:
      runAsUser: 1001
      runAsGroup: 1001
      runAsNonRoot: true
    env:
    - name: POSTGRESQL_PASSWORD
      value: "not-used-existing-cluster"
    - name: POSTGRESQL_VOLUME_DIR
      value: "{K8S_MOUNT_PATH}"
    - name: PGDATA
      value: "{K8S_MOUNT_PATH}/data"{f'''
    - name: POSTGRESQL_PERFORM_RESTORE
      value: "yes"''' if pitr else ""}
    ports:
    - containerPort: 5432
    volumeMounts:
    - name: pgdata
      mountPath: {K8S_MOUNT_PATH}
    # The entrypoint writes its generated postgresql.conf/pg_hba.conf
    # under /opt/bitnami/postgresql/conf, and needs /tmp too -- all three
    # need to be an actual writable volume, not the read-only image layer
    # (uid 1001 doesn't own those paths in the image itself). Mirrors the
    # real Postgres deployment's own volume layout for these paths.
    - name: scratch
      mountPath: /tmp
      subPath: tmp-dir
    - name: scratch
      mountPath: /opt/bitnami/postgresql/conf
      subPath: app-conf-dir
    - name: scratch
      mountPath: /opt/bitnami/postgresql/tmp
      subPath: app-tmp-dir
    readinessProbe:
      exec:
        # -U postgres is required here: pg_isready's default-username
        # lookup depends on the exec'd process resolving the current uid
        # (1001) against /etc/passwd, which only happens via the Bitnami
        # entrypoint's own nss_wrapper setup (its container NOTES warn
        # about this exact gotcha for any raw exec) -- a k8s exec probe
        # runs the bare binary directly, bypassing that, so without an
        # explicit username pg_isready fails immediately ("no attempt",
        # exit 3) regardless of whether Postgres itself is actually up.
        command: ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]
      initialDelaySeconds: 5
      periodSeconds: 3
      failureThreshold: 30
  volumes:
  - name: pgdata
    persistentVolumeClaim:
      claimName: {name}-data
  - name: scratch
    emptyDir: {{}}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {K8S_NAMESPACE}
  labels:
    app: {TEMP_INSTANCE_LABEL}
spec:
  selector:
    instance: {name}
  ports:
  - port: 5432
    targetPort: 5432
"""


async def _run_new_instance_job(
    job: dict, db_id: str, filename: str, recovery_target: dict | None = None,
    tablespace_mapping: dict[str, str] | None = None,
) -> None:
    try:
        if not is_physical_restore_configured():
            raise BackupError(
                "Restore-to-a-new-instance isn't configured on this deployment "
                "(PHYSICAL_RESTORE_NAMESPACE/STATEFULSET/PVC env vars are unset)."
            )
        if recovery_target and not is_pitr_configured():
            raise BackupError(
                "Point-in-time recovery isn't configured on this deployment "
                "(PHYSICAL_RESTORE_WAL_ARCHIVE_PATH is unset)."
            )
        chain = _resolve_chain(db_id, filename)
        _log(job, "Restore chain (oldest first): " + " -> ".join(c["filename"] for c in chain))
        # Any node works for a brand-new PVC (nothing has pinned it yet) --
        # reusing the live server's own node is just the simplest choice.
        node = await _pod_node(job, K8S_POD_NAME)

        name = f"pg-ai-monitor-tmp-{uuid.uuid4().hex[:8]}"
        _log(job, f"=== Creating storage for temporary instance {name} ===")
        await _kubectl_apply(job, _temp_pvc_manifest(name))

        worker_name = f"{name}-worker"
        try:
            _log(job, f"=== Starting restore worker pod {worker_name} ===")
            await _kubectl_apply(job, _worker_pod_manifest(worker_name, node, pvc_name=f"{name}-data"))
            await _wait_pod_ready(job, worker_name)

            for i, entry in enumerate(chain):
                src = _entry_path(db_id, entry["filename"])
                remote_dir = f"/restore-work/{i}"
                dest = f"{K8S_NAMESPACE}/{worker_name}:{remote_dir}"
                await _mkdir_in_pod(job, worker_name, remote_dir)
                _log(job, f"Copying {entry['filename']} into the worker pod...")
                rc = await _run(job, ["kubectl", "cp", src, dest], os.environ, timeout=120)
                if rc != 0:
                    raise BackupError(f"Failed to copy {entry['filename']} into the restore worker.")

            if recovery_target:
                await _copy_wal_archive(job, worker_name)

            _log(job, "=== Extracting and assembling the data directory ===")
            await _exec_in_pod(job, worker_name, _build_restore_script(chain, recovery_target, tablespace_mapping))
        finally:
            _log(job, f"Removing restore worker pod {worker_name}...")
            await _run(job, _kubectl("delete", "pod", worker_name, "--wait=true", "--timeout=60s", "--ignore-not-found=true"), os.environ)

        _log(job, f"=== Starting temporary Postgres instance {name} ===")
        await _kubectl_apply(job, _temp_instance_manifest(name, node, filename, pitr=bool(recovery_target)))
        await _wait_pod_ready(job, name)

        host = f"{name}.{K8S_NAMESPACE}.svc.cluster.local"
        if recovery_target:
            _log(job, f"Temporary instance ready at {host}:5432, replaying archived WAL toward "
                       f"{recovery_target['type']}={recovery_target['value']}.")
            if recovery_target["action"] == "pause":
                _log(job, "It will PAUSE once it reaches that point (recovery_target_action=pause) instead of "
                           "auto-promoting -- connect and inspect, then use the Promote action when ready to "
                           "bring it fully online (or leave it paused/read-only indefinitely).")
            else:
                _log(job, "It will auto-promote once it reaches that point (or runs out of archived WAL, if that "
                           "comes first) -- check pg_is_in_recovery() there before trusting it's done replaying.")
        else:
            _log(job, f"Temporary instance ready at {host}:5432 -- same credentials as the original server (it's a byte copy).")
        _log(job, f"Delete it from the Temporary instances list when you're done with it -- it isn't cleaned up automatically.")
        job["instance_name"] = name
        job["instance_host"] = host
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()


async def promote_temp_instance(conn_info: ConnInfo, name: str) -> None:
    """Promotes a temp instance parked at recovery_target_action=pause --
    same credentials as the source server (it's a byte copy), just a
    different host/port, so conn_info is reused from whichever registered
    database the caller has at hand."""
    import asyncpg
    _safe_k8s_name(name)
    host = f"{name}.{K8S_NAMESPACE}.svc.cluster.local"
    target = ConnInfo(host=host, port=5432, dbname=conn_info.dbname, username=conn_info.username, password=conn_info.password)
    try:
        conn = await asyncpg.connect(dsn=target.dsn, timeout=8)
    except (asyncpg.PostgresError, OSError) as e:
        raise BackupError(f"Couldn't connect to {name} to promote it: {e}")
    try:
        await conn.execute("SELECT pg_promote()")
    except asyncpg.PostgresError as e:
        raise BackupError(f"pg_promote() failed: {e}")
    finally:
        await conn.close()


async def start_new_instance_restore(
    db_id: str, filename: str, recovery_target: dict | None = None, tablespace_mapping: dict[str, str] | None = None,
) -> str:
    recovery_target = _validate_recovery_target(recovery_target)
    tablespace_mapping = _validate_tablespace_mapping(tablespace_mapping)
    job = _new_job("new-instance-restore")
    asyncio.create_task(_run_new_instance_job(job, db_id, filename, recovery_target, tablespace_mapping))
    return job["id"]
