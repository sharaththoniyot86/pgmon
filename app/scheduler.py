"""Background task: runs recurring backup schedules (app/backup_schedule.py)
and prunes old backups per each schedule's retention policy. Same
start()/stop() shape as app/poller.py, registered separately in main.py's
lifespan.

Checks every 5 minutes -- coarse compared to the poller's 30s, since the
coarsest schedule granularity offered is hourly and being a few minutes
late to fire is harmless."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app import backup, backup_schedule, db, dbstore

logger = logging.getLogger("pgmon.scheduler")

_task: asyncio.Task | None = None
_CHECK_INTERVAL_SECONDS = 300
_WAL_PRUNE_SAFETY_BUFFER_SECONDS = 86400  # 1 day margin past the oldest kept backup


async def _prune_wal_archive_if_needed() -> None:
    """Runs every tick regardless of whether any schedule fired this round
    -- WAL retention needs to stay correct even for physical/incremental
    backups taken manually, outside any schedule."""
    db_ids = [row["id"] for row in await dbstore.list_databases()]
    oldest = None
    for db_id in db_ids:
        try:
            for b in await asyncio.to_thread(backup.list_backups, db_id):
                if b["type"] in ("physical", "incremental"):
                    if oldest is None or b["created_at"] < oldest:
                        oldest = b["created_at"]
        except backup.BackupError:
            continue
    if oldest is None:
        return  # no physical/incremental backup exists anywhere -- nothing depends on the archive yet
    cutoff = oldest - _WAL_PRUNE_SAFETY_BUFFER_SECONDS
    try:
        deleted = await backup.prune_wal_archive_before(cutoff)
        if deleted:
            logger.info("WAL archive retention: pruned %d file(s) older than the oldest kept backup", deleted)
    except Exception:
        logger.exception("WAL archive pruning failed")


def _is_due(record: backup_schedule.ScheduleRecord) -> bool:
    if not record.last_run_at:
        return True
    try:
        last_run = datetime.fromisoformat(record.last_run_at)
    except ValueError:
        return True
    frequency_hours = backup_schedule.FREQUENCIES_HOURS.get(record.frequency)
    if frequency_hours is None:
        return False
    elapsed = datetime.now(timezone.utc) - last_run
    return elapsed.total_seconds() >= frequency_hours * 3600


async def _run_schedule(record: backup_schedule.ScheduleRecord) -> None:
    try:
        conn_info = await db.get_conn_info(record.db_id)
    except KeyError:
        logger.warning("scheduled backup skipped: db_id=%s no longer exists", record.db_id)
        return

    try:
        if record.backup_type == "incremental":
            existing = backup.list_backups(record.db_id)
            base = next((b for b in existing if b["type"] in ("physical", "incremental")), None)
            if base is None:
                logger.info(
                    "schedule %s (db_id=%s) wants incremental but has no base yet -- taking a physical backup instead",
                    record.id, record.db_id,
                )
                job_id = await backup.start_backup(record.db_id, conn_info, backup_type="physical")
            else:
                job_id = await backup.start_backup(
                    record.db_id, conn_info, backup_type="incremental", incremental_base=base["filename"]
                )
        else:
            job_id = await backup.start_backup(record.db_id, conn_info, backup_type=record.backup_type)
    except backup.BackupError:
        logger.exception("failed to start scheduled backup for schedule=%s db_id=%s", record.id, record.db_id)
        await backup_schedule.record_run(record.id, "error")
        return

    # start_backup launches its own asyncio task and returns immediately;
    # wait here for it to actually finish before recording the run and
    # pruning, rather than firing retention against a still-in-progress
    # backup. Different schedules' _run_schedule calls run concurrently
    # (see _loop's gather), so this wait doesn't block other schedules.
    while True:
        job = backup.get_job(job_id)
        if job is None or job["status"] != "running":
            break
        await asyncio.sleep(5)

    status = job["status"] if job else "unknown"
    await backup_schedule.record_run(record.id, status)
    if status != "done":
        logger.warning("scheduled backup for schedule=%s db_id=%s ended with status=%s", record.id, record.db_id, status)
        return

    try:
        deleted = await asyncio.to_thread(backup.prune_old_backups, record.db_id, record.retention_days)
        if deleted:
            logger.info("retention: pruned %d old backup(s) for db_id=%s: %s", len(deleted), record.db_id, deleted)
    except backup.BackupError:
        logger.exception("retention prune failed for schedule=%s db_id=%s", record.id, record.db_id)


async def _loop() -> None:
    while True:
        try:
            records = await backup_schedule.list_enabled_records()
            due = [r for r in records if _is_due(r)]
            if due:
                await asyncio.gather(*(_run_schedule(r) for r in due), return_exceptions=True)
            await _prune_wal_archive_if_needed()
        except Exception:
            logger.exception("scheduler iteration failed")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
