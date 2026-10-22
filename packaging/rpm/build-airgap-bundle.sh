#!/usr/bin/env bash
# Builds a fully offline-installable bundle: pgmon's own RPM (which already
# carries a complete pre-built Python virtualenv -- see build-rpm.sh) PLUS
# its genuine OS-level dependencies (python3.12 on RHEL 8/9 or python3 on
# RHEL 10, nginx, postgresql, sudo, openssl, and everything those pull in)
# as plain .rpm files. `dnf install ./*.rpm` on the target then resolves
# everything from those local files plus what's already on the machine --
# no repo access needed at all, verified by actually installing with the
# target machine's network namespace disabled (not just unconfigured
# repos) -- see packaging/rpm/README.md.
#
# How the dependency set is captured is the one subtle thing here: this
# runs `dnf install --downloadonly` -- a REAL install-transaction
# resolution, not just metadata lookup -- while THIS machine is still
# pristine (nothing installed beyond the base image, not even build
# tools). That's what makes it correct: run the same command on a machine
# that already has python3.12 installed (e.g. because a build prerequisite
# put it there first), and dnf silently excludes it and everything it
# needs from the download, since they're "already satisfied here" --
# confirmed the hard way, producing a bundle that failed on a real target
# with "nothing provides python3.12-libs" even though the target never had
# it either. Running this FIRST, before build-rpm.sh's own prerequisites
# touch the machine, avoids that trap entirely, and as a bonus naturally
# computes the exact right set with no hand-maintained package-name
# whitelist: confirmed on RHEL 8 that nginx's dependency chain there pulls
# in ~80 packages (the entire mod_perl stack, image-filter's font/graphics
# libraries, libxslt, ...) that no reasonably-sized whitelist could have
# kept up with reliably, where RHEL 9/10 only need a double-digit handful.
#
# Must run ON the exact distro (or a FRESH, nothing-installed-yet
# container of one) you're targeting: the OS-level dependency set is
# distro-specific (e.g. nginx on Oracle Linux pulls in oracle-logos-httpd,
# on Rocky it pulls in rocky-logos-httpd instead) -- a bundle built on one
# doesn't necessarily install cleanly on another, even within the same
# RHEL-family major-version ABI that pgmon's own RPM is portable across.
#
#   packaging/rpm/build-airgap-bundle.sh [version]
#
# (No prerequisite install needed before running this -- it installs its
# own build prerequisites itself, in the right order, for exactly the
# reason explained above.)
set -euo pipefail

VERSION="${1:-1.0.0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"

log() { echo "==> $*"; }

command -v dnf >/dev/null 2>&1 || { echo "ERROR: dnf not found -- this only runs on a dnf-based distro." >&2; exit 1; }

# Subshell, not a direct `. /etc/os-release` into this script: that file
# defines its own $VERSION (the OS's pretty version string, e.g. "9.8"),
# which would silently clobber this script's own $VERSION (the pgmon
# version being built) -- confirmed the hard way, not guessed.
DISTRO_TAG="$(. /etc/os-release && echo "${ID}${VERSION_ID%%.*}")"   # e.g. ol9, rocky9, rhel9, almalinux10
MAJOR="$(. /etc/os-release && echo "${VERSION_ID%%.*}")"

# Which Python package to ask for -- can't detect this by checking which
# binary already exists (build-rpm.sh's trick) since this machine is
# meant to still be pristine at this point, before either binary is
# necessarily installed. Confirmed directly instead: RHEL 8/9 offer
# "python3.12" as its own distinct AppStream package; RHEL 10 has no
# package by that exact name -- its system "python3" package already IS
# 3.12.
if [ "$MAJOR" -ge 10 ]; then
    PYTHON_PKG="python3"
else
    PYTHON_PKG="python3.12"
fi

STAGE="$(mktemp -d)/pgmon-$VERSION-airgap"
mkdir -p "$STAGE"
DL_DIR="$(mktemp -d)"

log "Capturing OS-level dependencies on this still-pristine machine ($DISTRO_TAG, $PYTHON_PKG)..."
dnf install -y --downloadonly --downloaddir="$DL_DIR" "$PYTHON_PKG" nginx postgresql sudo openssl

