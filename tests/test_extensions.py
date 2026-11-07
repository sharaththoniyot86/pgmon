"""Tests for the Extensions tab's catalog, package mapping and DDL guards.

Run with:  python tests/test_extensions.py

Optionally set PGMON_TEST_EXT_DSN to a disposable PostgreSQL database
(superuser) to also run the live checks: every extension the server offers
must be listed, enable/disable round-trips, preload refusals.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import extension_catalog as cat  # noqa: E402
from app import metrics  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}{'' if cond else ' -- ' + str(detail)}")
    if not cond:
        FAILS.append(name)


def test_index_coverage():
    print("\nPACKAGE INDEX COVERAGE")
    idx = cat.index()
    check("index loaded with hundreds of extensions", len(idx) >= 250, len(idx))
    contrib = ["pg_walinspect", "pg_freespacemap", "pg_surgery", "pgrowlocks", "bloom", "btree_gin",
               "btree_gist", "fuzzystrmatch", "isn", "lo", "seg", "tcn", "tsm_system_rows", "tsm_system_time",
               "xml2", "moddatetime", "autoinc", "insert_username", "refint", "intagg", "dict_int",
               "dict_xsyn", "amcheck", "pageinspect", "pg_visibility", "pg_buffercache", "pg_prewarm",
               "pg_stat_statements", "pgstattuple", "postgres_fdw", "dblink", "file_fdw", "plpgsql",
               "plperl", "plpython3u", "pltcl"]
    for name in contrib:
        check(f"{name} is in the index", name in idx)
        check(f"{name} is contrib", idx.get(name, {}).get("contrib") is True)
    third = ["pg_cron", "vector", "postgis", "pg_repack", "hypopg", "pgaudit", "pg_partman", "orafce",
             "pg_hint_plan", "pg_qualstats", "pg_stat_kcache", "pg_wait_sampling", "pglogical", "timescaledb",
             "pgtap", "plpgsql_check", "pg_squeeze", "set_user", "rum", "hll", "age", "pgrouting", "http"]
    for name in third:
        check(f"{name} is in the index", name in idx)
        check(f"{name} is not contrib", idx.get(name, {}).get("contrib") is False)
    check("babelfish-only extensions are excluded", not any(n.startswith("babelfishpg") for n in idx))
    check("compat shims are excluded", not any("--" in n for n in idx))


def test_packages():
    print("\nPACKAGE NAMES PER PLATFORM")
    pk = cat.packages_for("pg_cron", 17)
    check("pg_cron deb", pk["deb"] == "postgresql-17-cron", pk)
    check("pg_cron el9", pk["el9"] == "pg_cron_17", pk)
    pk = cat.packages_for("vector", 16)
    check("pgvector deb", pk["deb"] == "postgresql-16-pgvector", pk)
    check("pgvector el9", pk["el9"] == "pgvector_16", pk)
    pk = cat.packages_for("pg_walinspect", 15)
    check("pg_walinspect deb is the server package", pk["deb"] == "postgresql-15", pk)
    check("pg_walinspect el9 is contrib", pk["el9"] == "postgresql15-contrib", pk)
    pk = cat.packages_for("pg_walinspect", 14)
    check("pg_walinspect absent before PG15", pk["deb"] is None and pk["el9"] is None, pk)
    pk = cat.packages_for("plpython3u", 17)
    check("plpython3u deb", pk["deb"] == "postgresql-plpython3-17", pk)
    check("plpython3u el9", pk["el9"] == "postgresql17-plpython3", pk)
    pk = cat.packages_for("postgis", 17)
    check("postgis deb", (pk["deb"] or "").startswith("postgresql-17-postgis-3"), pk)
    check("postgis el9 is the newest postgisNN", (pk["el9"] or "").startswith("postgis3") and pk["el9"].endswith("_17"), pk)
    pk = cat.packages_for("wal2json", 17)
    check("wal2json module deb", pk["deb"] == "postgresql-17-wal2json", pk)
    check("auto_explain module is contrib", cat.packages_for("auto_explain", 16)["deb"] == "postgresql-16")


def test_platform_detection():
    print("\nPLATFORM DETECTION FROM version()")
    cases = [
        ("PostgreSQL 17.11 (Debian 17.11-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit",
         "debian", "Debian 13"),
        ("PostgreSQL 16.4 (Debian 16.4-1.pgdg120+1) on x86_64-pc-linux-gnu, compiled by gcc (Debian 12.2.0-14) 12.2.0, 64-bit",
         "debian", "Debian 12"),
        ("PostgreSQL 16.4 (Ubuntu 16.4-1.pgdg22.04+1) on x86_64-pc-linux-gnu, compiled by gcc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0, 64-bit",
         "debian", "Ubuntu 22.04"),
        ("PostgreSQL 16.4 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 11.4.1 20231218 (Red Hat 11.4.1-3), 64-bit",
         "rhel", "EL9"),
        ("PostgreSQL 15.8 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 8.5.0 20210514 (Red Hat 8.5.0-22), 64-bit",
         "rhel", "EL8"),
        ("PostgreSQL 17.2 on x86_64-pc-linux-musl, compiled by gcc (Alpine 14.2.0) 14.2.0, 64-bit", "alpine", "Alpine"),
        ("PostgreSQL 18.0 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 12.3.0, 64-bit", "unknown", "unknown"),
    ]
    for v, fam, distro in cases:
        p = cat.detect_platform(v)
        check(f"{distro} detected", p["family"] == fam and p["distro"] == distro, p)


def test_install_instructions():
    print("\nINSTALL INSTRUCTIONS")
    deb = cat.detect_platform("PostgreSQL 17.11 (Debian 17.11-1.pgdg13+2) on x86_64-pc-linux-gnu")
    help_ = cat.install_instructions("pg_cron", 17, deb)
    check("Debian target lists apt first", help_["options"][0]["platform"] == "debian", help_)
    check("apt command names the package", "postgresql-17-cron" in help_["options"][0]["commands"][-1], help_)
    rhel = cat.detect_platform("PostgreSQL 16.4 on x86_64-pc-linux-gnu, compiled by gcc (GCC) 8.5.0 (Red Hat 8.5.0-22), 64-bit")
    help_ = cat.install_instructions("postgis", 16, rhel)
    check("RHEL target lists dnf first", help_["options"][0]["platform"] == "rhel", help_)
    check("postgis on EL8 enables EPEL + powertools", "powertools" in help_["options"][0]["commands"][0], help_)
    help_ = cat.install_instructions("definitely_not_an_extension", 17, deb)
    check("unknown extension has no package", not help_["packaged"], help_)


def test_name_and_preload_guards():
    print("\nDDL NAME GUARDS")
    for good in ["uuid-ossp", "pg_walinspect", "address_standardizer_data_us", "pgx-bm25"]:
        try:
            metrics._validate_extension_name(good)
            check(f"accepts {good!r}", True)
        except metrics.UnsafeQueryError as e:
            check(f"accepts {good!r}", False, e)
    for bad in ['x"; DROP TABLE t; --', "", "a b", "-x", "a" * 80, "ext'", "a/b"]:
        try:
            metrics._validate_extension_name(bad)
            check(f"rejects {bad[:20]!r}", False, "accepted")
        except metrics.UnsafeQueryError:
            check(f"rejects {bad[:20]!r}", True)
    check("preload library for pg_partman is pg_partman_bgw", metrics._preload_library_for("pg_partman") == "pg_partman_bgw")
    check("preload library for auto_explain module", metrics._preload_library_for("auto_explain") == "auto_explain")
    try:
        metrics._preload_library_for("pg_trgm")
        check("refuses to preload a non-preload extension", False, "accepted")
    except metrics.UnsafeQueryError:
        check("refuses to preload a non-preload extension", True)
    check("guc list parsing", metrics._split_guc_list('pg_stat_statements, "pg_cron",auto_explain') ==
          ["pg_stat_statements", "pg_cron", "auto_explain"])
    check("empty guc list", metrics._split_guc_list("") == [])
    check("a quoted element keeps its commas (one bogus library, not two)",
          metrics._split_guc_list('"a,b"') == ["a,b"])
    check("doubled quotes inside a quoted element", metrics._split_guc_list('"a""b", c') == ['a"b', "c"])


def test_curated_consistency():
    print("\nCURATED METADATA")
    names = [e["name"] for e in cat.CURATED]
    check("no duplicate curated entries", len(names) == len(set(names)),
          sorted({n for n in names if names.count(n) > 1}))
    for e in cat.CURATED:
        if e["preload"] not in (None, "required", "recommended", "optional"):
            check(f"{e['name']} preload value", False, e["preload"])
        if e["preload"] and not e["library"]:
            check(f"{e['name']} has a library", False)
    required = {e["name"] for e in cat.CURATED if e["preload"] == "required"}
    for name in ["pg_stat_statements", "pg_cron", "pgaudit", "timescaledb", "pg_stat_kcache", "citus"]:
        check(f"{name} marked preload=required", name in required)


async def live_checks(dsn):
    """Needs a disposable superuser database. Exercises the one-click flow,
    including an enable that has to wait for a restart (PGMON_TEST_RESTART
    = a shell command that restarts that server, e.g. `podman restart x`)."""
    import asyncpg, subprocess, tempfile
    os.environ.setdefault("USERS_DB_PATH", os.path.join(tempfile.mkdtemp(), "users.db"))
    from app import dbstore
    from app.config import settings
    settings.USERS_DB_PATH = os.environ["USERS_DB_PATH"]
    await dbstore.init_db()
    print(f"\nLIVE CHECKS against {dsn.rsplit('@', 1)[-1]}")
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    db_id = "test1"
    try:
        state = await metrics.get_extension_catalog_status(pool, db_id)
        async with pool.acquire() as conn:
            available = {r["name"] for r in await conn.fetch("SELECT name FROM pg_available_extensions")}
        by_name = {x["name"]: x for x in state["extensions"]}
        import re as _re
        expected = {n for n in available if n not in cat.MODULES_BY_NAME
                    and not (_re.fullmatch(r"(.+)-\d+", n) and _re.fullmatch(r"(.+)-\d+", n).group(1) in available)}
        check("every extension on the server is listed", expected <= set(by_name), expected - set(by_name))
        check("pg_walinspect is offered when the server has it (or explained, for a non-superuser login)",
              "pg_walinspect" not in available or by_name["pg_walinspect"]["state"] in ("available", "enabled")
              or (by_name["pg_walinspect"]["state"] == "blocked" and not state["server"]["is_superuser"]))
        if not state["server"]["is_superuser"]:
            offered_untrusted = [x["name"] for x in state["extensions"] if x["state"] == "available"
                                 and x["kind"] == "extension" and "superuser" in (x.get("block_reason") or "")]
            check("non-superuser login: no superuser-only extension is offered", not offered_untrusted, offered_untrusted)
        if state["preload_on_command_line"]:
            stuck = [x["name"] for x in state["extensions"] if x["state"] == "available" and x["needs_restart"]]
            check("start command fixes shared_preload_libraries: nothing offered needs it changed", not stuck, stuck)
            blocked = [x for x in state["extensions"] if x["state"] == "blocked"]
            check("blocked items say what to add to the start option",
                  all("-c shared_preload_libraries=" in (x["block_reason"] or "") for x in blocked), blocked[:1])
        missing = [x for x in state["extensions"] if x["state"] == "missing" and x["kind"] == "extension"]
        check("not-installed extensions carry one install command",
              all(x["install_command"] for x in missing if cat.packages_for(x["name"], state["server"]["major"])["deb"]))

        if "earthdistance" in available and not state["server"]["is_superuser"]:
            try:
                await metrics.enable_extension(pool, "earthdistance", db_id)
                check("non-superuser: superuser-only extension refused with pgmon's explanation", False, "created")
            except metrics.UnsafeQueryError as e:
                check("non-superuser: superuser-only extension refused with pgmon's explanation", "Only a superuser" in str(e), e)
        if "earthdistance" in available and state["server"]["is_superuser"]:
            async with pool.acquire() as conn:
                await conn.execute("DROP EXTENSION IF EXISTS earthdistance")
                await conn.execute("DROP EXTENSION IF EXISTS cube")
            res = await metrics.enable_extension(pool, "earthdistance", db_id)
            check("Enable brings its dependency along", set(res["created"]) == {"cube", "earthdistance"}, res)
            try:
                await metrics.disable_extension(pool, "cube", db_id)
                check("won't disable something another extension needs", False, "dropped")
            except metrics.UnsafeQueryError:
                check("won't disable something another extension needs", True)
            await metrics.disable_extension(pool, "earthdistance", db_id)
            await metrics.disable_extension(pool, "cube", db_id)
        try:
            await metrics.enable_extension(pool, "definitely_not_an_extension", db_id)
            check("refuses what the server doesn't have", False, "created")
        except metrics.UnsafeQueryError as e:
            check("refuses what the server doesn't have", "isn't installed" in str(e), e)
        try:
            await metrics.disable_extension(pool, "plpgsql", db_id)
            check("won't remove plpgsql", False, "dropped")
        except metrics.UnsafeQueryError:
            check("won't remove plpgsql", True)

        rec = await metrics.enable_recommended(pool, db_id)
        check("recommended set: every entry gets an outcome", set(rec["results"]) == set(metrics.RECOMMENDED_EXTENSIONS), rec)
        check("recommended set: nothing failed", not any(v.startswith("failed") for v in rec["results"].values()), rec)

        restart = os.environ.get("PGMON_TEST_RESTART")
        if restart and "pg_wait_sampling" in available:
            res = await metrics.enable_extension(pool, "pg_wait_sampling", db_id)
            check("preload extension: Enable answers 'restart'", res["status"] == "restart", res)
            state = await metrics.get_extension_catalog_status(pool, db_id)
            w = {x["name"]: x for x in state["extensions"]}["pg_wait_sampling"]
            check("shown as waiting for restart", w["state"] == "restart", w)
            await pool.close()
            subprocess.run(restart, shell=True, check=True)
            for _ in range(30):
                try:
                    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
                    break
                except Exception:
                    await asyncio.sleep(1)
            state = await metrics.get_extension_catalog_status(pool, db_id)
            w = {x["name"]: x for x in state["extensions"]}["pg_wait_sampling"]
            check("after the restart pgmon finishes it by itself", w["state"] == "enabled" and "pg_wait_sampling" in state["finished"], w)
            check("nothing left pending", await dbstore.list_pending_extensions(db_id) == [])
    finally:
        await pool.close()


if __name__ == "__main__":
    test_index_coverage()
    test_packages()
    test_platform_detection()
    test_install_instructions()
    test_name_and_preload_guards()
    test_curated_consistency()
    dsn = os.environ.get("PGMON_TEST_EXT_DSN")
    if dsn:
        asyncio.run(live_checks(dsn))
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
