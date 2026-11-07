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
    # Older majors' client tools too: pg_verifybackup has to match the
    # server a backup came from (app/backup.py's _pg_tool picks them from
    # /usr/lib/postgresql/<major>/bin); 18 stays the one on PATH.
        postgresql-client-14 postgresql-client-15 postgresql-client-16 postgresql-client-17 \
    # postgresql-common only puts /usr/bin wrappers on the client programs
    # it knows about; pg_verifybackup isn't one of them, so the binary
    # shipped in postgresql-client-18 sat in /usr/lib/postgresql/18/bin,
    # off PATH, and the Verify button on every physical/incremental backup
    # failed with a bare "No such file or directory". Symlinking the whole
    # bin directory is what the Kubernetes-oriented Dockerfile at the repo
    # root already does, and covers any future tool the same way.
    && ln -sf /usr/lib/postgresql/18/bin/* /usr/local/bin/ \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

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
