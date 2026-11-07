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
the Account page (see §3.12). `viewer` can see everything but every
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
  or on a schedule (hourly/daily/weekly, per database). Backup files are
  stored under the container's `/var/lib/pgmon` data volume, so they
  survive the container being recreated on upgrade; set `BACKUPS_DIR` to
  move them elsewhere.
- **Retention** never prunes the most recent backup however old it is, and
  never prunes a base that a surviving incremental still chains back to.
- **Verify** re-checks a physical/incremental backup against the checksums
  in its own manifest (`pg_verifybackup`). Logical dumps have no manifest,
  so the button isn't offered for them. A backup is checked with the
  `pg_verifybackup` of the PostgreSQL version it came from (the images
  ship client tools 14–18): before 18 that tool can't read tar-format
  backups, so the backup is unpacked into a temporary folder first.
- **S3 storage** (*S3 settings* above the backup controls): copies backups
  to any S3-compatible bucket — MinIO or Ceph in your own air-gapped
  Rancher/Kubernetes cluster, or AWS S3. One destination for all
  databases: endpoint URL (e.g.
  `https://minio.minio.svc.cluster.local:9000`), bucket, optional folder,
  access key and secret, region, path-style URLs (on by default, as MinIO
  needs) and the CA certificate (PEM) that signed the endpoint's TLS
  certificate. *Test & save* lists the bucket and writes and deletes a test
  object before saving; errors say what's wrong (untrusted certificate,
  unreachable endpoint, missing bucket, wrong keys, no permission). The
  access key needs list, read, write and delete on the bucket. pgmon only
  ever contacts that endpoint.
  - Every finished backup, manual or scheduled, is copied to
    `s3://<bucket>/<folder>/<database name>/<backup>` (a physical backup
    as one object per file, with its metadata next to it). The local copy
    stays; the backup list's **S3** column shows *In S3*, or *Failed* with
    the reason and a Retry button. A failed copy never fails the backup.
    **Copy to S3** sends a backup taken before S3 was set up.
  - Deleting a backup in pgmon — by hand or by retention — deletes its S3
    copy too, unless *delete its S3 copy too* is unticked (then manage the
    bucket with its own lifecycle rules).
  - **Only in S3** lists backups in the bucket that pgmon doesn't have
    locally — after pgmon's volume was lost or pgmon was reinstalled.
    **Copy back from S3** brings one back; it then verifies and restores
    like any other backup.
- **WAL mining** needs `pg_waldump`, which ships only in Postgres' server
  package; the standalone container image installs the client package
  alone, so that one panel reports itself unavailable there.
- **WAL archiving** can be turned on (and off) from the tab: pgmon runs
  `ALTER SYSTEM` to set `archive_mode`, `archive_command` (Postgres' own
  recommended `test ! -f DIR/%f && cp %p DIR/%f`, which never overwrites
  an archived segment) and, if `wal_level` is `minimal`, raises it to
  `replica`. `archive_mode` and `wal_level` are postmaster-context, so the
  panel reports the change as *pending restart* until Postgres is
  restarted rather than implying archiving is already live. The archive
  directory must exist on the database server and be writable by the OS
  user Postgres runs as — pgmon can't create it over a SQL connection, and
  says so; where the connected role is a superuser it also checks and
  warns when the directory is missing. Needs a role allowed to run
  `ALTER SYSTEM`; otherwise the controls are disabled with that reason.
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

### 3.6 Extensions
One search box and three lists:

- **Set up this database for monitoring** (shown on a new database): one
  button enables the extensions pgmon itself uses — `pg_stat_statements`,
  `pg_buffercache`, `pgstattuple`, `pg_walinspect`, `amcheck`,
  `pg_visibility`, and `hypopg`/`pg_repack` where the server has them.
- **Enabled** — with Disable, and Update when the server has a newer
  version's files.
- **Ready to enable** — every extension whose files are on the server.
  **Enable** does whatever the extension needs: it creates its
  dependencies too, and if its library must be loaded at server start
  (`pg_cron`, `pgaudit`, `timescaledb`, `pg_stat_statements`, …) pgmon adds
  it to `shared_preload_libraries` and shows *waiting for restart*. After
  you restart Postgres, pgmon finishes enabling it by itself the next time
  the tab is opened (restart the server the way you normally do).
  `auto_explain` is turned on per database (`session_preload_libraries`),
  so it works immediately with no restart; startup-only modules
  (`auth_delay`, `passwordcheck`, …) go into the preload list like the
  extensions above.
- **Not available on this server** — every other extension packaged for
  that PostgreSQL version, each with the one package command for the
  server's platform (`apt-get install -y postgresql-17-cron`,
  `dnf install -y pg_cron_17`), plus anything that must be loaded at
  server start on a server whose start command fixes
  `shared_preload_libraries` — that row says exactly what to add.
  Searching for one opens the list. Every **Enable** button the tab shows
  works on that server.

pgmon reaches databases only over SQL, so it can't install OS packages. A
server running the **pgmon-postgres** image (§6) has every packaged
extension already, so everything is under *Ready to enable*.

