# pgmon-postgres

The official `postgres:<major>` image (Debian) with every extension package
that apt.postgresql.org publishes for that major already installed. Use it
for the Postgres servers pgmon monitors and the Extensions tab can enable
any Debian-packaged extension with one click, with nothing to install on
the host.

## Build

```sh
podman build --build-arg PG_MAJOR=17 -t pgmon-postgres:17 postgres-image
podman build --build-arg PG_MAJOR=16 -t pgmon-postgres:16 postgres-image
podman build --build-arg PG_MAJOR=15 -t pgmon-postgres:15 postgres-image
```

Build arguments:

| Argument         | Default   | Meaning |
|------------------|-----------|---------|
| `PG_MAJOR`       | `17`      | PostgreSQL major version (14-18). |
| `DEBIAN_RELEASE` | `trixie`  | Debian release of the base image. **Must match** the image the data directory was created with. |
| `EXCLUDE`        | `dbgsym\|-dbg$\|-doc$` | Extended regex of package names to skip. |

Inside the image, `/usr/share/pgmon-postgres/` lists what was installed:
`packages.txt` (package and version), `extensions.txt` (every control
file), and `failed.txt` if any package couldn't be installed.

Air-gapped hosts: `podman save -o pgmon-postgres-17.tar pgmon-postgres:17`,
copy the file, `podman load -i pgmon-postgres-17.tar`.

## Move an existing container onto it

Same major version, same Debian release, same data volume. Check the
release first: `podman exec <name> grep PRETTY /etc/os-release`.

```sh
podman inspect pg17 > pg17.inspect.json        # keep the old definition
podman stop pg17
podman rename pg17 pg17-old
podman run -d --name pg17 \
  -v <same data volume>:/var/lib/postgresql/data \
  -p <same ports> -e <same environment> \
  --restart unless-stopped \
  localhost/pgmon-postgres:17 [same command arguments]
# once it's healthy:
podman rm pg17-old
```

Nothing is preloaded by the image. If the old container passed
`-c shared_preload_libraries=...` on its command line, that setting keeps
overriding `ALTER SYSTEM`, so pgmon's **Add to preload** can't change it;
drop the option and let `postgresql.auto.conf` hold the value, or keep
managing it in the command.

## Extensions that need more than the package

Verified by enabling every extension in the PG17 image, each in a fresh
database:

- **Preload first** (Add to preload, then restart): pg_stat_statements,
  pg_cron, pgaudit, timescaledb, pg_stat_kcache, pg_wait_sampling,
  pg_qualstats, pg_squeeze, pglogical, pgsentinel, powa, repmgr,
  pgautofailover, credcheck, pg_show_plans, pg_stat_plans, pg_rewrite,
  pgnodemx, pg_textsearch, documentdb. Several of these start background
  workers; with many of them preloaded, raise `max_worker_processes`.
- **pgtt** must *not* go in `shared_preload_libraries` (the server refuses
  to start); load it per session or via `session_preload_libraries`.
- **pgautofailover** should only be preloaded on a node that is part of a
  pg_auto_failover setup; elsewhere its background worker blocks
  `DROP DATABASE` indefinitely.
- **documentdb** likewise: with `pg_documentdb` preloaded, its leader
  worker blocks `DROP DATABASE`. Preload it only on a DocumentDB server.
- **pg_cron** can only be created in the database named by
  `cron.database_name` (default `postgres`).
- **pljava** on Java 18+ needs `pljava.allow_unenforced = 'java,javaU'`
  and `pljava.allow_unenforced_udt = on`.
- **pg_snakeoil** needs ClamAV signature databases (freshclam).
- **documentdb_distributed** needs Citus, which PGDG doesn't package.
