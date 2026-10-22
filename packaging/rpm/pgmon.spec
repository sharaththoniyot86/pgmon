%define repo_root %{getenv:PGMON_REPO_ROOT}
%define wheel_dir %{getenv:PGMON_WHEEL_DIR}
# python_bin/python_pkg: which Python to build/require. Set by build-rpm.sh
# (never hardcoded here) since the right answer differs by RHEL major
# version -- confirmed directly, not assumed: RHEL 8/9 offer "python3.12"
# as its own distinct, optional AppStream package (the system python3
# stays on an older default), while RHEL 10 has no package by that name at
# all -- its system "python3" package already *is* 3.12.
%define python_bin %{getenv:PGMON_PYTHON_BIN}
%define python_pkg %{getenv:PGMON_PYTHON_PKG}

Name:           pgmon
Version:        %{getenv:PGMON_VERSION}
Release:        1%{?dist}
Summary:        AI-powered PostgreSQL monitoring dashboard
License:        Proprietary
URL:            https://example.invalid/pgmon
BuildArch:      x86_64

Requires:       %{python_pkg}
Requires:       nginx
Requires:       postgresql
# pgmon-reset-password itself needs sudo (to run create_user as the pgmon
# user) -- almost always already present on a real RHEL/OL/Rocky install,
# but a bare minimal image genuinely doesn't ship it, confirmed by actually
# testing on one. Keep this list in sync with build-airgap-bundle.sh's own
# explicit package list.
Requires:       sudo
Requires:       openssl
Requires(pre):  shadow-utils
%{?systemd_requires}
BuildRequires:  systemd-rpm-macros
BuildRequires:  %{python_pkg}
BuildRequires:  %{python_pkg}-pip

%description
A self-hosted PostgreSQL monitoring, administration, and tuning dashboard.
This package installs it as a systemd service behind an Nginx reverse
proxy, with its own Python virtualenv under /opt/pgmon -- built from
dependency wheels bundled at package-build time, so `dnf install` needs no
PyPI access on the target machine.

%install
rm -rf %{buildroot}
set -e

# ---------------------------------------------------------------- app code
mkdir -p %{buildroot}/opt/pgmon
cp -r %{repo_root}/app %{buildroot}/opt/pgmon/app
find %{buildroot}/opt/pgmon/app -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
cp %{repo_root}/requirements.txt %{buildroot}/opt/pgmon/requirements.txt

# --------------------------------------------------- virtualenv, offline
%{python_bin} -m venv %{buildroot}/opt/pgmon/venv
%{buildroot}/opt/pgmon/venv/bin/pip install --no-cache-dir --no-index --find-links %{wheel_dir} \
    -r %{repo_root}/requirements.txt "gunicorn==23.0.0"
find %{buildroot}/opt/pgmon/venv -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# A venv bakes its own creation-time absolute path into pyvenv.cfg, every
# console-script shebang under venv/bin, and the activate* scripts -- all
# of which still say %{buildroot} right now since that's where this venv
# was actually created. rpmbuild's own check-buildroot refuses to package
# that (and it would be wrong anyway: %{buildroot} won't exist on the
# installed machine, only /opt/pgmon/venv will), so rewrite every text file
# under the venv before %files gets to it.
grep -rlI -- "%{buildroot}" %{buildroot}/opt/pgmon/venv | xargs -r sed -i "s#%{buildroot}##g"

# ------------------------------------------------------------------ extras
mkdir -p %{buildroot}%{_bindir}
install -m 755 %{repo_root}/packaging/linux/pgmon-reset-password %{buildroot}%{_bindir}/pgmon-reset-password

mkdir -p %{buildroot}%{_unitdir}
install -m 644 %{repo_root}/packaging/rpm/pgmon.service %{buildroot}%{_unitdir}/pgmon.service

mkdir -p %{buildroot}%{_sysconfdir}/nginx/conf.d
install -m 644 %{repo_root}/packaging/rpm/pgmon-nginx.conf %{buildroot}%{_sysconfdir}/nginx/conf.d/pgmon.conf

%pre
getent group pgmon >/dev/null || groupadd -r pgmon
getent passwd pgmon >/dev/null || useradd -r -g pgmon -d /opt/pgmon -s /sbin/nologin -c "pgmon service account" pgmon
exit 0

%post
%systemd_post pgmon.service
CONFIG_DIR=/etc/pgmon
DATA_DIR=/var/lib/pgmon
ENV_FILE="$CONFIG_DIR/pgmon.env"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"
chown root:pgmon "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
chown pgmon:pgmon "$DATA_DIR"

