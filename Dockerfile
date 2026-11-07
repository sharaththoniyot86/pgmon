FROM python:3.12-slim

WORKDIR /srv

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The backup/restore feature (app/backup.py) shells out to pg_dump/pg_restore
# against whatever Postgres version the monitored server runs -- pg_dump
# refuses to dump from a server *newer* than itself, and Debian trixie's
# own postgresql-client package (17.x) is already older than current
# Postgres (18.x), so the PGDG apt repo is added here to get a client new
# enough to stay ahead of that for the foreseeable future.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl gnupg ca-certificates \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends postgresql-18 postgresql-18-repack \
        postgresql-client-14 postgresql-client-15 postgresql-client-16 postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/* \
    # postgresql-18 (the full server package, not just postgresql-client-18)
    # -- it includes pg_dump/pg_restore/pg_basebackup/psql same as the
    # client package, plus pg_combinebackup, needed to reassemble a
    # physical+incremental backup chain into a restorable data directory.
    && ln -sf /usr/lib/postgresql/18/bin/* /usr/local/bin/ \
    # kubectl -- the physical/incremental restore flow (app/backup.py)
    # shells out to it to scale the target Postgres StatefulSet down/up
    # and drive a short-lived pod that swaps its data directory; this
    # container's own ServiceAccount (granted narrow, StatefulSet-specific
    # RBAC -- the Helm chart's rbac-physical-restore.yaml) is what it authenticates as in-cluster.
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.35.5/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

COPY app ./app

RUN useradd -u 10001 -m appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
