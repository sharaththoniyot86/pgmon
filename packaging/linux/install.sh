#!/usr/bin/env bash
# pgmon installer -- sets this app up as a systemd-managed service behind an
# Nginx reverse proxy, on any systemd-based Linux distro (Debian/Ubuntu,
# RHEL/Fedora/Rocky/Alma, openSUSE). Run as root from an extracted pgmon
# release tarball:
#
#   tar xzf pgmon-1.0.0.tar.gz && cd pgmon-1.0.0
#   sudo ./packaging/install.sh
#
# Re-running this script (e.g. after dropping in a new app/ directory from a
# newer release) upgrades an existing install in place: code under
# /opt/pgmon is replaced, but /etc/pgmon (config) and /var/lib/pgmon (the
# SQLite accounts DB + anything else written at runtime) are left alone, and
# no new admin account is created if one already exists.
set -euo pipefail

APP_USER="pgmon"
APP_GROUP="pgmon"
INSTALL_DIR="/opt/pgmon"
DATA_DIR="/var/lib/pgmon"
CONFIG_DIR="/etc/pgmon"
ENV_FILE="$CONFIG_DIR/pgmon.env"
LISTEN_PORT="127.0.0.1:8000"
HTTP_PORT="${PGMON_HTTP_PORT:-80}"
GUNICORN_VERSION="23.0.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"   # the extracted release root: app/, requirements.txt, packaging/

# A release built by build-package.sh --airgap ships every pip dependency as
# a pre-downloaded wheel under vendor/wheels/ -- its presence is what tells
# this same install.sh to skip PyPI (and, below, skip trying to reach OS
# package repos too) rather than needing a separate air-gapped install.sh.
VENDOR_WHEELS_DIR="$SRC_DIR/vendor/wheels"
AIRGAP=0
[[ -d "$VENDOR_WHEELS_DIR" ]] && AIRGAP=1

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo ./packaging/install.sh)."
[[ -d "$SRC_DIR/app" && -f "$SRC_DIR/requirements.txt" ]] || die "Can't find app/ and requirements.txt next to this script -- run install.sh from inside the extracted release tarball."

# --------------------------------------------------------------- package manager
if [[ $AIRGAP -eq 1 ]]; then
    log "Air-gapped bundle detected (vendor/wheels/ present) -- skipping OS package-repo access entirely."
    log "Verifying Python, Nginx and Postgres client tools are already installed..."
    MISSING=()
    command -v nginx >/dev/null 2>&1 || MISSING+=("nginx")
    command -v psql >/dev/null 2>&1 || MISSING+=("postgresql-client (psql)")
    command -v pg_dump >/dev/null 2>&1 || MISSING+=("postgresql-client (pg_dump)")
    if [[ ${#MISSING[@]} -gt 0 ]]; then
        die "Missing required packages that this air-gapped bundle doesn't vendor: ${MISSING[*]}. Install them from your distro's offline/internal package mirror or installation media, then re-run this installer. (Only the Python/pip layer is vendored in vendor/wheels/ -- see packaging/linux/README.md.)"
    fi
else
    if command -v apt-get >/dev/null 2>&1; then PKG=apt
    elif command -v dnf >/dev/null 2>&1; then PKG=dnf
    elif command -v yum >/dev/null 2>&1; then PKG=yum
    elif command -v zypper >/dev/null 2>&1; then PKG=zypper
    else die "No supported package manager found (apt-get/dnf/yum/zypper)."
    fi
    log "Detected package manager: $PKG"

    install_system_packages() {
        case "$PKG" in
            apt)
                apt-get update -qq
                DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
                    python3 python3-venv python3-pip nginx postgresql-client ca-certificates
                ;;
            dnf)
                dnf install -y -q python3 python3-pip nginx postgresql
                # RHEL/Rocky/Alma 9's default python3 is 3.9, too old for this app
                # (needs 3.10+) -- a newer interpreter is one more package away on
                # AppStream, so grab it too; harmless no-op where it's already
                # the default, and quietly skipped where no such package exists
                # (e.g. RHEL 8, where 'dnf module install python3.12' is the
                # manual fallback the error message below points at).
                dnf install -y -q python3.12 2>/dev/null || dnf install -y -q python3.11 2>/dev/null || true
                ;;
            yum)
                yum install -y -q python3 python3-pip nginx postgresql
                ;;
            zypper)
                zypper --non-interactive --quiet install python3 python3-pip python3-venv nginx postgresql
                ;;
        esac
    }
    log "Installing system packages (Python, Nginx, Postgres client tools)..."
    install_system_packages
fi

# --------------------------------------------------------------------- Python
# str|None-style unions (PEP 604) run throughout app/ -- needs 3.10+. Most
# distros from the last few years ship that as python3 already; fall back to
# scanning for a newer versioned binary before giving up.
PYTHON_BIN=""
for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done
[[ -n "$PYTHON_BIN" ]] || die "Python 3.10+ is required but wasn't found. Install one (e.g. the deadsnakes PPA on older Ubuntu, or 'dnf module install python3.12' on RHEL/Fedora) and re-run."
log "Using $($PYTHON_BIN --version) at $PYTHON_BIN"

