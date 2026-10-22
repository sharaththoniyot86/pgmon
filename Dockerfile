FROM python:3.12-slim

WORKDIR /srv

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------- ora2pg ---
# Full schema+data Oracle->Postgres conversion (tables, views, sequences,
# indexes, constraints, triggers, functions/procedures -- everything the
# hand-rolled migration.py deliberately leaves out). ora2pg itself and its
# Postgres driver are packaged in Debian; DBD::Oracle is NOT (Debian can't
# bundle Oracle's proprietary client libs), so it's built here against a
# manually-fetched Oracle Instant Client, same as any other ora2pg install.
ARG INSTANTCLIENT_VERSION=21.13.0.0.0
ARG INSTANTCLIENT_MAJOR=2113000
ENV ORACLE_HOME=/opt/oracle/instantclient \
    LD_LIBRARY_PATH=/opt/oracle/instantclient \
    PATH=/opt/oracle/instantclient:$PATH

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
    && apt-get install -y -qq --no-install-recommends \
        unzip libaio1t64 gcc libc6-dev make perl cpanminus \
        ora2pg postgresql-18 postgresql-18-repack \
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
    # RBAC -- see k8s/ in the repo) is what it authenticates as in-cluster.
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.35.5/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && mkdir -p /opt/oracle && cd /opt/oracle \
    && curl -fsSL -o basic.zip "https://download.oracle.com/otn_software/linux/instantclient/${INSTANTCLIENT_MAJOR}/instantclient-basic-linux.x64-${INSTANTCLIENT_VERSION}dbru.zip" \
    && curl -fsSL -o sdk.zip   "https://download.oracle.com/otn_software/linux/instantclient/${INSTANTCLIENT_MAJOR}/instantclient-sdk-linux.x64-${INSTANTCLIENT_VERSION}dbru.zip" \
    && unzip -q basic.zip && unzip -q sdk.zip && rm basic.zip sdk.zip \
    && mv instantclient_* instantclient \
    && ln -sf libclntsh.so.21.1 instantclient/libclntsh.so \
    # Debian trixie's time64 transition renamed libaio's soname to
    # libaio.so.1t64; Oracle's prebuilt client blob still looks for the
    # classic libaio.so.1, so bridge it via a symlink on the client's own
    # search path rather than touching the system-wide lib directory.
    && ln -sf /usr/lib/x86_64-linux-gnu/libaio.so.1t64 instantclient/libaio.so.1 \
    && echo /opt/oracle/instantclient > /etc/ld.so.conf.d/oracle-instantclient.conf && ldconfig \
    && cpanm --notest DBD::Oracle \
    && apt-get purge -y -qq gcc libc6-dev make cpanminus unzip && apt-get autoremove -y -qq

COPY app ./app

RUN useradd -u 10001 -m appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