if [ "$1" -eq 1 ]; then
    # Fresh install only -- an upgrade ($1 > 1) must never touch an
    # existing config/accounts DB. Same reasoning as install.sh's
    # FRESH_INSTALL guard.

    # Desired HTTPS port, e.g.: sudo PGMON_PORT=8443 dnf install ./pgmon....rpm
    # Confirmed directly (not assumed) that dnf passes a command-line
    # VAR=val through to this scriptlet's environment, both invoked as
    # root directly and via `sudo VAR=val dnf install ...` as a regular
    # user -- both work the same way on this sudoers/dnf configuration.
    # Falls back to 443 if unset, or if set to something that isn't a
    # plausible TCP port (never trust an env var's contents enough to
    # drop it straight into a sed replacement otherwise).
    PORT="${PGMON_PORT:-443}"
    case "$PORT" in
        ''|*[!0-9]*) echo "WARNING: PGMON_PORT='$PORT' isn't a number -- using 443 instead." >&2; PORT=443 ;;
    esac
    if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
        echo "WARNING: PGMON_PORT=$PORT is out of range -- using 443 instead." >&2
        PORT=443
    fi

    SESSION_SECRET="$(/opt/pgmon/venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
    cat > "$ENV_FILE" <<EOF
# pgmon runtime configuration. After editing: systemctl restart pgmon
SESSION_SECRET=$SESSION_SECRET
USERS_DB_PATH=$DATA_DIR/pgmon_users.db
BASE_PATH=
DEPLOYMENT_MODE=standalone
POLL_INTERVAL_SECONDS=30
SNAPSHOT_RETENTION_HOURS=48
# ANTHROPIC_API_KEY=
# ANTHROPIC_MODEL=claude-sonnet-5
EOF
    chmod 640 "$ENV_FILE"
    chown root:pgmon "$ENV_FILE"

    systemctl start pgmon.service || true

    ADMIN_PASSWORD="$(/opt/pgmon/venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"
    HELPER="$(mktemp)"
    cat > "$HELPER" <<HELPEREOF