# --------------------------------------------------------------------- user
NOLOGIN_SHELL="$(command -v nologin || true)"
[[ -n "$NOLOGIN_SHELL" ]] || NOLOGIN_SHELL="/usr/sbin/nologin"
[[ -x "$NOLOGIN_SHELL" ]] || NOLOGIN_SHELL="/sbin/nologin"
if ! id "$APP_USER" >/dev/null 2>&1; then
    log "Creating system user '$APP_USER'..."
    useradd --system --home-dir "$INSTALL_DIR" --shell "$NOLOGIN_SHELL" "$APP_USER"
fi

# ---------------------------------------------------------------- directories
mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$CONFIG_DIR"

log "Installing application code to $INSTALL_DIR..."
rm -rf "$INSTALL_DIR/app"
cp -r "$SRC_DIR/app" "$INSTALL_DIR/app"
cp "$SRC_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
find "$INSTALL_DIR/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

if [[ -f "$SCRIPT_DIR/pgmon-reset-password" ]]; then
    install -m 755 "$SCRIPT_DIR/pgmon-reset-password" /usr/local/bin/pgmon-reset-password
fi

log "Creating virtualenv and installing Python dependencies (this can take a minute)..."
"$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
if [[ $AIRGAP -eq 1 ]]; then
    cp -r "$VENDOR_WHEELS_DIR" "$INSTALL_DIR/vendor-wheels"
    # No `pip install --upgrade pip` here -- that would reach PyPI too; the
    # pip bundled with this Python's venv module is new enough to understand
    # the manylinux tags these wheels were downloaded for.
    "$INSTALL_DIR/venv/bin/pip" install --quiet --no-index --find-links "$INSTALL_DIR/vendor-wheels" \
        -r "$INSTALL_DIR/requirements.txt" "gunicorn==$GUNICORN_VERSION"
else
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" "gunicorn==$GUNICORN_VERSION"
fi

# ------------------------------------------------------------------- config
FRESH_INSTALL=0
if [[ ! -f "$ENV_FILE" ]]; then
    FRESH_INSTALL=1
    log "Writing default config to $ENV_FILE..."
    SESSION_SECRET="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "$ENV_FILE" <<EOF
# pgmon runtime configuration.
# After editing, apply with: systemctl restart pgmon

# Signs session cookies and derives the keys that encrypt stored database
# passwords / the AI API key at rest. Generated once at install time --
# changing it invalidates existing sessions and makes previously-encrypted
# secrets unreadable, so back it up along with $DATA_DIR.
SESSION_SECRET=$SESSION_SECRET

# Where the local accounts/settings SQLite DB lives. Kept outside
# $INSTALL_DIR so it survives an in-place upgrade (re-running install.sh).
USERS_DB_PATH=$DATA_DIR/pgmon_users.db

# Tells the login page and Account tab to show "sudo pgmon-reset-password"
# instead of kubectl instructions for resetting a password.
DEPLOYMENT_MODE=standalone

# Empty = served at "/" (this installer's Nginx vhost expects that). Only
# set this if you're putting pgmon behind a path-prefixing proxy of your own.
BASE_PATH=

POLL_INTERVAL_SECONDS=30
SNAPSHOT_RETENTION_HOURS=48

# Optional: enables the AI assistant / chat features.
# ANTHROPIC_API_KEY=
# ANTHROPIC_MODEL=claude-sonnet-5

# Optional: only needed to automate physical/incremental backup *restores*
# through the UI, and only if pgmon itself is running inside the same
# Kubernetes cluster as the Postgres server being restored (it shells out to
# kubectl to scale the target StatefulSet and swap its data directory). Taking
# and downloading physical/incremental backups, and all logical (pg_dump)
# backup/restore, work on a plain Linux install without any of this.
# PHYSICAL_RESTORE_NAMESPACE=
# PHYSICAL_RESTORE_STATEFULSET=
# PHYSICAL_RESTORE_PVC=
EOF
    chmod 640 "$ENV_FILE"
else
    log "Existing config found at $ENV_FILE -- leaving it as-is."
fi

chown -R "$APP_USER:$APP_GROUP" "$INSTALL_DIR" "$DATA_DIR"
chown "root:$APP_GROUP" "$CONFIG_DIR" "$ENV_FILE"
chmod 750 "$CONFIG_DIR"

