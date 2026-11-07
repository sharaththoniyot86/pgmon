#!/usr/bin/env bash
# Removes pgmon's systemd service, Nginx vhost and application code.
#
#   sudo ./uninstall.sh            # keeps /etc/pgmon and /var/lib/pgmon
#   sudo ./uninstall.sh --purge    # also deletes config and all data (SQLite
#                                   # accounts DB, encryption key) -- irreversible
set -euo pipefail

APP_USER="pgmon"
INSTALL_DIR="/opt/pgmon"
DATA_DIR="/var/lib/pgmon"
CONFIG_DIR="/etc/pgmon"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

log() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || { echo "ERROR: Run as root (sudo ./uninstall.sh)." >&2; exit 1; }

log "Stopping and disabling pgmon..."
systemctl disable --now pgmon 2>/dev/null || true
rm -f /etc/systemd/system/pgmon.service
systemctl daemon-reload

log "Removing Nginx vhost..."
rm -f /etc/nginx/conf.d/pgmon.conf /etc/nginx/sites-available/pgmon /etc/nginx/sites-enabled/pgmon
systemctl reload nginx 2>/dev/null || true

log "Removing application code ($INSTALL_DIR)..."
rm -rf "$INSTALL_DIR"
rm -f /usr/local/bin/pgmon-reset-password

if [[ $PURGE -eq 1 ]]; then
    log "Purging config and data ($CONFIG_DIR, $DATA_DIR)..."
    rm -rf "$CONFIG_DIR" "$DATA_DIR"
else
    log "Keeping $CONFIG_DIR and $DATA_DIR (re-run with --purge to remove them too)."
fi

if id "$APP_USER" >/dev/null 2>&1; then
    userdel "$APP_USER" 2>/dev/null || true
fi

log "Done."
