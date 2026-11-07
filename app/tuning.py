"""Hardware-based Postgres configuration advisor for the "Optimize database"
button on the Server Parameters panel.

This is deliberately a pure, deterministic calculation -- not an AI call
like the rest of app/ai.py -- because "how big should shared_buffers be
given 4 CPUs and 16GB RAM" already has a well-established, formula-based
answer (the same methodology used by the community's pgtune calculator),
and a deterministic answer is more trustworthy here than an LLM guessing
at numbers that a formula already gets right. It needs no database
connection and no ANTHROPIC_API_KEY, so it works even with AI disabled.

Callers only ever see recommend_settings(); the tables above it are the
per-workload/storage knobs the formulas are built from.
"""
from __future__ import annotations

DB_TYPES = ("web", "oltp", "dw", "desktop", "mixed")
STORAGE_TYPES = ("ssd", "hdd", "san")

# Fraction of total RAM assigned to shared_buffers, per workload. Desktop
# is much smaller since the machine also runs other things.
_SHARED_BUFFERS_FRACTION = {"web": 0.25, "oltp": 0.25, "dw": 0.25, "desktop": 0.0625, "mixed": 0.25}
# effective_cache_size is a *planner hint* (OS page cache + shared_buffers
# combined), not memory Postgres allocates -- so it's fine for it to be
# most of RAM.
_EFFECTIVE_CACHE_FRACTION = {"web": 0.75, "oltp": 0.75, "dw": 0.75, "desktop": 0.25, "mixed": 0.75}
# Fraction of RAM budgeted for work_mem in total, before dividing by
# max_connections and parallelism -- dw gets more since it runs fewer,
# bigger sorts/hashes rather than many small concurrent ones.
_WORK_MEM_FRACTION = {"web": 0.25, "oltp": 0.25, "dw": 0.5, "desktop": 0.0625, "mixed": 0.25}
_DEFAULT_MAX_CONNECTIONS = {"web": 200, "oltp": 300, "dw": 40, "desktop": 20, "mixed": 100}
_DEFAULT_STATS_TARGET = {"web": 100, "oltp": 100, "dw": 500, "desktop": 100, "mixed": 100}
_MAINT_WORK_MEM_CAP_MB = {"web": 2048, "oltp": 2048, "dw": 4096, "desktop": 2048, "mixed": 2048}
# (min_wal_size, max_wal_size) in MB.
_WAL_SIZE_MB = {"web": (1024, 4096), "oltp": (2048, 8192), "dw": (4096, 16384), "desktop": (100, 2048), "mixed": (1024, 4096)}

_RANDOM_PAGE_COST = {"ssd": 1.1, "hdd": 4.0, "san": 1.1}
_IO_CONCURRENCY = {"ssd": 200, "hdd": 2, "san": 300}

DB_TYPE_LABELS = {
    "web": "Web application", "oltp": "OLTP / transactional", "dw": "Data warehousing / analytics",
    "desktop": "Desktop / dev machine", "mixed": "Mixed workload",
}
STORAGE_LABELS = {"ssd": "SSD / NVMe", "hdd": "Spinning HDD", "san": "SAN / network storage"}


def _mb_str(mb: int) -> str:
    return f"{mb}MB"