# Copy out of dnf's downloaddir into $STAGE *immediately*, before any other
# dnf command runs -- confirmed the hard way (direct repro: download bash
# via --downloadonly into a directory, then separately `dnf install` some
# unrelated package, and the first file silently vanishes) that dnf treats
# anything under a --downloaddir as part of its own cache bookkeeping and
# can purge it during a *later, unrelated* transaction, not just the one
# that downloaded it. This is exactly what ate 78 of 79 captured packages
# the first time this script ran end-to-end: the very next command here
# used to be another dnf install (build prerequisites), which silently
# wiped the previous downloaddir. $STAGE itself is never passed to dnf
# again after this point, so it's safe from that behavior.
cp "$DL_DIR"/*.rpm "$STAGE/"
rm -rf "$DL_DIR"

# One targeted safety net kept even with a pristine-machine capture:
# kernel*/systemd* are real (if indirect) dependencies of nginx and pgmon
# itself, and a pristine image's own copies can still show as "needs
# upgrading" here depending on exactly when its base image was built vs.
# the repo snapshot -- confirmed on a real Oracle Linux VM (not a
# container, which has no real protected kernel package to trip over)
# that a bundled systemd/systemd-udev can make dnf's installer decide the
# only way to reconcile them with what's already running is to remove the
# kernel -- which dnf correctly refuses (protected package), aborting the
# whole install. The target is definitionally a running Linux system, so
# it already has a perfectly good systemd and kernel; never ship a second
# copy of either.
rm -f "$STAGE"/kernel-*.rpm "$STAGE"/systemd-*.rpm
# Plain bash globbing, not `find` -- confirmed the hard way that a truly
# pristine Rocky 8 base image doesn't include findutils at all.
shopt -s nullglob
DEP_RPMS=("$STAGE"/*.rpm)
RPM_COUNT_DEPS="${#DEP_RPMS[@]}"
log "Captured $RPM_COUNT_DEPS dependency packages"

log "Installing build prerequisites and building pgmon's own RPM..."
dnf install -y -q rpm-build rpmdevtools "$PYTHON_PKG" "$PYTHON_PKG-pip"
BUILD_RPM_OUTPUT="$(bash "$SCRIPT_DIR/build-rpm.sh" "$VERSION")"
echo "$BUILD_RPM_OUTPUT"
# Parsed from build-rpm.sh's own last line, not re-globbed from $DIST_DIR:
# confirmed the hard way that dist/ can hold RPMs for more than one RHEL
# major version at once, and a glob like "pgmon-$VERSION-*.rpm" matches
# all of them, grabbing whichever one the filesystem lists first rather
# than the one this run just built.
PGMON_RPM="$(echo "$BUILD_RPM_OUTPUT" | sed -n 's/^PGMON_BUILT_RPM=//p')"
[[ -n "$PGMON_RPM" && -f "$PGMON_RPM" ]] || { echo "ERROR: couldn't determine which RPM build-rpm.sh just built" >&2; exit 1; }
cp "$PGMON_RPM" "$STAGE/"

cat > "$STAGE/install.sh" <<'EOF'
#!/usr/bin/env bash
# Installs every .rpm in this directory in one transaction -- dnf resolves
# all of pgmon's dependencies purely from these local files, no repo or
# network access needed at all. --disablerepo='*' is required for that
# last part: confirmed the hard way that dnf otherwise still tries to
# refresh configured repos' metadata even for a pure local-file install
# (and fails loudly if none are reachable), despite not actually needing
# them when every dependency is already satisfied by the named files.
#
# Want a non-default HTTPS port? Set PGMON_PORT before running this, e.g.:
#   sudo PGMON_PORT=8443 ./install.sh
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo ./install.sh)." >&2; exit 1; }
dnf install -y --disablerepo='*' ./*.rpm
EOF
chmod +x "$STAGE/install.sh"

ALL_RPMS=("$STAGE"/*.rpm)
RPM_COUNT="${#ALL_RPMS[@]}"
mkdir -p "$DIST_DIR"
OUT="$DIST_DIR/pgmon-$VERSION-airgap-$DISTRO_TAG.tar.gz"
tar -czf "$OUT" -C "$(dirname "$STAGE")" "$(basename "$STAGE")"
rm -rf "$(dirname "$STAGE")"

log "Built $OUT ($(du -sh "$OUT" | cut -f1), $RPM_COUNT rpms)"
echo
echo "On the air-gapped machine:"
echo "  tar xzf $(basename "$OUT")"
echo "  cd pgmon-$VERSION-airgap"
echo "  sudo ./install.sh"
