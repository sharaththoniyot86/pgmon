# pgmon native RPM (RHEL / Oracle Linux / Rocky / AlmaLinux 8, 9, 10)

A native `dnf`/`rpm`-managed install -- bundles a full Python virtualenv
(every dependency pre-installed from wheels at build time, so `dnf install`
needs no PyPI access) as part of the RPM payload, with `dnf` pulling in
Python, `nginx`, and `postgresql` automatically via normal package
`Requires`. Which Python package that means is detected at build time, not
hardcoded: RHEL 8/9 offer `python3.12` as its own distinct AppStream
package, but RHEL 10 has no package by that exact name -- its system
`python3` package already *is* 3.12.

Verified by actually building and installing it (not just authoring the
spec) on every supported major version:

- **RHEL 8 family**: built and installed on Rocky Linux 8. By far the
  biggest OS-level dependency footprint of the three -- RHEL 8's default
  `nginx` stream is the old 1.14.1 (2018-era) and ships as
  `nginx-all-modules`, which hard-requires the entire `mod_perl` stack,
  `mod_http_xslt_filter` (`libxslt`), and `mod_http_image_filter` (a whole
  font/graphics stack: `freetype`, `fontconfig`, `libX11`, ...) -- ~80
  packages total, vs. a double-digit handful on 9/10. Also needed a
  RHEL-8-specific fix: the stock `/etc/nginx/nginx.conf` ships its own
  `listen 80 default_server;` block, which on 8 is a **hard** "duplicate
  default_server" error (9/10 only log a harmless warning for the
  equivalent conflict) -- `%post` now strips just the `default_server`
  flag from nginx's own stock config on first install, never touching
  pgmon's own vhost.
- **RHEL 9 family**: built inside an Oracle Linux 9 container, then
  installed fresh on both **Oracle Linux 9** and **Rocky Linux 9** (a free
  RHEL-9-compatible stand-in -- real RHEL images need a subscription) --
  including across two different Python 3.12 patch versions (3.12.15 vs
  3.12.13), proving the bundled compiled extensions (asyncpg, cryptography,
  bcrypt, etc.) are portable across the whole RHEL-family ABI, not just the
  exact build machine. Also verified: password reset, a real version
  upgrade (1.0.0 -> 1.0.1, config/admin preserved), and `dnf remove`
  (service stopped, code removed, config/data left in place).
- **RHEL 10 family**: built and installed on AlmaLinux 10. Needed real
  fixes along the way, not just a recompile -- see "Known limitations"
  below for the air-gap-bundle-specific ones. One applies to **every**
  version, found here but just as real on 8/9: the initial admin account
  used to be created via `sudo -u pgmon` from inside the RPM's `%post`
  scriptlet, which can fail with `PAM account management error` on a
  freshly-installed system (confirmed on a from-scratch AlmaLinux 10
  install) -- `%post` already runs as root, so it now creates the account
  directly as root and `chown`s the result, sidestepping `sudo`'s PAM
  requirements entirely rather than fighting them.

## Choosing the HTTPS port

Defaults to 443. To use a different port, set `PGMON_PORT` when installing
(first install only -- it has no effect on an upgrade):

```bash
sudo PGMON_PORT=8443 dnf install ./pgmon-1.0.0-1.el9.x86_64.rpm
```

Confirmed `dnf` passes environment variables through to the RPM's `%post`
scriptlet correctly, both as root directly and via `sudo` from a non-root
user with passwordless sudo rights. `%post` validates the value (falls
back to 443 with a warning if it isn't a plain number in the 1-65535
range), substitutes it into both the Nginx vhost (`listen <port> ssl
default_server;`) and the plain-HTTP-to-HTTPS redirect (so the redirect
names the real port explicitly, not just `https://host/`), opens it in
`firewalld` if running (either the `https` service definition when the
port is 443, or an explicit `--add-port=<port>/tcp` otherwise), and prints
it in the final install banner (`https://<host>:<port>/`).

## Installing

```bash
sudo dnf install ./pgmon-1.0.0-1.el9.x86_64.rpm
```

`dnf` resolves and installs `python3.12`, `nginx`, `postgresql`, `sudo`,
and `openssl` automatically if they aren't already present. This:

- installs to `/opt/pgmon/` (app code + a pre-built virtualenv)
- registers `pgmon.service` (gunicorn + Uvicorn workers, `127.0.0.1:8000`)
  and an Nginx vhost (`/etc/nginx/conf.d/pgmon.conf`) in front of it
- on first install only: generates a **self-signed TLS certificate**
  (`/etc/pki/tls/certs/pgmon.crt` + `.../private/pgmon.key`, 10-year
  validity) and configures Nginx to serve HTTPS on 443, redirecting plain
  HTTP (port 80) to it; sets the `httpd_can_network_connect` SELinux
  boolean if SELinux is enforcing (without it, Nginx can't even connect to
  its own proxy backend on `127.0.0.1` -- every request fails with a
  generic `502 Bad Gateway` that gives no hint it's SELinux rather than
  pgmon itself being down; confirmed on a real enforcing VM, a failure
  mode no container test can catch since containers never enforce
  SELinux); opens both ports in `firewalld` if it's running; generates
  `/etc/pgmon/pgmon.env` (random session secret); and creates an initial
  `admin` account, **printing its password directly in the `dnf install`
  output** -- copy it before closing that terminal

