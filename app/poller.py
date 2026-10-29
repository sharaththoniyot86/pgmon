"""Background task: periodically snapshot metrics (for every monitored
database -- the default one plus any registered via the Databases tab)
into each database's own history, and prune old rows. Also runs the same
rule-based checks as the Alerts tab (app/alerts.py) on every cycle and
caches the result here -- so a database's alerts are captured on a
regular schedule regardless of whether anyone has that tab open, and the
Databases list screen can show a per-database alert count without paying
for a live check on every card render."""
import asyncio
import logging
import time

from app.config import settings
from app import alerts, db, dbstore, metrics, notify

logger = logging.getLogger("pgmon.poller")

_task: asyncio.Task | None = None

# db_id -> {"alerts": [...], "checked_at": epoch-seconds}. In-memory only --
# same lifetime/durability tradeoff as app.db's connection pools; a restart
# just means the next poll cycle (within POLL_INTERVAL_SECONDS) repopulates
# it, same as everything else the poller maintains.
_alert_cache: dict[str, dict] = {}


def get_cached_alerts(db_id: str) -> list[dict] | None:
    """None means "never checked yet" (e.g. just registered, first poll
    hasn't run) -- distinct from an empty list, which means checked and
    genuinely clean."""
    entry = _alert_cache.get(db_id)
    return entry["alerts"] if entry else None


def drop_cached_alerts(db_id: str) -> None:
    _alert_cache.pop(db_id, None)
    # Also clear what's been notified about it: ids are reused as databases
    # are removed and re-added, and inheriting the previous occupant's
    # "already told you about this" record would silently suppress the
    # first alerts of the new one.
    notify.forget_database(db_id)


def get_cached_alert_summary(db_id: str) -> dict:
    entry = _alert_cache.get(db_id)
    if entry is None:
        return {"error_count": 0, "warning_count": 0, "info_count": 0, "checked_at": None}
    counts = {"error_count": 0, "warning_count": 0, "info_count": 0}
    for a in entry["alerts"]:
        counts[f"{a['severity']}_count"] += 1
    counts["checked_at"] = entry["checked_at"]
    return counts


async def _poll_one(db_id: str) -> None:
    pool = await db.get_pool(db_id)
    try:
        alert_list = await alerts.get_alerts(pool, db_id)
        _alert_cache[db_id] = {"alerts": alert_list, "checked_at": time.time()}
        # Delivery is attached to the poller rather than the Alerts tab on
        # purpose: the tab only evaluates when someone opens it, and an
        # alert that only fires when a human was already looking is not
        # worth sending. notify itself no-ops unless a webhook is
        # configured, and swallows its own delivery failures.
        try:
            entry = await dbstore.get_database(db_id)
            label = entry.name if entry else db_id
            await notify.send_alerts(db_id, label, alert_list)
        except Exception:
            logger.exception("alert delivery failed for db_id=%s", db_id)
    except Exception:
        logger.exception("alert check failed for db_id=%s", db_id)
    if db.is_read_only(db_id):
        return  # a standby can't have its own pgmon snapshot history -- see db._make_pool
    snap = await metrics.build_snapshot(pool)
    await metrics.save_snapshot(pool, snap)
    # Separate from the snapshot blob: per-query counters go to their own
    # table for regression detection. Failing here must not cost us the
    # snapshot we just stored, hence its own try.
    try:
        await metrics.save_query_stats(pool)
    except Exception:
        logger.exception("query stats sampling failed for db_id=%s", db_id)


# Bounds how long one slow/unreachable database can hold up a poll cycle --
# without this, _poll_one awaited in a plain `for` loop means one bad
# target (network partition, overloaded server, a newly-registered pool
# stuck connecting) delays metrics freshness for every *other* database
# too, which defeats the point of fleet monitoring as the fleet grows.
_POLL_ONE_TIMEOUT_SECONDS = 25


async def _poll_one_bounded(db_id: str) -> None:
    try:
        await asyncio.wait_for(_poll_one(db_id), timeout=_POLL_ONE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("metrics poll timed out after %ss for db_id=%s", _POLL_ONE_TIMEOUT_SECONDS, db_id)
    except Exception:
        logger.exception("metrics poll failed for db_id=%s", db_id)


async def _prune_one(db_id: str) -> None:
    if db.is_read_only(db_id):
        return
    try:
        pool = await db.get_pool(db_id)
        await metrics.prune_history(pool, settings.SNAPSHOT_RETENTION_HOURS)
    except Exception:
        logger.exception("history prune failed for db_id=%s", db_id)


async def _loop() -> None:
    prune_counter = 0
    while True:
        try:
            db_ids = [row["id"] for row in await dbstore.list_databases()]
            await asyncio.gather(*(_poll_one_bounded(db_id) for db_id in db_ids))

            prune_counter += 1
            if prune_counter >= 20:  # roughly every ~10 min at default 30s interval
                await asyncio.gather(*(_prune_one(db_id) for db_id in db_ids))
                prune_counter = 0
        except Exception:
            logger.exception("metrics poll iteration failed")
        await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)


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