#!/usr/bin/env bash
set -euo pipefail
cd /opt/pgmon
set -a
source "$ENV_FILE"
set +a
exec /opt/pgmon/venv/bin/python -m app.create_user "\$@"
HELPEREOF
    chmod 755 "$HELPER"
    # Run directly as root (%post already IS root), not `sudo -u pgmon` --
    # confirmed the hard way that sudo can fail here with "PAM account
    # management error: Permission denied" on a freshly-installed RHEL 10
    # system, apparently because authselect hasn't been "selected" yet to
    # fully generate sudo's PAM stack mid-transaction. Since nothing else
    # at this point needs to actually impersonate the pgmon user (unlike
    # pgmon-reset-password, which genuinely does need to drop privileges
    # FROM an interactive admin TO pgmon), running as root and fixing file
    # ownership afterward sidesteps the whole sudo/runuser/su-via-PAM
    # question entirely.
    HELPER_OUT="$("$HELPER" admin "$ADMIN_PASSWORD" 2>&1)" && HELPER_OK=1 || HELPER_OK=0
    # Recursive, not just the one file: SQLite's WAL mode (which userdb.py
    # uses) creates -wal/-shm sidecar files alongside it that the running
    # service (as pgmon) needs to write to as well.
    chown -R pgmon:pgmon "$DATA_DIR" 2>/dev/null || true
    if [ "$HELPER_OK" -eq 1 ]; then
        echo ""
        echo "================================================================"
        echo " pgmon admin account created:"
        echo "   username: admin"
        echo "   password: $ADMIN_PASSWORD   (shown once -- change it after logging in)"
        echo "================================================================"
    else
        # Surfaced rather than silently discarded: an install-time failure
        # here with no visible reason is exactly what cost real debugging
        # time once already -- better to show whatever it was (and the
        # package install still succeeds either way; `sudo
        # pgmon-reset-password admin` always remains the fallback).
        echo ""
        echo "WARNING: couldn't create the initial admin account automatically:"
        echo "$HELPER_OUT" | sed 's/^/  /'
        echo "Create one yourself with: sudo pgmon-reset-password admin"
    fi
    rm -f "$HELPER"

    # Self-signed TLS cert, generated once -- RHEL-native paths
    # (/etc/pki/tls/{certs,private}), already correctly SELinux-labeled for
    # nginx to read on an enforcing system (see pgmon-nginx.conf for why
    # this doesn't use a custom /etc/pgmon/ path instead). Guarded on the
    # files not already existing so a later reinstall never clobbers a
    # cert you've since replaced with a real CA-signed one.
    CERT_FILE=/etc/pki/tls/certs/pgmon.crt
    KEY_FILE=/etc/pki/tls/private/pgmon.key
    if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
        # Not the external `hostname` command -- confirmed the hard way
        # that it isn't guaranteed present (a minimal install can lack the
        # standalone `hostname` package entirely); Python is, since it's
        # literally what this RPM installs.
        CN="$(/opt/pgmon/venv/bin/python -c 'import socket; print(socket.getfqdn())')"
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

    # On an SELinux-enforcing machine (the default on a real RHEL/OL/Rocky
    # install -- confirmed on a real VM, not caught by any container test,
    # since containers never enforce SELinux at all), nginx is blocked by
    # policy from even making an outbound connection to its own proxy_pass
    # backend on 127.0.0.1 unless this boolean is set -- every request
    # fails with a generic "502 Bad Gateway" that gives no hint it's an
    # SELinux denial rather than pgmon itself being down. -P persists it
    # across reboots; harmless/no-op where SELinux is permissive/disabled.
    if command -v setsebool >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = "Enforcing" ]; then
        setsebool -P httpd_can_network_connect 1 || true
    fi

    # Substitute the HTTPS port into the packaged nginx vhost -- only here
    # on fresh install, same reasoning as the cert/env-file guards above:
    # %config(noreplace) already protects this file on upgrade, so if an
    # admin has since hand-edited it (e.g. to change the port after the
    # fact), a later `dnf reinstall`/upgrade must never overwrite that.
    NGINX_CONF=/etc/nginx/conf.d/pgmon.conf
    [ -f "$NGINX_CONF" ] && sed -i "s/__PGMON_HTTPS_PORT__/$PORT/g" "$NGINX_CONF"

    # RHEL 8's stock nginx.conf embeds its own catch-all server block for
    # port 80 marked `default_server` (RHEL 9/10's doesn't go that far --
    # same idea, but without the explicit flag, so it only ever produced
    # the harmless "conflicting server name" warning seen above, not
    # this). Two blocks both claiming default_server for the same address
    # is a hard nginx config error ("duplicate default_server"), not a
    # warning -- confirmed on a real RHEL 8 install, where it kept nginx
    # from starting at all. Strip the flag from the STOCK file only
    # (never touch our own conf.d vhost, which needs to keep it) so ours
    # remains the sole default for that port; a no-op everywhere this
    # exact conflict doesn't exist.
    if [ -f /etc/nginx/nginx.conf ]; then
        sed -i '/^[[:space:]]*listen/ s/[[:space:]]\+default_server;/;/' /etc/nginx/nginx.conf
    fi

    if command -v nginx >/dev/null 2>&1; then
        systemctl enable --now nginx >/dev/null 2>&1 || systemctl restart nginx || true
        nginx -t 2>/dev/null && systemctl reload nginx || true
    fi

    if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
        firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true
        if [ "$PORT" -eq 443 ]; then
            firewall-cmd --permanent --add-service=https >/dev/null 2>&1 || true
        else
            firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null 2>&1 || true
        fi
        firewall-cmd --reload >/dev/null 2>&1 || true
    fi

    SERVER_HOST="$(/opt/pgmon/venv/bin/python -c 'import socket; print(socket.getfqdn())')"
    if [ "$PORT" -eq 443 ]; then
        PGMON_URL="https://$SERVER_HOST/"
    else
        PGMON_URL="https://$SERVER_HOST:$PORT/"
    fi
    echo ""
    echo "================================================================"
    echo " pgmon is available at: $PGMON_URL"
    echo " (self-signed certificate -- your browser will warn about this"
    echo " once; that's expected, not an error. Add a trusted exception,"
    echo " or replace $CERT_FILE / $KEY_FILE with a real certificate.)"
    echo "================================================================"
else
    systemctl daemon-reload || true
    systemctl restart pgmon.service || true
fi
exit 0

%preun
%systemd_preun pgmon.service

%postun
%systemd_postun_with_restart pgmon.service
if [ "$1" -eq 0 ]; then
    # True removal, not an upgrade -- config/data under /etc/pgmon and
    # /var/lib/pgmon are deliberately left in place (same default as the
    # universal installer's uninstall.sh): rpm erase shouldn't silently
    # destroy the accounts DB or encryption key. Remove them yourself with
    # `rm -rf /etc/pgmon /var/lib/pgmon` if you really want a clean wipe.
    true
fi
exit 0

%files
%dir /opt/pgmon
/opt/pgmon/app
/opt/pgmon/requirements.txt
/opt/pgmon/venv
%{_bindir}/pgmon-reset-password
%{_unitdir}/pgmon.service
%config(noreplace) %{_sysconfdir}/nginx/conf.d/pgmon.conf

%changelog
* Thu Oct 08 2026 pgmon <noreply@example.invalid> - %{version}-1
- See the project repository for release notes.
