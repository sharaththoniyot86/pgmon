FROM python:3.12-slim

WORKDIR /opt/pgmon

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        nginx openssl curl gnupg ca-certificates \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update -qq \
    # postgresql-client-18, not Debian trixie's own (older) postgresql-client:
    # pg_dump/pg_restore refuse to talk to a Postgres server *newer* than
    # themselves, and this needs to stay ahead of whatever the monitored
    # server runs -- same reasoning as the main Kubernetes-oriented
    # Dockerfile at the repo root.
    && apt-get install -y -qq --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

# ---------------------------------------------------------------- ora2pg ---
# Full schema+data Oracle->Postgres conversion, same reasoning and setup as
# the Kubernetes-oriented Dockerfile at the repo root (see its comments for
# the full rationale) -- trimmed here of the two things that image needs
# but this standalone one doesn't: `kubectl` (physical restore is a
# Kubernetes-only feature, not reachable from this image at all) and the
# full `postgresql-18` server package (only needed there for
# pg_combinebackup, part of that same k8s-only feature -- this image's
# already-installed postgresql-client-18 covers pg_dump/pg_restore/psql).
ARG INSTANTCLIENT_VERSION=21.13.0.0.0
ARG INSTANTCLIENT_MAJOR=2113000
ENV ORACLE_HOME=/opt/oracle/instantclient \
    LD_LIBRARY_PATH=/opt/oracle/instantclient \
    PATH=/opt/oracle/instantclient:$PATH

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        unzip libaio1t64 gcc libc6-dev make perl cpanminus ora2pg \
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
    && apt-get purge -y -qq gcc libc6-dev make cpanminus unzip && apt-get autoremove -y -qq \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY app ./app
# Kept outside conf.d (as a template, with the port still a literal
# placeholder) rather than installed directly -- the entrypoint renders it
# into conf.d fresh on every container start using $PGMON_PORT, so changing
# that env var on a new container (or a recreated one) always takes effect
# rather than depending on in-place sed idempotency across restarts.
COPY packaging/docker/pgmon-nginx.conf /etc/nginx/templates/pgmon.conf.template
COPY packaging/docker/entrypoint.sh /usr/local/bin/pgmon-entrypoint
COPY packaging/docker/pgmon-reset-password /usr/local/bin/pgmon-reset-password
RUN chmod +x /usr/local/bin/pgmon-entrypoint /usr/local/bin/pgmon-reset-password

# Not run as a non-root user, unlike the Kubernetes-oriented image at the
# repo root -- this single container also runs nginx's master process,
# which needs root (or CAP_NET_BIND_SERVICE) to bind ports below 1024
# such as the default 443. Isolation is the container boundary itself,
# same tradeoff the RPM/systemd install makes (nginx there also runs as
# root's own services, just split across two system services instead of
# one container).
EXPOSE 443 80
VOLUME ["/etc/pgmon", "/var/lib/pgmon"]
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s CMD curl -fsSk https://127.0.0.1:${PGMON_PORT:-443}/healthz || exit 1

ENTRYPOINT ["pgmon-entrypoint"]
