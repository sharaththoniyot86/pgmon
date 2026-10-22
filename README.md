# pgmon — AI-Powered PostgreSQL Monitor

A self-hosted PostgreSQL monitoring, administration, and tuning dashboard.
Monitors only what you explicitly register via the Databases screen (no
built-in default database), with optional Claude-powered analysis layered
on top when configured.

**See [DOCUMENTATION.md](DOCUMENTATION.md) for the full application
documentation (every tab/feature, architecture, and how to deploy this to
your own cluster, including Rancher).** The rest of this file is a
lower-level reference for working on the app itself in this specific dev
environment (k3d).

Don't need Kubernetes? [packaging/linux/](packaging/linux/README.md) builds
a release tarball that installs pgmon as a systemd service behind Nginx on
any plain Debian/Ubuntu, RHEL/Fedora/Rocky/Alma, or openSUSE machine;
[packaging/rpm/](packaging/rpm/README.md) builds a native `.rpm`
specifically for RHEL / Oracle Linux / Rocky / AlmaLinux 9 (`dnf install`,
with dependency resolution and upgrade/removal tracking); and
[packaging/windows/](packaging/windows/README.md) builds an MSI that
installs it as a Windows service on a plain Windows machine.

## Navigation

The app is database-centric: **Databases** is the home screen — a list of
every registered Postgres instance/database as a card (name, host:port/db,
a live up/down status badge, current connection count, cache hit %, and
transactions/sec — all auto-refreshing every 15s; TPS is derived from the
two most recent poller snapshots, so it appears once a database has been
monitored for at least one poll interval). A **+ Add database** button at
the top right opens a modal with the same test-then-save flow as before.
Clicking a card drills into that database's own detail page, which has its
own tab strip:

1. **Details** — DB size, connection counts, cache hit ratio, uptime,
   commit/rollback rates, 6h trend charts, **✦ AI Health Summary**; below
   that, every server parameter from `pg_settings` (name, current value,
   compiled-in default, category), filterable by name with a "only show
   changed" toggle, sorted changed-first, with every row that's been
   changed from its default (via `postgresql.conf`, `ALTER SYSTEM`, the
   environment, or the session — read from Postgres's own `source` column,
   not a fragile text diff of the values) highlighted in amber. Every
   parameter that isn't compiled-in (`context <> 'internal'`) has an
   **Edit…** button opening a modal (a dropdown for booleans/enums, a
   number input with its unit shown for numeric GUCs, validated against
   `pg_settings.enumvals`/`min_val`/`max_val` first) that runs a real
   `ALTER SYSTEM SET` — name and value are passed through Postgres's own
   `format('%I = %L', ...)` rather than hand-quoted, so it's not
   injectable — followed by `pg_reload_conf()`, then polls briefly for the
   change to actually settle rather than trusting an immediate read (config
   reload is asynchronous signal delivery, so an immediate re-read can
   still show the old value even though the reload was requested
   correctly). Settings that need a full server restart (`context =
   'postmaster'`) are written to `postgresql.auto.conf` and clearly warn
   that pgmon does not restart the server automatically; a **Reset to
   default** button runs `ALTER SYSTEM RESET`.
2. **SQL Performance** — top queries via `pg_stat_statements` (with an
   enablement notice if it isn't turned on), **✦ AI Query Optimizer** (paste
   a `SELECT`/`WITH`, runs real `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`
   read-only/10s-timeout/single-statement-guarded, Claude explains the plan).
3. **Backup & Recovery** — read-only status: WAL level, archive mode,
   `pg_stat_archiver` counts, last checkpoint, replica count, **✦ AI Backup
   Readiness Review**. Info only — pgmon never takes or schedules backups.
4. **Security** — SSL enforcement, password encryption method, role
   privileges table, `pg_hba.conf` auth rules (flags `trust`), installed
   extensions, **✦ AI Security Review**.
