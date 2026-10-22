"""A curated reference catalog of well-known PostgreSQL extensions, for the
Extensions tab -- browse what exists, see per-database availability/enabled
status (live, via pg_available_extensions / pg_extension), and enable one
with a click.

This is metadata only, not a binary repository: pgmon has no general way to
install a genuinely new compiled extension onto an arbitrary *registered*
database (that needs filesystem/container access to that specific server,
which varies per target). "Available" here always means the extension's
control file already exists on that target server; enabling it is then a
plain `CREATE EXTENSION`, safe to run against any registered database over
its normal SQL connection.

Every extension below marked "bundled": False (hypopg, pg_repack, pg_cron,
pg_partman, vector/pgvector) needs postgres-image/Dockerfile's custom image
-- stock bitnami/postgresql doesn't have their control files at all. A few
"bundled": True ones (pg_stat_statements, pgaudit) are in stock contrib but
still need shared_preload_libraries set at server *start*, which a later
CREATE EXTENSION can't do either. postgres-image/new-database-values.yaml
is the standard way to deploy a new instance with all of the above already
satisfied -- see its comments and README.md's "Deploying a new monitored
Postgres instance with full extension support" for the actual command.
"""
from __future__ import annotations

CATALOG = [
    # ---------------------------------------------------- monitoring / perf
    {
        "name": "pg_stat_statements", "category": "Monitoring & Performance",
        "description": "Tracks execution statistics (calls, timing, rows, I/O) for every "
                        "distinct query shape. Powers this app's own SQL Performance tab.",
        "bundled": True,
    },
    {
        "name": "pgstattuple", "category": "Monitoring & Performance",
        "description": "Exact per-table bloat and free-space statistics via a full table scan "
                        "-- more precise (and more expensive) than the dead-tuple-ratio estimate "
                        "this app's Administration tab shows by default.",
        "bundled": True,
    },
    {
        "name": "pg_buffercache", "category": "Monitoring & Performance",
        "description": "Inspect what's actually resident in shared_buffers right now. Powers "
                        "this app's Performance Hub buffer cache occupancy panel, if installed.",
        "bundled": True,
    },
    {
        "name": "pg_prewarm", "category": "Monitoring & Performance",
        "description": "Load a relation's data into shared_buffers (or the OS cache) on demand "
                        "-- useful right after a restart, before real traffic hits a cold cache.",
        "bundled": True,
    },
    {
        "name": "hypopg", "category": "Monitoring & Performance",
        "description": "Hypothetical indexes -- simulate an index and see the planner's cost "
                        "estimate without actually building it. Powers this app's Tuning Advisor "
                        "missing-index check (SQL Performance tab) when installed.",
        "bundled": False,
    },
    {
        "name": "pg_repack", "category": "Monitoring & Performance",
        "description": "Online table/index bloat removal without VACUUM FULL's long exclusive "
                        "lock. Powers this app's Repack button (Administration tab bloat table) "
                        "when installed.",
        "bundled": False,
    },
    {
        "name": "pageinspect", "category": "Monitoring & Performance",
        "description": "Low-level inspection of raw page contents -- mostly a debugging/learning "
                        "tool, rarely needed day to day.",
        "bundled": True,
    },
    {
        "name": "pg_visibility", "category": "Monitoring & Performance",
        "description": "Inspect a table's visibility map and page-level all-visible/all-frozen "
                        "flags -- useful when investigating unexpected VACUUM/autovacuum behavior.",
        "bundled": True,
    },
    {
        "name": "amcheck", "category": "Monitoring & Performance",
        "description": "Verifies B-tree indexes (and, since PG14, some heap invariants) for "
                        "corruption -- worth running after a suspicious crash or storage issue.",
        "bundled": True,
    },
    # -------------------------------------------------------------- search
    {
        "name": "pg_trgm", "category": "Search & Text",
        "description": "Trigram matching for fuzzy text search and similarity scoring (%, "
                        "similarity()) -- also enables fast ILIKE/regex search via a GIN/GiST index.",
        "bundled": True,
    },
    {
        "name": "unaccent", "category": "Search & Text",
        "description": "Strips accents/diacritics from text -- so a search for \"cafe\" also "
                        "matches \"café\".",
        "bundled": True,
    },
    {
        "name": "citext", "category": "Search & Text",
        "description": "A case-insensitive text type -- comparisons and unique constraints "
                        "ignore case automatically, without wrapping every query in lower().",
        "bundled": True,
    },
    {
        "name": "tablefunc", "category": "Search & Text",
        "description": "crosstab() and friends, for pivot-table-style queries directly in SQL.",
        "bundled": True,
    },
    # ------------------------------------------------------------ data types
    {
        "name": "uuid-ossp", "category": "Data Types",
        "description": "UUID-generation functions (uuid_generate_v4(), etc.) -- Postgres 13+ "
                        "also has the built-in gen_random_uuid() with no extension needed.",
        "bundled": True,
    },
    {
        "name": "hstore", "category": "Data Types",
        "description": "A schemaless key-value pair type stored in a single column -- predates "
                        "jsonb and is more compact for flat string-to-string maps.",
        "bundled": True,
    },
    {
        "name": "ltree", "category": "Data Types",
        "description": "A hierarchical \"label path\" type (e.g. 'top.science.astronomy') with "
                        "indexable ancestor/descendant queries -- a good fit for tree-shaped data "
                        "that doesn't need a full recursive-CTE adjacency list.",
        "bundled": True,
    },
    {
        "name": "intarray", "category": "Data Types",
        "description": "Extra operators and index support for one-dimensional integer arrays "
                        "(intersects, contains, etc.).",
        "bundled": True,
    },
    {
        "name": "cube", "category": "Data Types",
        "description": "A multi-dimensional cube data type -- mostly useful as a building block "
                        "(earthdistance depends on it) rather than directly.",
        "bundled": True,
    },
    {
        "name": "earthdistance", "category": "Data Types",
        "description": "Great-circle distance calculations between points on Earth's surface -- "
                        "simpler than PostGIS if that's literally all you need.",
        "bundled": True,
    },
    {
        "name": "vector", "category": "Data Types",
        "description": "pgvector -- a vector type with L2/cosine/inner-product distance operators "
                        "and approximate-nearest-neighbor indexes (IVFFlat, HNSW), for embeddings "
                        "and AI/ML similarity search.",
        "bundled": False,
    },
    {
        "name": "postgis", "category": "Data Types",
        "description": "Full geospatial data types, indexes, and functions (points, polygons, "
                        "projections, spatial joins) -- the standard for real GIS work in Postgres.",
        "bundled": False,
    },
    # -------------------------------------------------------------- security
    {
        "name": "pgcrypto", "category": "Security",
        "description": "Hashing (digest, crypt/gen_salt for password hashing), symmetric/PGP "
                        "encryption, and random-bytes functions, usable directly in SQL.",
        "bundled": True,
    },
    {
        "name": "pgaudit", "category": "Security",
        "description": "Detailed, configurable audit logging of SQL statements (DDL/role/write) "
                        "to the Postgres log -- separate from pgmon's own in-app audit trail, which "
                        "only covers actions taken through this app's UI, not raw SQL.",
        "bundled": True,
    },
    {
        "name": "sslinfo", "category": "Security",
        "description": "Functions to inspect the SSL/TLS certificate details of the current "
                        "connection.",
        "bundled": True,
    },
    # ------------------------------------------------------- federation/util
    {
        "name": "postgres_fdw", "category": "Federation & Utility",
        "description": "Query another Postgres server's tables as if they were local -- a "
                        "foreign data wrapper. Useful for cross-database joins without a full ETL.",
        "bundled": True,
    },
    {
        "name": "dblink", "category": "Federation & Utility",
        "description": "Ad hoc synchronous connections to other Postgres databases from within "
                        "SQL -- older and less capable than postgres_fdw, but simpler for one-off "
                        "cross-database calls.",
        "bundled": True,
    },
    {
        "name": "file_fdw", "category": "Federation & Utility",
        "description": "Read a flat file (CSV, etc.) on the server's filesystem as if it were a "
                        "regular table.",
        "bundled": True,
    },
    {
        "name": "pg_cron", "category": "Federation & Utility",
        "description": "An in-database cron-syntax job scheduler -- run SQL on a schedule "
                        "without an external cron job needing its own DB credentials.",
        "bundled": False,
    },
    {
        "name": "pg_partman", "category": "Federation & Utility",
        "description": "Automates creating and dropping time/ID-range partitions on a schedule "
                        "-- complements this app's manual partition create/detach tooling (Schema "
                        "tab) for tables that need ongoing, unattended partition maintenance.",
        "bundled": False,
    },
]

_BY_NAME = {e["name"]: e for e in CATALOG}


def get(name: str) -> dict | None:
    return _BY_NAME.get(name)