### 3.7 Connections
Live connection list (auto-refreshes every 5s) and a blocking-locks view
(who's blocking whom, with a one-click terminate).

### 3.8 Performance Hub
I/O load, current + historical wait events, and memory/buffer-cache
occupancy (the last one needs `pg_buffercache` installed).

### 3.9 Schema
A two-pane browser that fills the window: schemas → objects on the left,
details on the right.

- **Landing view**: every schema with owner, object counts and size — the
  tab answers "what's in this database" before you click anything.
- **Schema view**: that schema's full inventory, biggest object first,
  with kind, size, estimated rows, index count and comment.
- **Tree**: objects listed directly under their schema (no intermediate
  folders), each showing its size and row estimate; partitions nest under
  their parent table, and a partitioned parent reports its whole tree's
  size rather than the 0 bytes it physically stores.
- **Search** runs server-side across every schema at once, so a table is
  findable without expanding the schema it lives in first.
- **Object detail**: a header strip (size, rows, indexes, owner) plus
  Columns (with PK/FK/unique markers and the FK's target table),
  Indexes, **Constraints** — including *Referenced by*, the foreign keys
  pointing at this object that block a `DROP`/`TRUNCATE` — **DDL**
  reconstructed from the catalog, Statistics, and Data (paged, sortable
  real rows).
- **Partitions** tab for partitioned tables: create a partition with a
  bound, or detach an existing one.

The DDL is assembled from `pg_catalog` (`format_type`, `pg_get_expr`,
`pg_get_constraintdef`, `pg_indexes`), covering columns, constraints and
indexes. It is not a `pg_dump` replacement — no ownership, grants,
triggers, RLS or storage parameters — and says so above the output.

### 3.10 Administration
Autovacuum status per table, an estimated-bloat table (dead-tuple %,
sortable), disk usage & growth trend, WAL generation rate, checkpoint
activity, and index-usage stats. A one-click VACUUM action, and an
**Optimize database** modal that recommends `postgresql.conf` values
(shared_buffers, work_mem, etc.) from your hardware specs using the same
formula-based methodology as the community `pgtune` calculator —
deterministic, not AI, works with no configuration.

### 3.11 ✦ AI Assistant
A general-purpose chat interface with context on the current database, for
free-form questions. Requires `ANTHROPIC_API_KEY`.

### 3.12 Account page (top-right menu, not a database tab)
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
including one managed by Rancher. DEPLOYING.md has the same steps in a
shorter, copy-paste-first form.

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

`postgres-image/` builds **pgmon-postgres**: the official Debian-based
`postgres:<major>` image plus every extension package apt.postgresql.org
publishes for that major (PostGIS, pgvector, TimescaleDB, pg_cron,
pg_partman, pgaudit, pg_repack, hypopg, orafce, pg_hint_plan, pglogical,
the PL languages, the foreign data wrappers, ...; 189 extensions for
PostgreSQL 17). On a target running it, every Debian-packaged row in the
Extensions tab is a one-click Enable.

```
podman build --build-arg PG_MAJOR=17 -t pgmon-postgres:17 postgres-image
```

To move an existing `postgres:<major>` container onto it, keep the same
major and the same Debian release (`DEBIAN_RELEASE`, default `trixie`; a
different glibc changes collation order), stop the container, and start a
new one from `pgmon-postgres:<major>` with the same data volume, ports and
environment. See `postgres-image/README.md`.

## 6a. Monitoring CloudNativePG clusters (e.g. on Rancher)

pgmon monitors CloudNativePG clusters like any other PostgreSQL. Deploy it
with its Helm chart (§5) into the same Rancher/Kubernetes cluster and add
each database from the UI:

- **Host**: the cluster's read-write service,
  `<cluster>-rw.<namespace>.svc.cluster.local`, so pgmon follows the
  primary through failovers and switchovers. Add `<cluster>-ro` as a
  separate entry to watch the replicas.
- **Login**: either the `app` user from the `<cluster>-app` secret, or a
  dedicated monitoring role declared in the Cluster's `spec.managed.roles`
  with `inRoles: [pg_monitor]` (plus `CREATE` on the database for pgmon's
  small `pgmon` schema). Untick the replication `pg_hba` option when
  adding it: CloudNativePG manages `pg_hba.conf` itself.
- **pg_stat_statements** (SQL Performance): set any `pg_stat_statements.*`
  parameter in the Cluster spec (CloudNativePG then preloads it) and create
  the extension, e.g. through a `Database` resource's `spec.extensions`.

What works with those logins (verified against a PostgreSQL 17 server set
up like a CloudNativePG instance: settings from postgresql.conf,
`allow_alter_system = off`, non-superuser logins): every tab loads;
Details, Alerts, SQL Performance, Connections, Performance Hub, Schema,
Administration and logical backups work; the Extensions tab enables every
trusted extension in one click and lists the rest with the reason.

Left to CloudNativePG, by design: server settings (pgmon can't use ALTER
SYSTEM there — change `spec.postgresql.parameters`), `shared_preload_libraries`,
`pg_hba` rules, WAL archiving, physical backups and point-in-time recovery
(use its Backup/ScheduledBackup resources and recovery bootstrap), and
anything that needs a superuser (untrusted extensions, reading
`pg_hba_file_rules`) unless the Cluster has `enableSuperuserAccess: true`.

## 7. Known scope boundaries (by design, not oversights)

- Two roles, `admin` and `viewer` — no finer-grained per-database or
  per-feature permissions.
- pgmon uses the Postgres superuser credentials you register a database
  with, for both monitoring and its own `pgmon` schema. A hardened
  production setup would use a dedicated least-privilege monitoring role
  instead.
- No multi-tenancy: every registered database is visible to every
  `admin`/`viewer` account. If different teams need to see different
  databases, that's multiple pgmon deployments today, not one with scoped
  access.
