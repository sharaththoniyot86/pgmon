#!/usr/bin/env bash
# Builds pgmon-<version>-1.<dist>.x86_64.rpm -- a native RPM for RHEL,
# Oracle Linux, Rocky Linux, AlmaLinux 8/9/10. Must run ON one of those
# distros (or a container of one) since it needs rpm-build and a real venv
# built against that exact glibc/Python ABI -- unlike the universal
# install.sh, this doesn't cross-build from anywhere else.
#
#   sudo dnf install -y rpm-build rpmdevtools python3.12 python3.12-pip   # RHEL 8/9
#   sudo dnf install -y rpm-build rpmdevtools python3 python3-pip        # RHEL 10
#   packaging/rpm/build-rpm.sh [version]
set -euo pipefail

VERSION="${1:-1.0.0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
WHEEL_DIR="$SCRIPT_DIR/vendor-wheels"
RPMBUILD_TOP="${HOME}/rpmbuild"

log() { echo "==> $*"; }

command -v rpmbuild >/dev/null 2>&1 || { echo "ERROR: rpmbuild not found -- dnf install rpm-build rpmdevtools" >&2; exit 1; }

# Which Python to build against -- confirmed directly, not assumed, that
# this genuinely differs by RHEL major version: 8/9 offer "python3.12" as
# its own distinct AppStream package (the system python3 stays on an older
# default, 3.6/3.9), but RHEL 10 has no package by that exact name at all
# -- its system "python3" package already *is* 3.12. Prefer the versioned
# binary where it exists; otherwise fall back to python3, but only if it's
# actually 3.10+ (this app's syntax needs that) rather than silently
# building against something too old.
if command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="python3.12"
    PYTHON_PKG="python3.12"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PYTHON_BIN="python3"
    PYTHON_PKG="python3"
else
    echo "ERROR: need a python3.12 binary, or a system python3 that's already 3.10+ -- neither found." >&2
    exit 1
fi
log "Using $PYTHON_BIN ($($PYTHON_BIN --version)), package name '$PYTHON_PKG'"

log "Vendoring pip dependencies (this build needs internet; the resulting RPM won't)..."
rm -rf "$WHEEL_DIR"
mkdir -p "$WHEEL_DIR"
"$PYTHON_BIN" -m pip download --no-cache-dir -d "$WHEEL_DIR" \
    --only-binary=:all: \
    -r "$REPO_ROOT/requirements.txt" "gunicorn==23.0.0"

log "Setting up rpmbuild tree at $RPMBUILD_TOP..."
mkdir -p "$RPMBUILD_TOP"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
cp "$SCRIPT_DIR/pgmon.spec" "$RPMBUILD_TOP/SPECS/pgmon.spec"

log "Building RPM (version $VERSION)..."
export PGMON_REPO_ROOT="$REPO_ROOT"
export PGMON_WHEEL_DIR="$WHEEL_DIR"
export PGMON_VERSION="$VERSION"
export PGMON_PYTHON_BIN="$PYTHON_BIN"
export PGMON_PYTHON_PKG="$PYTHON_PKG"
rpmbuild -bb --define "_topdir $RPMBUILD_TOP" "$RPMBUILD_TOP/SPECS/pgmon.spec"

mkdir -p "$DIST_DIR"
# Plain bash globbing, not `find` -- confirmed the hard way that a truly
# pristine RHEL-family base image doesn't necessarily include findutils
# at all (build-airgap-bundle.sh calls this script right after installing
# only rpm-build/rpmdevtools/python -- nothing guarantees find came along).
shopt -s nullglob
CANDIDATE_RPMS=("$RPMBUILD_TOP"/RPMS/*/"pgmon-$VERSION"-*.rpm)
BUILT_RPM="${CANDIDATE_RPMS[0]:-}"
[[ -n "$BUILT_RPM" ]] || { echo "ERROR: build finished but no output rpm found under $RPMBUILD_TOP/RPMS" >&2; exit 1; }
cp "$BUILT_RPM" "$DIST_DIR/"

log "Built $DIST_DIR/$(basename "$BUILT_RPM")"
echo
echo "Install with:"
echo "  sudo dnf install $DIST_DIR/$(basename "$BUILT_RPM")"
# Machine-readable, last line, for build-airgap-bundle.sh to pick up --
# NOT by re-globbing $DIST_DIR afterward: confirmed the hard way that
# dist/ can (and in normal use, will) hold RPMs from more than one RHEL
# major version at once (e.g. both ...el9... and ...el10... after building
# for each), and "pgmon-$VERSION-*.rpm" matches all of them -- `head -1`
# then silently grabs whichever one the filesystem happens to list first,
# which is not necessarily the one this run just built.
echo "PGMON_BUILT_RPM=$DIST_DIR/$(basename "$BUILT_RPM")"
