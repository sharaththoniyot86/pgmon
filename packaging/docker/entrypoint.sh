#!/usr/bin/env bash
# Container entrypoint for the self-contained, standalone pgmon image.
# Mirrors what the RPM's %post / the universal Linux installer do on first
# boot (generate config, a self-signed cert, and an initial admin account),
# but gated on file existence in the two mounted volumes (/etc/pgmon,
# /var/lib/pgmon) rather than an RPM transaction's $1-eq-1 "fresh install"
# flag -- "first run" here means "those volumes are still empty", which
# holds whether that's because the container was just created or because
# no volume was mounted at all (ephemeral use, e.g. a quick local test).
set -euo pipefail

CONFIG_DIR=/etc/pgmon
DATA_DIR=/var/lib/pgmon
ENV_FILE="$CONFIG_DIR/pgmon.env"
CERT_FILE="$CONFIG_DIR/tls/pgmon.crt"
KEY_FILE="$CONFIG_DIR/tls/pgmon.key"
USERS_DB="$DATA_DIR/pgmon_users.db"

mkdir -p "$CONFIG_DIR/tls" "$DATA_DIR"

PORT="${PGMON_PORT:-443}"
case "$PORT" in
    ''|*[!0-9]*) echo "WARNING: PGMON_PORT='$PORT' isn't a number -- using 443 instead." >&2; PORT=443 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "WARNING: PGMON_PORT=$PORT is out of range -- using 443 instead." >&2
    PORT=443
fi

if [ ! -f "$ENV_FILE" ]; then
    SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "$ENV_FILE" <<EOF
# pgmon runtime configuration. After editing: restart this container.
SESSION_SECRET=$SESSION_SECRET
USERS_DB_PATH=$USERS_DB
BASE_PATH=
DEPLOYMENT_MODE=container
POLL_INTERVAL_SECONDS=30
SNAPSHOT_RETENTION_HOURS=48
# ANTHROPIC_API_KEY=
# ANTHROPIC_MODEL=claude-sonnet-5
EOF
    chmod 600 "$ENV_FILE"
fi
# The persisted config acts as *defaults*, not overrides. Sourcing it
# plainly meant any key it contains silently beat the same variable passed
# with `docker run -e` / `podman run -e` -- so `-e POLL_INTERVAL_SECONDS=15`
# on an existing install did nothing at all, with no error to explain why.
# Snapshotting the environment first and re-applying it afterwards makes -e
# win, while a key only present in the file still takes effect.
_env_before="$(mktemp)"
export -p > "$_env_before"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
# Re-applying can trip over readonly names (UID and friends); those are
# identical either way, so a failure there must not abort startup.
set +e
# shellcheck disable=SC1090
. "$_env_before" 2>/dev/null
set -e
rm -f "$_env_before"

set -a
# Forced after sourcing, not just defaulted above: this is not a per-install
# choice -- running inside this image *is* the deployment mode, and it
# decides which password-reset instructions the login/account pages show.
# Existing volumes were written before this mode existed and still say
# "standalone" on disk, which told locked-out users to run
# pgmon-reset-password on the host, where it does not exist. Overriding here
# fixes those installs with no migration step.
DEPLOYMENT_MODE=container
set +a

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    CN="$(python -c 'import socket; print(socket.getfqdn())')"
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -subj "/CN=$CN" \
        -addext "subjectAltName=DNS:$CN,DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1 || \
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -subj "/CN=$CN" >/dev/null 2>&1
    chmod 600 "$KEY_FILE"
    chmod 644 "$CERT_FILE"
fi

if [ ! -f "$USERS_DB" ]; then
    ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(18))')"
    cd /opt/pgmon
    if HELPER_OUT="$(python -m app.create_user admin "$ADMIN_PASSWORD" 2>&1)"; then
        echo ""
        echo "================================================================"
        echo " pgmon admin account created:"
        echo "   username: admin"
        echo "   password: $ADMIN_PASSWORD   (shown once -- change it after logging in)"
        echo "================================================================"
    else
        # Surfaced rather than silently discarded, same reasoning as the
        # RPM's %post: a first-run failure here with no visible reason is
        # exactly the kind of thing that costs real debugging time later.
        echo ""
        echo "WARNING: couldn't create the initial admin account automatically:"
        echo "$HELPER_OUT" | sed 's/^/  /'
        echo "Create one yourself with: docker exec -it <container> pgmon-reset-password admin"
    fi
fi

sed "s/__PGMON_HTTPS_PORT__/$PORT/g" /etc/nginx/templates/pgmon.conf.template > /etc/nginx/conf.d/pgmon.conf
nginx -t

cd /opt/pgmon
# --workers 1, deliberately: app/backup.py (and migration.py,
# ora2pg_migration.py, repack.py, partitioning.py) track running jobs in a
# plain in-process dict, not a shared store -- with more than one worker,
# whichever one didn't handle the job-creating POST 404s on every status
# poll that happens to land on it, rendering as "Backup: undefined (NaNh
# NaNm elapsed)" in the UI. Confirmed as the actual cause, not guessed.
# Single-process matches the Kubernetes deployment's own behavior (plain
# uvicorn, no multi-worker) -- fine here too since these endpoints are
# I/O-bound (async), not CPU-bound.
gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --workers 1 \
    --bind 127.0.0.1:8000 --timeout 120 --access-logfile - --error-logfile - &
GUNICORN_PID=$!

# nginx's own master process; `daemon off` keeps it attached so this
# script's `wait` actually blocks on it, same pattern as any other
# foreground-process container entrypoint.
nginx -g 'daemon off;' &
NGINX_PID=$!

cleanup() {
    kill -TERM "$NGINX_PID" "$GUNICORN_PID" 2>/dev/null || true
    wait "$NGINX_PID" "$GUNICORN_PID" 2>/dev/null || true
}
trap cleanup TERM INT

if [ "$PORT" -eq 443 ]; then
    PGMON_URL_NOTE="https://<this-host>/"
else
    PGMON_URL_NOTE="https://<this-host>:$PORT/"
fi
echo ""
echo "================================================================"
echo " pgmon is starting -- once ready, browse to: $PGMON_URL_NOTE"
echo " (self-signed certificate -- your browser will warn about this"
echo " once; that's expected, not an error.)"
echo "================================================================"

# Either process exiting (crash or normal shutdown) should bring the
# container down -- `wait -n` returns on the first one to exit.
wait -n "$NGINX_PID" "$GUNICORN_PID"
cleanup