Browse to `https://<this-machine>/`. Your browser will warn about the
certificate being self-signed/untrusted -- that's expected, not an error;
click through the warning (or add a trusted exception), or replace the
two files above with a real CA-issued certificate later if you get one.

## Where things live

| Path | Contents |
|---|---|
| `/opt/pgmon/` | application code + virtualenv (replaced on upgrade) |
| `/etc/pgmon/pgmon.env` | configuration (session secret, feature toggles) |
| `/var/lib/pgmon/pgmon_users.db` | the accounts/settings SQLite DB -- back this up |
| `/etc/pki/tls/certs/pgmon.crt`, `.../private/pgmon.key` | the self-signed TLS cert/key (replace with a real one anytime) |

```bash
systemctl status pgmon      # or restart / stop
journalctl -u pgmon -f      # logs
```

## Resetting a password

```bash
sudo pgmon-reset-password            # resets 'admin', prompts for the new password
sudo pgmon-reset-password alice      # resets (or creates) a specific account
echo 'newpassword' | sudo pgmon-reset-password admin   # non-interactive
```

Same tool as the universal Linux installer (`packaging/linux/`) -- if the
named account doesn't exist, it's created fresh as an admin instead of
failing.

## Upgrading

```bash
sudo dnf upgrade ./pgmon-1.0.1-1.el9.x86_64.rpm
```

Config and the accounts DB are untouched; only `/opt/pgmon/` (code +
virtualenv) is replaced, and the service is restarted.

## Uninstalling

```bash
sudo dnf remove pgmon
```

Stops and disables the service, removes `/opt/pgmon/`. `/etc/pgmon/` and
`/var/lib/pgmon/` are left behind (same default as every other pgmon
installer) -- `rm -rf /etc/pgmon /var/lib/pgmon` yourself for a fully clean
wipe. Note `dnf` will also offer to remove `nginx`/`python3.12`/
`postgresql` if nothing else on the machine depends on them -- decline that
if you want to keep them for other uses (`dnf remove pgmon --no-autoremove` on newer dnf, or just confirm the list it shows you).

## Known limitations

