"""Full schema-and-data Oracle->Postgres conversion, via ora2pg.

This is deliberately a separate module from migration.py rather than a mode
bolted onto it: migration.py's hand-rolled copier only ever moves column
data for tables you already picked, one at a time, and explicitly does not
attempt DDL. This module instead shells out to `ora2pg` (see the Dockerfile
for how it and Oracle Instant Client get installed) to convert everything
in a schema -- tables, views, sequences, indexes, constraints, triggers,
functions/procedures -- and then load the data, table-by-table, straight
into the destination database this app is already managing.

Design, mirroring migration.py where it still applies:
  - The *destination* is always the database the Migration tab is open on.
  - The *source* (Oracle) connection is ad hoc: entered in the tab, used
    only for the duration of the job, never persisted to disk.
  - Jobs run as in-memory asyncio background tasks tracked in `_jobs`,
    same lightweight pattern as migration.py -- no external queue/worker.
  - Unlike migration.py, this necessarily runs real subprocesses (ora2pg
    itself is Perl, and shells out to psql for applying DDL), so progress
    is tracked as a phase name plus a capped tail of the subprocess output
    rather than per-table row counts.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid

import asyncpg

from app.db import ConnInfo

# Object types ora2pg can pull out of an Oracle schema, as a single -t/TYPE
# run (indexes, foreign keys, and check constraints are NOT separate types
# -- ora2pg bundles those into each table's own TABLE output automatically).
# COPY (actual data) is always a separate, final pass -- see _run_job.
SCHEMA_EXPORT_TYPES = "TABLE,VIEW,SEQUENCE,TRIGGER,FUNCTION,PROCEDURE,PACKAGE,PARTITION"

MAX_LOG_LINES = 800


class Ora2pgError(Exception):
    pass


def is_available() -> dict:
    """Whether ora2pg and the Oracle client it needs are actually installed.

    Cheap, synchronous, no network -- just checks the image was built with
    the Dockerfile's ora2pg/Instant Client block, so the UI can grey out
    the full-schema-migration option with a clear reason instead of only
    failing once a job is started.
    """
    ora2pg_path = shutil.which("ora2pg")
    oracle_home = os.environ.get("ORACLE_HOME", "")
    client_lib = os.path.join(oracle_home, "libclntsh.so") if oracle_home else ""
    if not ora2pg_path:
        return {"ok": False, "reason": "ora2pg is not installed in this image."}
    if not oracle_home or not os.path.exists(client_lib):
        return {"ok": False, "reason": "Oracle Instant Client is not installed in this image."}
    return {"ok": True}


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


def _new_job(owner: str, dest_schema: str) -> dict:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "running",
        "phase": "starting",
        "cancel_requested": False,
        "started_at": time.time(),
        "finished_at": None,
        "source_label": owner,
        "dest_label": dest_schema,
        "error": None,
        "log": [],
    }
    _jobs[job_id] = job
    return job


def _log(job: dict, text: str) -> None:
    for line in text.splitlines():
        if not line.strip():
            continue
        job["log"].append(line)
    if len(job["log"]) > MAX_LOG_LINES:
        job["log"] = job["log"][-MAX_LOG_LINES:]


def _oracle_dsn(host: str, port: int, service_name: str) -> str:
    return f"dbi:Oracle:host={host};service_name={service_name};port={port}"


async def _check_for_collisions(
    host: str, port: int, service_name: str, username: str, password: str, owner: str,
    dest_conn: ConnInfo, dest_schema: str,
) -> None:
    """Fails fast, in seconds, if the destination schema already has a
    table by any of the names Oracle is about to export -- rather than
    only finding out after the full ora2pg schema+data export (which can
    take several minutes on a real schema) dies partway through on
    "relation already exists" / a later data-load error against a
    same-named table that was never actually ora2pg's (confirmed this
    exact sequence on a real run: a destination schema with an unrelated
    pre-existing "countries" table let schema apply silently skip
    recreating it, then the data load failed with "column region_id does
    not exist" since the pre-existing table's columns didn't match).
    `CREATE TABLE` deliberately never overwrites an existing table (doing
    so automatically would risk real data loss), so this can't be fixed
    silently -- the job fails here instead, with the exact names involved,
    while that's still cheap to redo with a different destination schema.
    """
    from app.migration import list_oracle_tables  # local import: avoids a
    # module-level cycle, since migration.py has no reason to import this
    # module back.

    oracle_tables = await list_oracle_tables(host, port, service_name, username, password, owner)
    oracle_names = {t["table_name"].lower() for t in oracle_tables}

    conn = await asyncpg.connect(dsn=dest_conn.dsn, timeout=8)
    try:
        existing = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = $1", dest_schema
        )
    finally:
        await conn.close()
    existing_names = {r["tablename"].lower() for r in existing}

    collisions = sorted(oracle_names & existing_names)
    if collisions:
        raise Ora2pgError(
            f"Destination schema {dest_schema!r} already has {len(collisions)} table(s) with the same "
            f"name as tables being migrated from Oracle, and a pre-existing table is never overwritten "
            f"(to avoid risking its data): {', '.join(collisions)}. Use an empty destination schema, or "
            "rename/drop the conflicting table(s) first."
        )


async def _dest_pg_major_version(dest_conn: ConnInfo) -> str:
    # ora2pg picks its output syntax (identity columns vs SERIAL, native
    # partitioning, etc) off PG_VERSION, and otherwise warns and falls back
    # to a legacy default (11) -- so tell it what it's actually talking to.
    try:
        conn = await asyncpg.connect(dsn=dest_conn.dsn, timeout=8)
        try:
            version_num = await conn.fetchval("SHOW server_version_num")
            return str(int(version_num) // 10000)
        finally:
            await conn.close()
    except Exception:
        return "15"


def _write_config(
    work_dir: str, host: str, port: int, service_name: str, username: str, password: str, owner: str,
    dest_conn: ConnInfo, dest_schema: str, export_types: str, pg_version: str,
) -> str:
    # ora2pg.conf is a flat KEY value file; values are used as-is (no shell
    # interpolation happens when ora2pg reads it), but it does land on disk
    # with real credentials in it, so it's written per-job under a private
    # temp dir and only for the job's lifetime -- never under the app's
    # source tree, and cleaned up by the caller once the job finishes.
    conf_path = os.path.join(work_dir, "ora2pg.conf")
    lines = [
        f"ORACLE_DSN\tdbi:Oracle:host={host};service_name={service_name};port={port}",
        f"ORACLE_USER\t{username}",
        f"ORACLE_PWD\t{password}",
        f"SCHEMA\t{owner.upper()}",
        f"EXPORT_SCHEMA\t1",
        f"CREATE_SCHEMA\t0",
        f"PG_SCHEMA\t{dest_schema}",
        f"PG_VERSION\t{pg_version}",
        # The source account is typically the schema's own owner, not a
        # DBA -- ora2pg otherwise warns and falls back to a slower,
        # DBA-only way of gathering object grants.
        f"USER_GRANTS\t1",
        f"TYPE\t{export_types}",
        f"OUTPUT\tschema.sql",
        # Direct-to-Postgres mode for the data pass (TYPE COPY, run
        # separately -- see _run_job): with these set, ora2pg connects to
        # the destination itself and streams rows via COPY instead of only
        # producing a .sql file.
        f"PG_DSN\tdbi:Pg:dbname={dest_conn.dbname};host={dest_conn.host};port={dest_conn.port}",
        f"PG_USER\t{dest_conn.username}",
        f"PG_PWD\t{dest_conn.password}",
        # ora2pg loads tables in whatever order it scanned them (alphabetical
        # by table name here, not a dependency order), so a table whose FK
        # references another table loaded later fails immediately -- e.g.
        # COUNTRIES (has a FK to REGIONS) loads before REGIONS has any rows
        # at all, every row failing with "Key (region_id)=(...) is not
        # present in table regions" -- confirmed on a real HR-schema export,
        # not guessed. ora2pg's own docs name two fixes for this: DEFER_FKEY
        # (works only when NOT using direct-to-Postgres import, i.e. PG_DSN
        # unset -- this job always sets PG_DSN above, so that one's not an
        # option) or DROP_FKEY, which supports direct import: drops every FK
        # before the data pass and recreates them all once it's done.
        f"DROP_FKEY\t1",
        # User-defined triggers can carry real business logic that has no
        # business running against a one-time bulk historical-data load --
        # confirmed on a real HR-schema export, where Oracle's canonical
        # sample HR schema ships a trigger (secure_employees, calling
        # secure_dml()) that deliberately blocks all DML outside business
        # hours as a teaching example, which ora2pg faithfully converts
        # and which then blocks the data load itself outside that window.
        # USER (not ALL) disables only user-defined triggers, not RI/FK
        # enforcement ones -- doesn't need superuser, and DROP_FKEY above
        # already handles FK ordering directly rather than relying on
        # disabling those triggers.
        f"DISABLE_TRIGGERS\tUSER",
    ]
    with open(conf_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(conf_path, 0o600)
    return conf_path


async def _run_subprocess(job: dict, args: list[str], cwd: str, env: dict | None = None) -> int:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    job["_proc"] = proc
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            _log(job, line.decode(errors="replace"))
            if job["cancel_requested"]:
                proc.terminate()
                break
        return await proc.wait()
    finally:
        job["_proc"] = None


# Apply order for the per-type files ora2pg writes when TYPE names more
# than one type (each type gets its own "<TYPE>_schema.sql", not one
# combined file): tables first since everything else can reference them.
# AUTOINCREMENT is deliberately excluded here -- see the comment where it's
# applied below.
SCHEMA_APPLY_ORDER = ["TABLE", "SEQUENCE", "VIEW", "TRIGGER", "FUNCTION", "PROCEDURE", "PACKAGE", "PARTITION"]


async def _run_psql_file(dest_conn: ConnInfo, path: str, job: dict, label: str) -> int:
    _log(job, f"=== psql: applying {label} to {dest_conn.dbname} ===")
    env = dict(os.environ, PGPASSWORD=dest_conn.password)
    return await _run_subprocess(
        job,
        ["psql", "-h", dest_conn.host, "-p", str(dest_conn.port), "-U", dest_conn.username,
         "-d", dest_conn.dbname, "-v", "ON_ERROR_STOP=0", "-f", path],
        cwd=os.path.dirname(path), env=env,
    )


async def _run_job(
    job: dict, host: str, port: int, service_name: str, username: str, password: str, owner: str,
    dest_conn: ConnInfo, dest_schema: str, migrate_data: bool,
) -> None:
    work_dir = tempfile.mkdtemp(prefix="ora2pg-job-")
    try:
        job["phase"] = "checking for name collisions"
        await _check_for_collisions(host, port, service_name, username, password, owner, dest_conn, dest_schema)

        pg_version = await _dest_pg_major_version(dest_conn)
        conf_path = _write_config(
            work_dir, host, port, service_name, username, password, owner,
            dest_conn, dest_schema, SCHEMA_EXPORT_TYPES, pg_version,
        )

        # The destination schema has to exist before any unqualified
        # CREATE TABLE (via `SET search_path`) lands in it -- otherwise
        # Postgres silently falls through to the next schema on the path
        # (usually "public") instead of erroring, so this is done
        # directly rather than trusted to ora2pg's own CREATE_SCHEMA.
        job["phase"] = "creating destination schema"
        conn = await asyncpg.connect(dsn=dest_conn.dsn, timeout=8)
        try:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{dest_schema}"')
            # ora2pg's own config (BITMAP_AS_GIN, on by default -- see
            # ora2pg.conf's comment on that directive) converts Oracle
            # bitmap indexes to Postgres GIN indexes, which has no default
            # operator class for plain scalar types (int, bigint, ...) --
            # only for array/jsonb/tsvector-like types -- without this
            # extension providing one. ora2pg's own docs say outright "You
            # will need to create the extension by yourself"; without it,
            # schema apply fails on the first such index with "data type
            # bigint has no default operator class for access method
            # gin", which (since ora2pg's generated schema.sql runs as one
            # session) can cascade into later statements failing too --
            # confirmed from a real Oracle SH-schema export (COSTS/SALES,
            # both bitmap-indexed), not guessed.
            await conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")
            # Same situation as btree_gin above, different trigger:
            # Oracle Text/CONTEXT indexes convert into a Postgres text
            # search configuration whose mapping references the
            # "unaccent" dictionary -- which only exists once this
            # extension is created (it provides both the unaccent()
            # function and the dictionary itself), regardless of this
            # job's own USE_UNACCENT setting (confirmed: hit this on a
            # real export with that directive left at ora2pg's own
            # default of off). pg_trgm is ora2pg's documented companion
            # extension for the same accent-insensitive-search feature
            # (trigram indexes alongside the unaccent-wrapped text search
            # config) -- created alongside it pre-emptively rather than
            # waiting to hit that one as a separate failure on the next
            # schema that happens to use it.
            await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        finally:
            await conn.close()

        # --- phase 1: export schema DDL (tables, views, sequences, ...) ---
        job["phase"] = "exporting schema"
        _log(job, f"=== ora2pg: exporting schema for {owner.upper()} ===")
        rc = await _run_subprocess(job, ["ora2pg", "-c", conf_path, "-b", work_dir], cwd=work_dir)
        if job["cancel_requested"]:
            job["status"] = "cancelled"
            return
        if rc != 0:
            raise Ora2pgError("ora2pg failed to export the schema -- see log for details.")

        # --- phase 2: apply that DDL to the destination, one combined file ---
        # (ora2pg writes one file per TYPE when several are requested --
        # e.g. TABLE_schema.sql, VIEW_schema.sql -- rather than a single
        # schema.sql, so those are concatenated here in dependency order.)
        combined_path = os.path.join(work_dir, "_combined_schema.sql")
        found_any = False
        with open(combined_path, "w") as out:
            for type_name in SCHEMA_APPLY_ORDER:
                part_path = os.path.join(work_dir, f"{type_name}_schema.sql")
                if os.path.exists(part_path):
                    found_any = True
                    with open(part_path) as f:
                        out.write(f.read() + "\n")
        if not found_any:
            raise Ora2pgError("ora2pg produced no schema output files -- see log for details.")

        job["phase"] = "creating objects in destination"
        rc = await _run_psql_file(dest_conn, combined_path, job, "schema")
        if job["cancel_requested"]:
            job["status"] = "cancelled"
            return
        if rc != 0:
            # Not fatal on its own -- ora2pg's DDL commonly includes a few
            # objects psql can't create verbatim (e.g. PL/SQL bodies it
            # couldn't fully translate, marked FIXME in the SQL). Those
            # are surfaced in the log for a human to look at, but tables
            # that did apply cleanly are still worth loading data into.
            _log(job, "(schema apply reported errors -- see above; continuing to data load)")

        # --- phase 3: load data directly into the destination ---
        if migrate_data:
            job["phase"] = "copying data"
            _log(job, "=== ora2pg: copying data (TYPE COPY, direct to destination) ===")
            data_conf = conf_path + ".data"
            with open(conf_path) as f:
                conf_text = f.read()
            conf_text = conf_text.replace(f"TYPE\t{SCHEMA_EXPORT_TYPES}", "TYPE\tCOPY")
            with open(data_conf, "w") as f:
                f.write(conf_text)
            os.chmod(data_conf, 0o600)
            # --no_start_scn: by default ora2pg reads data "AS OF" the
            # current SCN (via SELECT CURRENT_SCN FROM v$database) for a
            # consistent snapshot -- but that view is DBA-only, and the
            # source account here is typically just the schema owner.
            rc = await _run_subprocess(
                job, ["ora2pg", "-c", data_conf, "-b", work_dir, "--no_start_scn"], cwd=work_dir
            )
            if job["cancel_requested"]:
                job["status"] = "cancelled"
                return
            if rc != 0:
                raise Ora2pgError("ora2pg failed during data load -- see log for details.")

            # --- phase 4: fix up identity/sequence current-values ---
            # ora2pg maps Oracle's auto-increment patterns to Postgres
            # identity columns starting at 1 -- AUTOINCREMENT_schema.sql
            # advances each one to max(id)+1 from the now-loaded data. It
            # must run after the data is in place (ora2pg's own output
            # says as much: "you must load the AUTOINCREMENT file after
            # data import").
            autoinc_path = os.path.join(work_dir, "AUTOINCREMENT_schema.sql")
            if os.path.exists(autoinc_path):
                job["phase"] = "syncing identity sequences"
                rc = await _run_psql_file(dest_conn, autoinc_path, job, "AUTOINCREMENT_schema.sql")
                if job["cancel_requested"]:
                    job["status"] = "cancelled"
                    return
                if rc != 0:
                    _log(job, "(identity sequence sync reported errors -- see above)")

        job["phase"] = "done"
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, f"ERROR: {e}")
    finally:
        job["finished_at"] = time.time()
        shutil.rmtree(work_dir, ignore_errors=True)


async def start_full_migration(
    host: str, port: int, service_name: str, username: str, password: str, owner: str,
    dest_conn: ConnInfo, dest_schema: str, migrate_data: bool = True,
) -> str:
    # Lowercased once, here, before it's used anywhere else: pgmon's own
    # CREATE SCHEMA IF NOT EXISTS below quotes dest_schema, which
    # preserves whatever case the user typed -- but ora2pg's own
    # generated SQL (PG_SCHEMA, its SET search_path statements, its
    # schema-qualified CREATE TABLE output) does not quote it, so
    # Postgres folds that copy to lowercase per standard unquoted-
    # identifier rules. A mixed-case or uppercase dest_schema used to
    # create a real mismatch: pgmon's own DDL landed in the exact-case
    # schema, while every byte ora2pg itself wrote (including all actual
    # data) landed in a *different*, lowercase-folded schema that pgmon
    # never created -- confirmed directly from a real run (typed "SHNEW",
    # wound up with schema apply and data both actually targeting
    # "shnew", including colliding with a previous run's leftover data
    # that happened to already be sitting in that same folded name).
    # Lowercasing up front makes both sides agree on the same name
    # unconditionally, matching the convention ora2pg's output already
    # follows regardless of what's fed into it.
    dest_schema = dest_schema.lower()
    job = _new_job(owner.upper(), dest_schema)
    asyncio.create_task(
        _run_job(job, host, port, service_name, username, password, owner, dest_conn, dest_schema, migrate_data)
    )
    return job["id"]