5. **Connections** — live `pg_stat_activity` (auto-refreshes every 5s;
   excludes only pgmon's own currently-executing request, not every pgmon
   connection, so its own poller and other legitimate activity still show)
   + blocking-lock detection, with a **Kill session** button on *every*
   live connection (not just blocking ones), also right-click-able for a
   context menu with the same action, and on each blocking pid in
   the locks table (`pg_terminate_backend`, confirmed before firing;
   refuses to terminate pgmon's own connection).
6. **Performance Hub** — an Oracle Enterprise Manager-style page: a shared
   time-range control (Real-Time 15m / 1H / 6H / 24H / **Custom…**, the
   last opening a date-time picker for any specific window within the 48h
   retention) governs two live charts -- **I/O Load** (reads/hits/writes
   per sec from `pg_stat_io`,
   PG16+, with a graceful "not available" fallback + reason on older
   servers) and **Wait Events** (only sessions actually *running a query*
   count -- idle connections waiting for their next command are excluded,
   since that's network idle time, not contention, and including it
   drowns out anything worth seeing; shows both the coarse
   `wait_event_type` and the specific `wait_event`, e.g. `Lock: relation`,
   alongside the actual query and how long it's been waiting -- a stacked
   trend chart over time plus a live detail table), each backed by real
   per-interval deltas / point-in-time samples computed from poller
   history, with always-current stat cards and badges above them that
   auto-refresh every 3 minutes (charts too, for the relative presets --
   a fixed custom window doesn't auto-refresh once applied, since it's
   not "the last N hours" anymore). Below that,
   **Memory & Buffer Cache**: cache hit
   ratio, `shared_buffers`/`effective_cache_size`/`work_mem`/
   `maintenance_work_mem`/`wal_buffers` (converted to human-readable
   sizes), temp-file spill (a sign `work_mem` is too small for some
   query), and buffer cache occupancy via `pg_buffercache` if that
   extension happens to be installed. **✦ AI Performance Review**.
7. **Schema** — a Toad-style two-pane browser: a collapsible tree on the
   left (Schema → Tables/Views/Sequences → object, lazy-loaded per
   expand, with a filter box; system schemas hidden by default,
   toggleable) and a sub-tabbed detail pane on the right for whatever's
   selected -- **Columns** / **Indexes** (with real usage counts) /
   **Statistics** (live/dead rows, last vacuum/analyze) / **Data** (a
   paginated, sortable row grid -- 50/page, click a column header to
   sort, capped at 500 rows/page, read-only transaction with a 10s
   statement timeout; the sort column is validated against the table's
   real columns before use, never taken as raw input) for tables;
   **Columns** / **Definition** (`pg_get_viewdef`) / **Data** for views;
   **Properties** (current value, increment, min/max, cycle) for
   sequences.
8. **Administration** — autovacuum config + per-table autovacuum/autoanalyze
   stats + currently-running autovacuum workers; estimated table bloat
   (dead-tuple ratio -- an exact figure needs `pgstattuple` and a full
   table scan, which pgmon doesn't run automatically), with a **Vacuum…**
   button on every bloat candidate opening a real `VACUUM` -- ANALYZE
   (recommended, on by default), FREEZE, and FULL (opt-in, warned clearly:
   it takes an `ACCESS EXCLUSIVE` lock blocking all reads/writes on that
   table, and asks for confirmation before running), validated against
   `pg_catalog` first (refuses anything that isn't a real table), run
   outside a transaction block with `statement_timeout` lifted just for
   that call, reporting dead-tuple count and size before/after; disk usage
   (database size, WAL directory size, temp file usage) and growth rate
   (MB/day, from poller history); WAL generation rate (bytes/sec,
   server-wide -- WAL isn't per-database); checkpoint frequency
   (`pg_stat_checkpointer` on PG17+, falls back to `pg_stat_bgwriter` on
   older servers) vs. `checkpoint_timeout`/`max_wal_size`; index usage
   (seq vs. index scan ratio, top unused indexes); **✦ AI Administration
   Review**.
9. **Migration** — copies tables into **this database** from Oracle or
   another PostgreSQL instance. The source connection (host/port/service
   or dbname/username/password) is entered ad hoc in the tab, tested live,
   and used only for that migration run — unlike registered databases,
   it's never persisted to disk. After a successful test, pick tables from
   the live list (with an approximate row count) and run: each selected
   table streams in batches straight into the destination via `COPY`
   (asyncpg's `copy_records_to_table`) rather than row-by-row `INSERT`s, so
   it's reasonably fast at real data volumes, with a destination schema
   the tables land in, an optional `CREATE TABLE IF NOT EXISTS` (Oracle
   column types mapped to their closest Postgres equivalent — `NUMBER`
   with a zero scale to `smallint`/`integer`/`bigint`/`numeric` depending
   on precision, `VARCHAR2`/`CLOB` to `text`, `DATE`/`TIMESTAMP` to
   `timestamp`(`tz`), `BLOB`/`RAW` to `bytea`, anything unrecognized falls
   back to `text`; for a Postgres source, the exact source column types
   via `format_type()` instead of a lossy mapping), and an optional
   truncate-first (confirmed before running, since it deletes existing
   destination rows). A job runs as a background task per tab, with a live
   per-table progress table (pending/running/done/error, rows copied vs.
   the estimate) polled every 1.5s, and a **Cancel** button. Only column
   data is migrated — primary keys, foreign keys, indexes, and constraints
   are **not** recreated on the destination; that's a deliberate scope
   boundary (add them yourself afterward), not an oversight. Oracle
   connectivity uses `python-oracledb`'s thin mode (pure Python, no Oracle
   Instant Client needed).
10. **✦ AI Assistant** — free-form chat; a fresh live snapshot of that
    specific database is attached as context on every turn.

Every AI feature above shares one pattern: real data pulled moments ago →
sent to Claude → a structured `score / status / issues / recommendations`
card (or, for the Assistant, a normal chat reply) grounded in those actual
values, never generic advice.

**Login** — session auth backed by a SQLite user store (`app/userdb.py`,
file persisted on its own PVC), bcrypt-hashed passwords, signed cookie
session.

**Adding databases** — the "Add a database" form on the Databases screen
registers additional Postgres instances/databases at runtime (host/port/db/
user/password), tested live before saving. Credentials are Fernet-encrypted
at rest in the same SQLite store as login accounts (key derived from
`SESSION_SECRET`). The background poller automatically picks up
newly-registered databases without a restart, and each gets its own
independent `pgmon` history schema.

A background poller snapshots metrics every 30s into a `pgmon` schema
inside `appdb` itself, retained 48h, powering the trend charts and giving
the AI features recent history to reason about.

## Architecture

```
Browser → https://<host>:8443/pgmonitor/*
        → Traefik Ingress (stripPrefix "/pgmonitor") → Service → FastAPI pod
                                                              ↓
                                              asyncpg → postgres-postgresql.postgres.svc (appdb)
                                                              ↓
                                              Anthropic API (claude-sonnet-5), when ANTHROPIC_API_KEY is set
```

Deployed in its own `pgmonitor` namespace, reusing the cluster's existing
ingress entrypoint (no new ports) via a path prefix + Traefik `Middleware`
that strips it before proxying to the app.

## Enabling AI features

AI features are gracefully disabled (UI shows "AI features disabled", API
returns 503) until an Anthropic API key is set:

```bash
kubectl create secret generic pgmonitor-secrets -n pgmonitor \
  --from-literal=DB_PASSWORD="$(cat /root/projects/postgres-password.txt)" \
  --from-literal=SESSION_SECRET="$(kubectl get secret pgmonitor-secrets -n pgmonitor -o jsonpath='{.data.SESSION_SECRET}' | base64 -d)" \
  --from-literal=ANTHROPIC_API_KEY="sk-ant-..." \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/pg-ai-monitor -n pgmonitor
```

## Login

- URL: `https://localhost:8443/pgmonitor/` (or `https://<LAN-IP>:8443/pgmonitor/`
  once the Windows portproxy is refreshed)
- Username: `admin`
- Password: see `/root/projects/pgmonitor-admin-password.txt`

Accounts live in a SQLite database at `/data/pgmon_users.db` inside the pod
(on its own PVC, `pg-ai-monitor-data`, so it survives restarts/redeploys).
Manage accounts with `app/create_user.py` via `kubectl exec`:

```bash
# add a new account
kubectl exec -n pgmonitor deployment/pg-ai-monitor -- python -m app.create_user newuser 'their-password'

# change an existing account's password
kubectl exec -n pgmonitor deployment/pg-ai-monitor -- python -m app.create_user admin 'new-password' --update
```

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
kubectl port-forward -n postgres svc/postgres-postgresql 15432:5432 &
export DB_HOST=127.0.0.1 DB_PORT=15432 DB_NAME=appdb DB_USER=postgres \
       DB_PASSWORD="$(cat /root/projects/postgres-password.txt)" \
       ADMIN_USERNAME=admin ADMIN_PASSWORD_HASH="$(.venv/bin/python -m app.hash_password 'devpass')" \
       SESSION_SECRET="$(openssl rand -hex 32)"
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Rebuilding after code changes

```bash
docker build -t pg-ai-monitor:latest .
k3d image import pg-ai-monitor:latest -c rancher-cluster
kubectl rollout restart deployment/pg-ai-monitor -n pgmonitor
```

The commands above are specific to this dev cluster (k3d + `local-path`).
To deploy this app itself to a *different* Kubernetes cluster (e.g. your
organization's Rancher), see **[DEPLOYING.md](DEPLOYING.md)** and the Helm
chart at `charts/pg-ai-monitor/` -- the plain manifests under `k8s/` won't
work unmodified anywhere but here.

## Deploying a new monitored Postgres instance with full extension support

A plain `helm install bitnami/postgresql ...` gets you stock Postgres
contrib only -- most of the Extensions tab's catalog (see
`app/extension_catalog.py`) either needs a library that isn't in that
image at all (`hypopg`, `pg_repack`, `pg_cron`, `pg_partman`, `pgvector`)
or needs `shared_preload_libraries` set at server *start*
(`pg_stat_statements`, `pgaudit`, `pg_cron`) -- neither of which a later
`CREATE EXTENSION` from the Extensions tab can fix on its own. To get a new
instance where every catalog extension (`postgis` aside -- see
`postgres-image/Dockerfile`) is enable-able from the Extensions tab
immediately:

```bash
helm install <name> bitnami/postgresql -n <namespace> --create-namespace \
  --version 18.11.1 \
  -f postgres-image/new-database-values.yaml \
  --set auth.postgresPassword=admin123 \
  --set auth.database=<dbname> \
  --set containerPorts.postgresql=<port> \
  --set primary.service.ports.postgresql=<port> \
  --set image.tag=<pgmon-postgres-image-tag>
```

Then register it in pgmon (Databases tab -> **+ Add database**) with host
`<name>-postgresql.<namespace>.svc.cluster.local`, the port above, and
username `postgres`. See `postgres-image/new-database-values.yaml` for the
full explanation (including pg_cron's own restriction to a single
database) and `postgres-image/Dockerfile` for how `<pgmon-postgres-image-tag>`
is built.

## Known scope boundaries (by design, not oversights)

- Single admin user via env-var credentials, not a multi-user accounts
  system — reasonable for a personal monitoring tool; would need a real
  users table + RBAC for a shared/production deployment.
- Uses the Postgres superuser credentials already provisioned for this
  cluster, for both monitoring queries and its own `pgmon` schema. A
  production deployment should use a dedicated least-privilege monitoring
  role (`pg_monitor` built-in role) instead.
- `pg_stat_statements` (and every other extension needing
  `shared_preload_libraries` or a non-contrib library) is not enabled on a
  *plain* `helm install bitnami/postgresql` by default — the SQL
  Performance tab and Extensions tab both detect this and explain what's
  missing rather than falsely reporting success. Deploying with
  `postgres-image/new-database-values.yaml` (see above) avoids the gap
  entirely for any new instance; it's still an opt-in step (not automatic)
  since pgmon has no way to reach into an arbitrary already-running
  server's container image or config.
- The Migration tab copies table data only, table-by-table — no primary
  keys, foreign keys, indexes, constraints, views, sequences ownership, or
  triggers. It's meant for getting data into a new database quickly, not
  as a full schema-and-all migration tool; recreate constraints yourself
  afterward. The Oracle type mapping covers the common scalar types
  (`VARCHAR2`, `NUMBER`, `DATE`/`TIMESTAMP`, `CLOB`/`BLOB`, etc.) and falls
  back to `text` for anything exotic (`XMLTYPE`, object types, nested
  tables) rather than failing the whole table.
