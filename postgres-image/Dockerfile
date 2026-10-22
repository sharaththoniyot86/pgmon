# The pgmon Postgres image: stock Bitnami postgresql plus every extension in
# app/extension_catalog.py marked "bundled": False (hypopg, pg_repack,
# pg_cron, pg_partman, pgvector) -- built from source via PGXS since this
# image is VMware Photon OS (no apt, no PGDG repo; tdnf only), not Debian.
# Pinned to the exact digest currently running in the postgres/postgres-postgresql
# StatefulSet -- re-pin this when that image is upgraded, rather than floating
# on :latest, so this build doesn't silently change the Postgres version too.
#
# Use this SAME image for every monitored Postgres instance -- the original
# primary/replica *and* every new one added later (see
# postgres-image/new-database-values.yaml, which already points at it) --
# so the Extensions tab's non-contrib extensions are enable-able everywhere,
# not just on whichever instance happened to get built by hand. A plain
# `bitnami/postgresql` image will never have these extensions' control
# files, no matter what's run against it over SQL.
#
# postgis is deliberately NOT included: it needs GEOS/PROJ/GDAL, none of
# which exist as tdnf packages on Photon OS, so it'd mean cross-compiling
# a small GIS stack from source -- a much bigger, separate undertaking, not
# a PGXS `make install` like everything else here. It stays available in
# the catalog (app/extension_catalog.py) as "bundled": False, i.e. honestly
# not enable-able on this image today.
#
# Built and verified in isolation (a throwaway pod, not the live primary):
#   docker build -t pg-ai-monitor-postgres:<tag> postgres-image/
#   k3d image import pg-ai-monitor-postgres:<tag> -c rancher-cluster
#
# Deploying it to a real instance needs the Bitnami chart's own
# non-standard-image guardrail lifted (global.security.allowInsecureImages:
# true) -- a deliberate, separate, explicit step; see
# postgres-image/new-database-values.yaml.
#
# pg_repack version MUST match pgmon's own pg_repack *client* binary
# exactly (from the postgresql-18-repack apt package in this app's own
# Dockerfile) -- it refuses to run otherwise ("program 'pg_repack X' does
# not match database library 'pg_repack Y'"). PGDG's apt repo only ever
# carries the latest release, not older ones, so the version here has to
# track whatever that package currently installs, not the other way
# around -- check `apt-cache madison postgresql-18-repack` against a
# trixie-pgdg source before bumping either side independently.
#
# pg_cron and pg_stat_statements (bundled, but still needs this) both
# require shared_preload_libraries at server *start* -- this image just
# makes the control files/libraries exist; postgres-image/new-database-values.yaml
# is what actually turns them on for a given instance.
FROM bitnami/postgresql@sha256:65d7506622ce2649914947b698d822d56c208295ba7ee80c1b34e48b251ba0c1
USER root
RUN tdnf install -y gcc make glibc-devel binutils linux-api-headers zlib-devel readline-devel openssl-devel lz4-devel curl tar \
    && curl -fsSL -o /tmp/hypopg.tar.gz https://github.com/HypoPG/hypopg/archive/refs/tags/1.4.1.tar.gz \
    && tar -xzf /tmp/hypopg.tar.gz -C /tmp \
    && make -C /tmp/hypopg-1.4.1 USE_PGXS=1 PG_CONFIG=/opt/bitnami/postgresql/bin/pg_config install \
    && curl -fsSL -o /tmp/pgrepack.tar.gz https://github.com/reorg/pg_repack/archive/refs/tags/ver_1.5.3.tar.gz \
    && tar -xzf /tmp/pgrepack.tar.gz -C /tmp \
    && make -C /tmp/pg_repack-ver_1.5.3 USE_PGXS=1 PG_CONFIG=/opt/bitnami/postgresql/bin/pg_config install \
    && curl -fsSL -o /tmp/pgcron.tar.gz https://github.com/citusdata/pg_cron/archive/refs/tags/v1.6.8.tar.gz \
    && tar -xzf /tmp/pgcron.tar.gz -C /tmp \
    && make -C /tmp/pg_cron-1.6.8 USE_PGXS=1 PG_CONFIG=/opt/bitnami/postgresql/bin/pg_config install \
    && curl -fsSL -o /tmp/pgpartman.tar.gz https://github.com/pgpartman/pg_partman/archive/refs/tags/v5.1.0.tar.gz \
    && tar -xzf /tmp/pgpartman.tar.gz -C /tmp \
    && make -C /tmp/pg_partman-5.1.0 USE_PGXS=1 PG_CONFIG=/opt/bitnami/postgresql/bin/pg_config install \
    && curl -fsSL -o /tmp/pgvector.tar.gz https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.6.tar.gz \
    && tar -xzf /tmp/pgvector.tar.gz -C /tmp \
    && make -C /tmp/pgvector-0.8.6 USE_PGXS=1 PG_CONFIG=/opt/bitnami/postgresql/bin/pg_config install \
    && tdnf clean all \
    && rm -rf /tmp/*.tar.gz /tmp/hypopg-* /tmp/pg_repack-* /tmp/pg_cron-* /tmp/pg_partman-* /tmp/pgvector-* \
    && chmod -R a+rX /opt/bitnami/postgresql/lib /opt/bitnami/postgresql/share/extension /opt/bitnami/postgresql/bin
USER 1001