# ----------------------------------------------------------------- systemd
# Deliberately 1, not scaled to nproc: app/backup.py (and migration.py,
# ora2pg_migration.py, repack.py, partitioning.py) track running jobs in a
# plain in-process dict, not a shared store -- with more than one gunicorn
# worker, whichever one didn't handle the job-creating POST 404s on every
# status poll that happens to land on it, rendering as "Backup: undefined
# (NaNh NaNm elapsed)" in the UI. Confirmed as the actual cause, not
# guessed. Single-process matches the Kubernetes deployment's own behavior
# (plain uvicorn, no multi-worker) -- fine here too since these endpoints
# are I/O-bound (async), not CPU-bound.
WORKERS=1
log "Installing systemd service (pgmon.service, $WORKERS worker(s))..."
cat > /etc/systemd/system/pgmon.service <<EOF
[Unit]
Description=pgmon - AI Postgres Monitor
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/venv/bin/gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --workers $WORKERS --bind $LISTEN_PORT --timeout 120 --access-logfile - --error-logfile -
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# ------------------------------------------------------------------- nginx
log "Configuring Nginx reverse proxy on port $HTTP_PORT..."
NGINX_SNIPPET="server {
    listen $HTTP_PORT default_server;
    listen [::]:$HTTP_PORT default_server;
    # No server_name here (RHEL-family nginx ships its own catch-all vhost
    # with server_name '_' already in nginx.conf itself -- repeating that
    # literal name here just logs a harmless but noisy 'conflicting server
    # name' warning; default_server above already makes this block the one
    # that wins regardless of Host header).

    client_max_body_size 200m;

    location /static/ {
        alias $INSTALL_DIR/app/static/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass http://$LISTEN_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
"
if [[ -d /etc/nginx/conf.d ]] && [[ ! -d /etc/nginx/sites-enabled ]]; then
    # RHEL/Fedora/SUSE-style layout: everything in conf.d is included directly.
    echo "$NGINX_SNIPPET" > /etc/nginx/conf.d/pgmon.conf
else
    # Debian/Ubuntu-style sites-available/sites-enabled layout.
    mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
    echo "$NGINX_SNIPPET" > /etc/nginx/sites-available/pgmon
    ln -sf /etc/nginx/sites-available/pgmon /etc/nginx/sites-enabled/pgmon
    # The distro's stock default site also claims port 80/default_server --
    # remove it so it doesn't fight ours (it's just the Nginx welcome page).
    rm -f /etc/nginx/sites-enabled/default
fi

nginx -t
systemctl enable --now nginx >/dev/null 2>&1 || systemctl restart nginx
systemctl reload nginx

# -------------------------------------------------------------------- start
log "Starting pgmon..."
systemctl enable --now pgmon

log "Waiting for pgmon to become healthy..."
HEALTHY=0
for _ in $(seq 1 30); do
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('http://$LISTEN_PORT/healthz', timeout=2)" >/dev/null 2>&1; then
        HEALTHY=1; break
    fi
    sleep 1
done
[[ $HEALTHY -eq 1 ]] || die "pgmon didn't become healthy in time -- check 'journalctl -u pgmon -n 50 --no-pager'."

# ---------------------------------------------------------- initial admin
# A real helper script file rather than `sudo -u ... VAR=val bash -c '...'`:
# sudo's handling of inline VAR=val assignments depends on the sudoers
# policy's env_reset/env_keep/setenv settings, which this installer can't
# assume one way or the other across distros.
CREATE_USER_HELPER="$(mktemp)"
cat > "$CREATE_USER_HELPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
set -a
source "$ENV_FILE"
set +a
exec "$INSTALL_DIR/venv/bin/python" -m app.create_user "\$@"
EOF
chmod 755 "$CREATE_USER_HELPER"   # chmod +x alone would leave it unreadable by $APP_USER (mktemp creates it 600)

ADMIN_LINE=""
if [[ $FRESH_INSTALL -eq 1 ]]; then
    ADMIN_PASSWORD="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(18))')"
    log "Creating initial admin account..."
    if sudo -u "$APP_USER" "$CREATE_USER_HELPER" admin "$ADMIN_PASSWORD" >/dev/null; then
        ADMIN_LINE="  username: admin
  password: $ADMIN_PASSWORD   (shown once -- change it after logging in)"
    else
        ADMIN_LINE="  (account creation failed -- create one manually, see below)"
    fi
fi
rm -f "$CREATE_USER_HELPER"

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

================================================================
 pgmon is installed and running.

 URL:        http://${SERVER_IP:-<this-server-IP>}/   (also reachable as http://localhost/ on this machine)
 Config:     $ENV_FILE
 Data:       $DATA_DIR
 Service:    systemctl {status|restart|stop} pgmon
 Logs:       journalctl -u pgmon -f
 Passwords:  sudo pgmon-reset-password [username]
EOF
if [[ -n "$ADMIN_LINE" ]]; then
cat <<EOF

 Admin login:
$ADMIN_LINE
EOF
else
cat <<EOF

 An accounts DB already existed at $DATA_DIR/pgmon_users.db -- no new admin
 account was created. If you've lost access, reset (or create) a password:
   sudo pgmon-reset-password [username]
EOF
fi
cat <<EOF

 This is plain HTTP. To add HTTPS, the simplest path on most distros is
 certbot's Nginx plugin against the vhost this installer created:
   sudo certbot --nginx
================================================================
EOF
