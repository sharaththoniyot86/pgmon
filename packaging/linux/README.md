# pgmon standalone Linux install

Installs pgmon as a systemd service behind an Nginx reverse proxy, on any
systemd-based Linux distro (Debian/Ubuntu, RHEL/Fedora/Rocky/Alma,
openSUSE) -- no Docker or Kubernetes required.

## Install

```bash
tar xzf pgmon-1.0.0.tar.gz
cd pgmon-1.0.0
sudo ./packaging/install.sh
```

This:
- installs Python 3, Nginx and Postgres client tools via the distro's own
  package manager (unless this is an air-gapped bundle -- see below)
- creates a dedicated `pgmon` system user, with the app under `/opt/pgmon`
  running in its own virtualenv
- generates `/etc/pgmon/pgmon.env` (session secret, DB paths, etc.) on first
  install only
- registers and starts `pgmon.service` (gunicorn + Uvicorn workers,
  `127.0.0.1:8000`) and an Nginx vhost that proxies port 80 to it
- creates a one-time initial `admin` account and prints its password

Re-running `install.sh` (e.g. after unpacking a newer release tarball over
the same checkout) upgrades the app code in place without touching your
config or data.

## Where things live

| Path | Contents |
|---|---|
| `/opt/pgmon` | application code + virtualenv (replaced on upgrade) |
| `/etc/pgmon/pgmon.env` | configuration (session secret, feature toggles) |
| `/var/lib/pgmon` | the SQLite accounts/settings DB -- back this up |

```bash
systemctl status pgmon      # or restart / stop
journalctl -u pgmon -f      # logs
```

## Resetting a password

```bash
sudo pgmon-reset-password            # resets 'admin', prompts for the new password
sudo pgmon-reset-password alice      # resets (or creates, if missing) a specific account
echo 'newpassword' | sudo pgmon-reset-password admin   # non-interactive
```

Installed to `/usr/local/bin/pgmon-reset-password` by `install.sh`. If the
named account doesn't exist, it's created fresh as an admin instead of
failing -- so this also covers losing every account, not just forgetting
one password. There's no `kubectl exec` to fall back on here, which is why
this exists as its own command rather than only the raw `python -m
app.create_user` CLI the Kubernetes deployment documents.

## HTTPS

The installer sets up plain HTTP. The simplest way to add TLS on top of the
Nginx vhost it created is certbot:

```bash
sudo certbot --nginx
```

## Known limitations of a plain Linux install

- **Oracle→Postgres full-schema migration** (the `ora2pg`-based path) needs
  `ora2pg` and the Oracle Instant Client installed separately -- the app
  already detects their absence and greys out that one feature rather than
  failing; everything else is unaffected.
- **Automated restore of physical/incremental backups through the UI**
  needs pgmon to be running inside the same Kubernetes cluster as the
  Postgres server (it drives `kubectl` to scale a StatefulSet and swap its
  data directory) -- set the `PHYSICAL_RESTORE_*` variables in
  `pgmon.env` if that applies to you. Taking and downloading
  physical/incremental backups, and all logical (`pg_dump`/`pg_restore`)
  backup and restore, work the same as anywhere else.
- `postgresql-client`/`postgresql` installed by this script is whatever
  version your distro ships; `pg_dump`/`pg_restore` refuse to talk to a
  *newer* Postgres server than themselves. If you're monitoring a newer
  Postgres than your distro's client tools support, install a newer client
  from PGDG (https://www.postgresql.org/download/) alongside the distro's.

## Uninstall

```bash
sudo ./packaging/uninstall.sh            # keeps config + data
sudo ./packaging/uninstall.sh --purge    # also deletes them -- irreversible
```

## Building the release tarball

From a checkout of the repo, on a machine with internet access:

```bash
packaging/linux/build-package.sh 1.0.0
# -> dist/pgmon-1.0.0.tar.gz
```

## Air-gapped / offline install

Add `--airgap` to also download and bundle every pip dependency as a wheel
file (`vendor/wheels/` in the tarball, ~20-30MB):

```bash
packaging/linux/build-package.sh 1.0.0 --airgap
# -> dist/pgmon-1.0.0-airgap.tar.gz
```

By default this targets Python 3.12 on x86_64 (RHEL/Rocky/Alma 9's
`python3.12` AppStream package, and Ubuntu/Debian's stock `python3` on
recent releases). For a different target, e.g. ARM64 or Python 3.11:

```bash
packaging/linux/build-package.sh 1.0.0 --airgap --python-version 3.11 --platform manylinux_2_28_aarch64
```

`install.sh` auto-detects `vendor/wheels/` and switches behavior
accordingly: it installs purely from those local wheels (`pip install
--no-index`, no PyPI reachable needed) and skips OS package-repo access
(`apt-get`/`dnf`/...) entirely, instead just verifying that Python 3.10+,
Nginx and `psql`/`pg_dump` are already present on the target -- i.e. **the
target machine still needs those three already installed** (from your
org's internal mirror, installation media, or a pre-baked golden image;
only the pip/PyPI layer is vendored by this bundle). If any are missing it
fails immediately with exactly which ones, rather than attempting -- and
hanging on -- a network call that an air-gapped box can't complete.

Verified end-to-end: built this way and installed on a Rocky Linux 9
container running with `--network none` (literally no network namespace),
with only `python3.12`/`nginx`/`postgresql` pre-staged -- full install,
login, and uninstall all succeeded with zero network access at install
time.