Same two caveats as the universal Linux installer, for the same reasons --
see [packaging/linux/README.md](../linux/README.md#known-limitations-of-a-plain-linux-install):
the `ora2pg`-based Oracle migration path and automated Kubernetes-only
physical-backup restore both aren't set up by this package, but the app
already degrades those two features gracefully rather than failing.

## Building the RPM

Must run **on** one of these distros (or a container of one) -- it builds
a real virtualenv against that machine's own glibc/Python ABI, no
cross-compilation. The only prerequisite is `rpmbuild` itself
(`rpm-build rpmdevtools`) -- the script auto-detects and installs nothing
else:

```bash
sudo dnf install -y rpm-build rpmdevtools   # python3.12 or python3 is auto-detected, not required up front
packaging/rpm/build-rpm.sh 1.0.0
# -> dist/pgmon-1.0.0-1.el9.x86_64.rpm (or el8/el10, matching the build machine)
```

(Needs internet on the *building* machine to download wheels; the
resulting RPM needs none on install -- but `dnf install ./pgmon....rpm`
will still try to reach your configured repos to fetch Python, `nginx`,
`postgresql`, and `sudo` unless they're already present. For a machine
with **no repo access at all**, see the air-gap bundle below.)

## Air-gapped install (zero network access on the target, at all)

The plain RPM above still needs `dnf` to reach a repo (yours, or an
internal mirror) for its OS-level dependencies (`python3.12`/`python3`,
`nginx`, `postgresql`, `sudo`, `openssl`). For a target with no repo
access whatsoever -- confirmed by actually testing with the target's
network namespace disabled entirely, not just unconfigured repos -- build
the full bundle instead, **on a genuinely fresh machine or container with
nothing pre-installed** (see "How the dependency set is captured" below
for why that precondition matters):

```bash
packaging/rpm/build-airgap-bundle.sh 1.0.0
# -> dist/pgmon-1.0.0-airgap-rocky8.tar.gz       (RHEL 8 family)
# -> dist/pgmon-1.0.0-airgap-ol9.tar.gz          (RHEL 9 family)
# -> dist/pgmon-1.0.0-airgap-almalinux10.tar.gz  (RHEL 10 family -- name reflects the distro you built on)
```

No prerequisite install needed first -- the script installs its own build
tools itself, in the right order, deliberately *after* it captures the
dependency set (see below). It downloads pgmon's genuine OS-level
dependency closure as plain `.rpm` files, bundled alongside pgmon's own
RPM in one tarball. **How many differs sharply by major version**: RHEL 9
~16-28 packages depending on what the specific minimal base image already
has; RHEL 10 ~27 packages (its minimal image is missing `openssl`, `pam`,
`openldap`, unlike RHEL 9's); **RHEL 8 ~80 packages, ~47MB** -- its default
`nginx` stream pulls in the entire `mod_perl`/`libxslt`/font-and-graphics
stack (see above). On the air-gapped machine:

```bash
tar xzf pgmon-1.0.0-airgap-rocky8.tar.gz
cd pgmon-1.0.0-airgap
sudo ./install.sh
# or, for a non-default HTTPS port:
sudo PGMON_PORT=8443 ./install.sh
```

`install.sh` runs `dnf install -y --disablerepo='*' ./*.rpm` -- dnf
resolves everything from those local files alone, installing only what's
actually missing (most of the bundled packages are already present on any
real machine and get silently skipped; only genuinely new ones like
`nginx`/`postgresql` actually install). Verified end-to-end on a RHEL 8,
9, and 10 machine each, with the network namespace fully disabled
(`--network none`, not just missing repo config): install (all bundled
packages resolve purely from local files), service start, HTTPS login
(including on a custom `PGMON_PORT`), and `pgmon-reset-password` all
confirmed working with zero network access at any point.

**Must build on the exact distro you're targeting** (or a container of
it): the OS-level dependency set is distro-specific -- e.g. nginx on
Oracle Linux pulls in `oracle-logos-httpd`, on Rocky it pulls in
`rocky-logos-httpd` instead. A bundle built on Oracle Linux 9 won't
necessarily install cleanly on Rocky 9, even though pgmon's own RPM is
portable across that whole family. Build separately for each target
distro you need to support.

### How the dependency set is captured

The script runs `dnf install --downloadonly` -- a **real install-transaction
resolution**, not just a metadata lookup -- while the build machine is
still pristine (nothing installed beyond the base image, not even build
tools). That's the whole trick, and it's load-bearing: run the same
command on a machine that already has e.g. `python3.12` installed (because
something else put it there first), and dnf silently excludes it and
everything it alone needs from the download, since they're "already
satisfied here" -- confirmed the hard way, producing a bundle that failed
on a real target with "nothing provides python3.12-libs" even though the
target never had it either. Doing the capture first, before the script's
own build prerequisites (`rpm-build`, `rpmdevtools`, the Python `-pip`
package) touch the machine, avoids that trap -- and as a bonus computes
the exact right set with no hand-maintained package-name whitelist
(earlier versions of this script tried to maintain one by hand; it became
unmaintainable once RHEL 8's ~80-package nginx closure showed up).

One subtlety that cost a full debugging cycle to find: **`dnf`'s
`--downloadonly --downloaddir=X` doesn't give you a safely independent
copy of the downloaded files.** It treats anything under a `--downloaddir`
as part of its own cache bookkeeping, and a *later, unrelated* `dnf
install` (such as the script's own next step, installing `rpm-build`)
can silently delete files out of that directory -- confirmed by direct
repro: download `bash` via `--downloadonly` into a throwaway directory,
then separately `dnf install` some unrelated package, and the first file
vanishes with no warning. This is exactly what happened the first time
the RHEL 8 rebuild ran end-to-end: it captured all 79 dependency packages
correctly (logged as "Captured 79 dependency packages"), but by the time
the final tarball was built, 78 of them had been silently wiped by the
very next `dnf install` (the build-prerequisites step), leaving a
13MB/1-rpm bundle that looked like it had built successfully (no error,
no non-zero exit) but would have failed on every real target. Fixed by
copying the captured RPMs out of dnf's downloaddir into a plain directory
*immediately* after the capture step, before any other `dnf` command runs
-- that plain directory is never passed to `dnf` again, so nothing can
reach in and delete from it afterward. Re-verified afterward: the RHEL 8
rebuild now correctly produces 80 packages (79 deps + pgmon's own RPM),
and a fresh zero-network install from that bundle works end to end.

One targeted safety net is still kept even with the pristine-machine
capture: `kernel*`/`systemd*` packages are stripped from the captured set
unconditionally. They're real (if indirect) dependencies of nginx and
pgmon itself, and a pristine image's own copies can still show as
"needs upgrading" in the capture depending on exactly when its base image
was built vs. the repo snapshot -- confirmed on a real Oracle Linux VM
(not a container, which has no real protected kernel package to trip
over) that a bundled `systemd`/`systemd-udev` can make dnf's installer
decide the only way to reconcile them with what's already running is to
remove the kernel -- which dnf correctly refuses (protected package),
aborting the whole install. The target is definitionally a running Linux
system, so it already has a perfectly good systemd and kernel; never ship
a second copy of either.

While testing RHEL 10, also found (and fixed, for every RHEL version, not
just 10) that the RPM's own `%post` scriptlet created the initial admin
account via `sudo -u pgmon`, which can fail with `PAM account management
error: Permission denied` on a freshly-installed system -- apparently
`authselect` needs to be "selected" to fully generate sudo's PAM stack,
which isn't guaranteed mid-package-transaction. Since `%post` already runs
as root, it now creates the account directly as root and `chown`s the
result afterward, sidestepping `sudo`'s PAM requirements entirely instead
of fighting them.