def recommend_settings(
    cpu_count: int, ram_mb: int, db_type: str = "web", storage: str = "ssd",
    max_connections: int | None = None,
) -> list[dict]:
    if cpu_count < 1:
        raise ValueError("cpu_count must be at least 1")
    if ram_mb < 256:
        raise ValueError("ram_mb must be at least 256")
    if db_type not in DB_TYPES:
        db_type = "mixed"
    if storage not in STORAGE_TYPES:
        storage = "ssd"

    max_conn = max_connections if max_connections and max_connections > 0 else _DEFAULT_MAX_CONNECTIONS[db_type]

    shared_buffers_mb = max(16, round(ram_mb * _SHARED_BUFFERS_FRACTION[db_type]))
    effective_cache_mb = max(16, round(ram_mb * _EFFECTIVE_CACHE_FRACTION[db_type]))
    maintenance_work_mem_mb = max(16, min(round(ram_mb / 16), _MAINT_WORK_MEM_CAP_MB[db_type]))

    # Desktop workloads get no default parallelism (the machine is doing
    # other things too); everyone else gets up to 4 workers per gather,
    # capped by how many CPUs are actually available.
    parallel_workers_per_gather = 0 if db_type == "desktop" else max(1, min(4, cpu_count // 2))
    divisor = max(1, parallel_workers_per_gather)
    work_mem_kb = max(64, round((ram_mb * _WORK_MEM_FRACTION[db_type] * 1024) / max_conn / divisor))

    wal_buffers_mb = min(16, max(1, round(shared_buffers_mb * 0.03)))
    min_wal_mb, max_wal_mb = _WAL_SIZE_MB[db_type]
    max_parallel_maint_workers = max(1, min(4, cpu_count // 2))

    def work_mem_str() -> str:
        return f"{work_mem_kb}kB" if work_mem_kb < 1024 else f"{round(work_mem_kb / 1024)}MB"

    return [
        {"name": "max_connections", "value": str(max_conn),
         "note": f"Concurrent-connection ceiling assumed for a {DB_TYPE_LABELS[db_type].lower()} workload."},
        {"name": "shared_buffers", "value": _mb_str(shared_buffers_mb),
         "note": f"{int(_SHARED_BUFFERS_FRACTION[db_type] * 100)}% of RAM -- Postgres's own shared page cache."},
        {"name": "effective_cache_size", "value": _mb_str(effective_cache_mb),
         "note": "A planner hint (Postgres cache + OS page cache combined), not memory actually reserved."},
        {"name": "maintenance_work_mem", "value": _mb_str(maintenance_work_mem_mb),
         "note": "Used by VACUUM, CREATE INDEX, and ALTER TABLE ADD FOREIGN KEY."},
        {"name": "work_mem", "value": work_mem_str(),
         "note": "Per sort/hash operation, per connection -- multiplies fast under concurrent load, hence the division by max_connections."},
        {"name": "wal_buffers", "value": _mb_str(wal_buffers_mb),
         "note": "~3% of shared_buffers, capped at 16MB -- rarely worth tuning further."},
        {"name": "checkpoint_completion_target", "value": "0.9",
         "note": "Spreads checkpoint I/O across most of the checkpoint interval instead of bursting it."},
        {"name": "default_statistics_target", "value": str(_DEFAULT_STATS_TARGET[db_type]),
         "note": "Higher improves planner estimates on large/skewed tables at the cost of slower ANALYZE."},
        {"name": "random_page_cost", "value": str(_RANDOM_PAGE_COST[storage]),
         "note": f"{STORAGE_LABELS[storage]} seek cost relative to a sequential page read (1.0)."},
        {"name": "effective_io_concurrency", "value": str(_IO_CONCURRENCY[storage]),
         "note": f"How many concurrent I/O requests {STORAGE_LABELS[storage].lower()} can usefully serve at once."},
        {"name": "min_wal_size", "value": _mb_str(min_wal_mb), "note": "Floor for WAL disk usage between checkpoints."},
        {"name": "max_wal_size", "value": _mb_str(max_wal_mb),
         "note": "Ceiling before a checkpoint is forced early -- bigger means fewer, cheaper checkpoints but more crash-recovery replay."},
        {"name": "max_worker_processes", "value": str(max(cpu_count, 8)),
         "note": "Total background worker slots (parallel queries, logical replication, extensions all share this pool)."},
        {"name": "max_parallel_workers", "value": str(cpu_count),
         "note": "Ceiling on workers usable by parallel queries at once, drawn from max_worker_processes."},
        {"name": "max_parallel_workers_per_gather", "value": str(parallel_workers_per_gather),
         "note": "Workers a single query can use -- 0 disables parallel query execution."},
        {"name": "max_parallel_maintenance_workers", "value": str(max_parallel_maint_workers),
         "note": "Workers usable by a single CREATE INDEX / VACUUM."},
    ]
