# pg-ai-monitor — Application Documentation

## 1. What this is

pg-ai-monitor is a self-hosted, web-based PostgreSQL monitoring and
administration dashboard. Point it at one or more Postgres servers and it
gives you live health metrics, query performance analysis, backup/restore
tooling, security auditing, and a curated extension catalog — plus a set
of deterministic ("no AI needed") tuning tools, with optional AI-assisted
review layered on top when an Anthropic API key is configured.

It is not an agent that changes your database on its own. Every mutating
action (running a VACUUM, adding a pg_hba rule, enabling an extension,
taking a backup) is a deliberate, logged, admin-only click in the UI.

**Who it's for**: a team (or a single DBA/platform engineer) that wants a
lightweight, self-hosted alternative to piecing together `psql` one-liners,
pgAdmin, and ad hoc scripts — without needing a hosted SaaS monitoring
product or Postgres extensions installed on servers you don't control the
image for.

## 2. Architecture

| Layer | What it is |
|---|---|
| Backend | Python 3.12, FastAPI, `asyncpg` for all Postgres connections |
| Frontend | Server-rendered HTML (Jinja2) + a single vanilla-JS file (`app/static/js/app.js`) — no build step, no framework |
| pgmon's own storage | SQLite (`app/userdb.py`, `app/dbstore.py`, `app/audit.py`, `app/backup_schedule.py`) on a small PVC — login accounts, the list of registered databases, the audit log, backup schedules |
| Per-monitored-database storage | A `pgmon` schema pgmon creates *inside* each registered database, holding its own metrics snapshot history (see `app/db.py`) |
| Background jobs | A poller (`app/poller.py`, default every 30s) snapshots metrics and pre-computes alerts for every registered database; a scheduler (`app/scheduler.py`) runs backup schedules and WAL-archive pruning |
| AI integration | Optional. Only used for a handful of explicitly-labeled "✦ AI ..." buttons. Every other feature (including the Tuning Advisor, the deterministic tuning calculator, and every metric/alert) works with `ANTHROPIC_API_KEY` unset |

**Multi-database, not multi-tenant**: pgmon monitors only what you
explicitly register via the Databases screen — there is no built-in
"default" database. Each registered entry is just a host/port/dbname/
username/password; pgmon opens its own connection pool per entry and
never assumes anything about how that Postgres server was deployed.

**Roles**: two application-level roles, `admin` and `viewer`, managed on
the Account page (see §3.11). `viewer` can see everything but every
mutating endpoint (VACUUM, backups, role/extension/pg_hba changes, user
management) requires `admin`.

## 3. Feature tour

