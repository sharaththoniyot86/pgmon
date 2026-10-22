#!/usr/bin/env bash
# Builds the self-contained, standalone pgmon Docker image and exports it
# as a portable tarball -- `docker load` on any machine with Docker (or
# Podman) needs no registry access at all, matching the RPM air-gap
# bundle's zero-network install story for the container path.
#
#   packaging/docker/build-image.sh [version]
set -euo pipefail

VERSION="${1:-1.0.0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
IMAGE="pgmon:$VERSION"

log() { echo "==> $*"; }

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found." >&2; exit 1; }

log "Building $IMAGE..."
docker build -t "$IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$REPO_ROOT"

mkdir -p "$DIST_DIR"
OUT="$DIST_DIR/pgmon-$VERSION-docker.tar.gz"
log "Exporting to $OUT..."
docker save "$IMAGE" | gzip > "$OUT"

log "Built $OUT ($(du -sh "$OUT" | cut -f1))"
echo
echo "On the target machine:"
echo "  docker load -i $(basename "$OUT")"
echo "  docker run -d --name pgmon -p 443:443 \\"
echo "    -v pgmon-config:/etc/pgmon -v pgmon-data:/var/lib/pgmon \\"
echo "    --restart unless-stopped $IMAGE"
