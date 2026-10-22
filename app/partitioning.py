"""Background job wrapper around metrics.convert_table_to_partitioned --
same job-store pattern as app.backup/app.migration/app.ora2pg_migration
(each of those has its own private in-memory job dict too; this isn't
shared with them on purpose, it's a different, simpler kind of job).
"""
from __future__ import annotations

import asyncio
import time
import uuid

import asyncpg

from app import metrics

_jobs: dict[str, dict] = {}


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def _new_job() -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id, "kind": "partitioning", "status": "running",
        "started_at": time.time(), "finished_at": None, "error": None, "log": [], "result": None,
    }
    _jobs[job_id] = job
    return job


def _log(job: dict, text: str) -> None:
    job["log"].append(text)
    if len(job["log"]) > 500:
        job["log"] = job["log"][-500:]


async def _run_job(job: dict, pool: asyncpg.Pool, schema: str, table: str, technique: str, column: str) -> None:
    try:
        check = await metrics.check_partitioning_prerequisites(pool, schema, table, column)
        if check["blocking"]:
            raise metrics.PartitioningError(
                f"Refusing to convert {schema}.{table} automatically:\n- " + "\n- ".join(check["blocking"])
            )
        for notice in check["notices"]:
            _log(job, notice)
        result = await metrics.convert_table_to_partitioned(
            pool, schema, table, technique, column, log=lambda text: _log(job, text),
        )
        job["result"] = result
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()


async def start_conversion(pool: asyncpg.Pool, schema: str, table: str, technique: str, column: str) -> str:
    job = _new_job()
    asyncio.create_task(_run_job(job, pool, schema, table, technique, column))
    return job["id"]