Screen 1, the **Databases** list, shows every registered database with
live status/latency/connection count and an alert-count badge, and is
where you add a new one (name, host, port, dbname, username, password —
tested with a real connection attempt before it's saved). Clicking a row
opens the detail screen, which has these tabs:

### 3.1 Details
Point-in-time server info (version, size, uptime, cache hit ratio),
connections-over-time and cache-hit-ratio-over-time charts (from pgmon's
own snapshot history), and the full `pg_settings` table.

### 3.2 Alerts
A live-updated list of ~20 categorized checks (`error`/`warning`/`info`),
computed by the background poller so the badge on the Databases screen is
always current without you having to open the tab. Covers: connection
pool exhaustion, cache hit ratio, deadlocks, long-running queries,
idle-in-transaction sessions, blocked sessions, disabled/failing
autovacuum, table bloat, checkpoint frequency, SSL/password-encryption
posture, insecure `pg_hba.conf` rules, and WAL archiving problems.

### 3.3 SQL Performance
- **Top queries**, from `pg_stat_statements` — sortable by total time,
  mean time, calls, rows, disk reads, temp-block spill, or WAL generated;
  filterable to all databases on the server or just this one.
- **View plan & Tuning Advisor** (per-row button, no AI required): runs a
  real `EXPLAIN` (optionally `ANALYZE`, restricted to read-only
  SELECT/WITH since that actually executes the query), auto-substituting
  parameter placeholders with a representative real value where it safely
  can. The Tuning Advisor layer adds a deterministic, Oracle-SQL-Tuning-
  Advisor-style report: a **verified** missing-index suggestion (simulated
  via `hypopg` and measured against the planner's own cost estimate,
  nothing actually created), plus flags for large sequential scans,
  disk-spilling sorts, and stale statistics.
- **✦ AI Query Optimizer** (optional): paste any read-only query and get
  an `EXPLAIN ANALYZE` plus an AI-written explanation. Requires
  `ANTHROPIC_API_KEY`.

### 3.4 Backup & Recovery
- **Backups**: logical (`pg_dump`, this database only), physical
  (`pg_basebackup`, the whole server), and incremental (PG17+, built
  against a prior physical/incremental backup's manifest) — run on demand
  or on a schedule (hourly/daily/weekly, per database).
- **Restore**: logical restores run directly; physical/incremental
  restores are a bigger operation (stop Postgres, swap the data directory)
  gated behind explicit opt-in configuration (see §5's `physicalRestore`
  values) since it needs narrow, StatefulSet-specific RBAC.
- **Temporary instances**: spins up a short-lived, disposable Postgres pod
  from a backup for point-in-time inspection without touching the real
  server.
- WAL/archiving status (`archive_mode`, failures, last checkpoint,
  connected replicas) is shown here too.

### 3.5 Security
- **Security posture**: SSL enforcement, password-encryption method,
  superuser count.
- **Roles**: create/edit roles and attributes (including `LOGIN`,
  `SUPERUSER`, `REPLICATION`, connection/statement limits), grant/revoke
  object privileges, view a role's active sessions, drop a role
  (with an option to reassign owned objects first).
- **pg_hba authentication rules**: view the live rule set, and **append a
  new rule** (type, database, user, address, auth method) — applied
  immediately via `pg_reload_conf()`, no restart needed. Deliberately
  append-only (a rewrite-in-place to edit/remove a rule risks corrupting
  the file); `trust`/`password` auth methods are refused since they're
  exactly what this tab's own alerts flag as insecure.
- **Installed extensions** summary (full management is on the Extensions
  tab).

### 3.6 Extensions
A curated, ~25-entry reference catalog (categories: Monitoring &
Performance, Search & Text, Data Types, Security, Federation & Utility)
showing live per-database availability and one-click enable/disable.
"Available" means the extension's control file already exists on that
specific Postgres server — pgmon can't install a genuinely new compiled
extension onto an arbitrary server over a SQL connection. Extensions that
need `shared_preload_libraries` (`pg_stat_statements`, `pgaudit`,
`pg_cron`) are checked for that precondition before claiming success,
rather than reporting a false "enabled." See §6 for how to get a Postgres
image/deployment where every catalog extension (except PostGIS — see
§6) is enable-able out of the box.

### 3.7 Connections
Live connection list (auto-refreshes every 5s) and a blocking-locks view
(who's blocking whom, with a one-click terminate).

### 3.8 Performance Hub
I/O load, current + historical wait events, and memory/buffer-cache
occupancy (the last one needs `pg_buffercache` installed).

### 3.9 Schema
Browse schemas → tables/views → columns, page through real row data
(filterable, sortable), and manage declarative partitions (create a new
partition with a bound, detach an existing one) for partitioned tables.

### 3.10 Administration
Autovacuum status per table, an estimated-bloat table (dead-tuple %,
sortable), disk usage & growth trend, WAL generation rate, checkpoint
activity, and index-usage stats. A one-click VACUUM action, and an
**Optimize database** modal that recommends `postgresql.conf` values
(shared_buffers, work_mem, etc.) from your hardware specs using the same
formula-based methodology as the community `pgtune` calculator —
deterministic, not AI, works with no configuration.

### 3.11 Migration
Two paths, both writing into whichever database this tab is open on:
- A hand-rolled table-by-table copy tool (source: Oracle or another
  Postgres database) — data only, fast to set up, no PK/FK/index/
  trigger recreation (do that yourself afterward).
- A full `ora2pg`-driven schema+data migration (Oracle sources only) when
  you need constraints, sequences, views, and more carried over
  automatically.

### 3.12 ✦ AI Assistant
A general-purpose chat interface with context on the current database, for
free-form questions. Requires `ANTHROPIC_API_KEY`.

### 3.13 Account page (top-right menu, not a database tab)
Change your own password; an admin-only **Users** section to create/list/
change-role/delete application accounts (`admin`/`viewer`); and the
**Audit log** — every mutating action taken through the UI, by whom, and
when (separate from Postgres's own `pgaudit`, which logs raw SQL on the
server side if you enable it).

## 4. Enabling AI features (optional)

Every feature above works with no AI configured. To turn on the
"✦ AI ..." buttons, set `ANTHROPIC_API_KEY` (and optionally
`ANTHROPIC_MODEL`, default `claude-sonnet-5`) — see the Secret in §5.2.

## 5. Deploying to Rancher

This app ships as a plain container image plus a Helm chart
(`charts/pg-ai-monitor/`) designed to install on any Kubernetes cluster,
including one managed by Rancher. (The raw manifests under `k8s/` in this
repo are hardcoded to this project's own dev cluster — k3d, a
`local-path` StorageClass, a Traefik-specific Ingress — don't reuse those
elsewhere; use the chart.)

### 5.1 Build and publish the image

```bash
docker build -t pg-ai-monitor:1.0.0 .
```

Get it onto your Rancher cluster's nodes one of two ways:

- **You have a registry the cluster can already pull from** (Harbor,
  Nexus, a cloud registry, Docker Hub, ...):
  ```bash
  docker tag pg-ai-monitor:1.0.0 <your-registry>/<project>/pg-ai-monitor:1.0.0
  docker push <your-registry>/<project>/pg-ai-monitor:1.0.0
  ```
  If it's private, create a pull secret in the target namespace and pass
  its name via the chart's `image.pullSecrets` value.

- **No shared registry / air-gapped**: save the image as a file and load
  it directly onto the target nodes:
  ```bash
  docker save pg-ai-monitor:1.0.0 -o pg-ai-monitor-1.0.0.tar
  # copy the file to the target environment, then:
  docker load -i pg-ai-monitor-1.0.0.tar        # Docker-backed nodes
  ctr -n k8s.io images import pg-ai-monitor-1.0.0.tar   # containerd (RKE2/K3s), run per node
  ```
  A multi-node cluster needs this on every node the pod could land on —
  past a couple of nodes, push to a registry instead.

### 5.2 Required configuration

Two chart values have no default and must be set for your cluster:

| Value | What it is |
|---|---|
| `persistence.storageClassName` | Your cluster's StorageClass for a small (256Mi default) ReadWriteOnce volume — e.g. `longhorn`, `standard`, a cloud CSI class. This is where pgmon's own accounts/registered-databases/audit-log SQLite files live. |
| `sessionSecret` | Signs login session cookies. Generate with `openssl rand -hex 32` and keep it stable across upgrades — changing it logs everyone out. |

### 5.3a Install via the Rancher UI

1. In Rancher, open the target cluster → **Apps & Marketplace** (or
   **Apps > Charts**, depending on your Rancher version).
2. If your org runs a chart repository (Harbor, ChartMuseum, a git repo
   Rancher can add as a repo source), push `charts/pg-ai-monitor/` there
   and add it as a repository, then install it like any other app — the
   install form's fields map 1:1 to `charts/pg-ai-monitor/values.yaml`.
3. If there's no chart repo available, use **Cluster Explorer → Apps →
   Install → "Custom"** (naming varies by Rancher version) to install
   directly from the chart directory without a repo, or fall back to the
   CLI method below and just use `kubectl`/`helm` against the cluster's
   kubeconfig, which Rancher gives you from the same UI (top-right →
   "Download KubeConfig" or the built-in kubectl shell).
4. Set at minimum `image.repository`, `image.tag`,
   `persistence.storageClassName`, and `sessionSecret`; everything else in
   `values.yaml` has a working default.

### 5.3b Install via the CLI (works against any cluster, Rancher-managed or not)

```bash
helm install pg-ai-monitor charts/pg-ai-monitor \
  --namespace pgmonitor --create-namespace \
  --set image.repository=<your-registry>/<project>/pg-ai-monitor \
  --set image.tag=1.0.0 \
  --set persistence.storageClassName=<your-storage-class> \
  --set sessionSecret="$(openssl rand -hex 32)"
```

Grab the cluster's kubeconfig from Rancher (cluster page → top-right →
"Download KubeConfig") if you're not already pointed at it.

### 5.4 First login

The chart does not create a default admin account — pgmon starts with an
empty accounts table on purpose (see `NOTES.txt`, which `helm install`
prints, for this exact command):

```bash
kubectl exec -n pgmonitor deployment/pg-ai-monitor -- \
  python -m app.create_user admin '<a-real-password>'
```

### 5.5 Exposing it (Ingress)

`ingress.enabled=false` by default (reach it via `kubectl port-forward`
until you're ready). To expose it:

```bash
helm upgrade pg-ai-monitor charts/pg-ai-monitor -n pgmonitor --reuse-values \
  --set ingress.enabled=true \
  --set ingress.host=pgmonitor.yourorg.example.com \
  --set ingress.className=<your-ingress-class>   # e.g. nginx
```

The chart's Ingress template is a plain, controller-agnostic
`networking.k8s.io/v1` resource — the simplest setup is a dedicated
hostname serving at `/` (`basePath: ""`, the default), which needs no
path-rewrite annotation. If you instead want it under a path prefix on a
shared host, set `basePath` **and** whatever prefix-stripping annotation
your actual ingress controller needs via `ingress.annotations` — that
part is controller-specific (nginx-ingress:
`nginx.ingress.kubernetes.io/rewrite-target`; Traefik: a `Middleware`
CRD) and the chart can't guess it for you.

### 5.6 Optional: physical/incremental backup restore automation

Off by default. Logical (`pg_dump`) backup/restore needs no configuration
either way. If you want the Backup & Recovery tab's physical/incremental
*restore* automation (it scales a specific Postgres StatefulSet and drives
short-lived worker pods against its PVC), set `physicalRestore.enabled=true`
plus `physicalRestore.namespace`/`statefulSet`/`pvc` — see
`charts/pg-ai-monitor/values.yaml` for the full list and what each does.
This creates a separate, narrowly-scoped Role/RoleBinding in *that*
StatefulSet's namespace (which may differ from pgmon's own).

### 5.7 Verifying the deployment

```bash
kubectl rollout status deployment/pg-ai-monitor -n pgmonitor
kubectl logs -n pgmonitor deployment/pg-ai-monitor --tail=20
```

A healthy startup log looks like:
```
INFO:pgmon:pgmon started (base_path='', ai_enabled=False)
INFO:     Application startup complete.
```

See `DEPLOYING.md` for the same steps in a shorter, copy-paste-first
format, and `charts/pg-ai-monitor/values.yaml` for every configurable
value with inline explanations.

## 6. Getting full extension support on a new Postgres instance

A plain, unmodified Postgres deployment only has stock contrib
extensions, and even two of *those* (`pg_stat_statements`, `pgaudit`)
need `shared_preload_libraries` set at server start — something no amount
of clicking "Enable" in the Extensions tab can retroactively fix. If
you're also deploying the Postgres instances pgmon will monitor (as
opposed to pointing it at ones your org already runs), see
`postgres-image/Dockerfile` (adds `hypopg`, `pg_repack`, `pg_cron`,
`pg_partman`, `pgvector` on top of the stock Bitnami image — PostGIS is
deliberately not included; it needs a GEOS/PROJ/GDAL stack this minimal
image doesn't build) and `postgres-image/new-database-values.yaml` (Helm
values that also set `shared_preload_libraries` correctly) for a
ready-made way to get every catalog extension enable-able immediately.

## 7. Known scope boundaries (by design, not oversights)

- Two roles, `admin` and `viewer` — no finer-grained per-database or
  per-feature permissions.
- pgmon uses the Postgres superuser credentials you register a database
  with, for both monitoring and its own `pgmon` schema. A hardened
  production setup would use a dedicated least-privilege monitoring role
  instead.
- The Migration tab's hand-rolled path copies table data only — no
  primary keys, foreign keys, indexes, constraints, views, or triggers;
  use the ora2pg-driven path (Oracle sources) if you need those carried
  over automatically.
- No multi-tenancy: every registered database is visible to every
  `admin`/`viewer` account. If different teams need to see different
  databases, that's multiple pgmon deployments today, not one with scoped
  access.
