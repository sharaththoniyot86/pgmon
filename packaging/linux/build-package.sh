#!/usr/bin/env bash
# Builds the standalone Linux release tarball: a self-contained directory
# (app/, requirements.txt, packaging/install.sh, packaging/uninstall.sh) that
# install.sh turns into a running systemd + Nginx deployment on a target
# machine. Run from anywhere with internet access; output lands in dist/ at
# the repo root.
#
#   packaging/linux/build-package.sh [version] [options]
#
# Options:
#   --airgap                     Also vendor every pip dependency as a
#                                 pre-downloaded wheel (vendor/wheels/ in the
#                                 tarball), so install.sh never needs PyPI
#                                 access on the target. install.sh then also
#                                 skips OS package-repo access entirely and
#                                 just verifies Python/Nginx/Postgres client
#                                 tools are already present -- see
#                                 packaging/linux/README.md.
#   --python-version X.Y          Target CPython ABI to download wheels for
#                                 (default: 3.12 -- matches 'dnf install
#                                 python3.12' on RHEL/Rocky/Alma 9).
#   --platform TAG[,TAG...]       Target manylinux platform tag(s) (default:
#                                 manylinux_2_28_x86_64,manylinux2014_x86_64
#                                 -- covers RHEL/Rocky/Alma/Ubuntu/Debian on
#                                 x86_64; pass e.g. manylinux_2_28_aarch64
#                                 for an ARM64 target).
#
# Examples:
#   packaging/linux/build-package.sh 1.0.0
#   packaging/linux/build-package.sh 1.0.0 --airgap
#   packaging/linux/build-package.sh 1.0.0 --airgap --python-version 3.11 --platform manylinux_2_28_aarch64
set -euo pipefail

VERSION="1.0.0"
AIRGAP=0
TARGET_PY="3.12"
TARGET_PLATFORMS="manylinux_2_28_x86_64,manylinux2014_x86_64"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --airgap) AIRGAP=1; shift ;;
        --python-version) TARGET_PY="$2"; shift 2 ;;
        --platform) TARGET_PLATFORMS="$2"; shift 2 ;;
        -*) echo "Unknown option: $1" >&2; exit 1 ;;
        *) VERSION="$1"; shift ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
STAGE_NAME="pgmon-$VERSION"
STAGE_DIR="$(mktemp -d)/$STAGE_NAME"

echo "==> Staging release $VERSION..."
mkdir -p "$STAGE_DIR"
cp -r "$REPO_ROOT/app" "$STAGE_DIR/app"
find "$STAGE_DIR/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
cp "$REPO_ROOT/requirements.txt" "$STAGE_DIR/requirements.txt"
mkdir -p "$STAGE_DIR/packaging"
cp "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/uninstall.sh" "$SCRIPT_DIR/pgmon-reset-password" "$STAGE_DIR/packaging/"
chmod +x "$STAGE_DIR/packaging/install.sh" "$STAGE_DIR/packaging/uninstall.sh" "$STAGE_DIR/packaging/pgmon-reset-password"
if [[ -f "$SCRIPT_DIR/README.md" ]]; then cp "$SCRIPT_DIR/README.md" "$STAGE_DIR/README.md"; fi

if [[ $AIRGAP -eq 1 ]]; then
    echo "==> Vendoring pip dependencies for Python $TARGET_PY / $TARGET_PLATFORMS (needs internet on THIS machine only)..."
    WHEEL_DIR="$STAGE_DIR/vendor/wheels"
    mkdir -p "$WHEEL_DIR"
    PLATFORM_ARGS=()
    IFS=',' read -ra PLATS <<< "$TARGET_PLATFORMS"
    for p in "${PLATS[@]}"; do PLATFORM_ARGS+=(--platform "$p"); done
    PY_ABI="cp${TARGET_PY//./}"
    pip download --no-cache-dir -d "$WHEEL_DIR" \
        "${PLATFORM_ARGS[@]}" \
        --python-version "$TARGET_PY" --implementation cp --abi "$PY_ABI" --only-binary=:all: \
        -r "$REPO_ROOT/requirements.txt" gunicorn==23.0.0
    echo "==> Vendored $(ls "$WHEEL_DIR" | wc -l) wheel files ($(du -sh "$WHEEL_DIR" | cut -f1))"
fi

mkdir -p "$DIST_DIR"
SUFFIX=""
if [[ $AIRGAP -eq 1 ]]; then SUFFIX="-airgap"; fi
OUT="$DIST_DIR/$STAGE_NAME$SUFFIX.tar.gz"
tar -czf "$OUT" -C "$(dirname "$STAGE_DIR")" "$STAGE_NAME"
rm -rf "$(dirname "$STAGE_DIR")"

echo "==> Built $OUT"
echo
echo "To install on a target machine:"
echo "  scp $OUT user@target:/tmp/"
echo "  ssh user@target 'cd /tmp && tar xzf $(basename "$OUT") && cd $STAGE_NAME && sudo ./packaging/install.sh'"
