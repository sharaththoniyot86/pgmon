#!/usr/bin/env bash
set -euo pipefail

# Builds the pg-ai-monitor image, packages its Helm chart, and wraps both --
# plus checksums and a self-contained loader script -- into ONE tarball to
# carry across an air-gapped boundary.
#
# Run this on a machine WITH internet access (the image build needs it: apt/
# PGDG mirrors, PyPI, dl.k8s.io, download.oracle.com, CPAN). It never touches
# the air-gapped side -- that's what the bundle's own deploy-bundle.sh is for,
# run separately once the bundle has been carried across.
#
# Usage:
#   airgap/build-bundle.sh <version> <harbor-repo-path>
#
# Example:
#   airgap/build-bundle.sh 1.0.0 harbor.internal.example/pgmonitor/pg-ai-monitor
#
# Produces (in the current directory):
#   pg-ai-monitor-airgap-<version>.tar.gz

VERSION="${1:?Usage: $0 <version> <harbor-repo-path>  e.g. $0 1.0.0 harbor.internal.example/pgmonitor/pg-ai-monitor}"
REPO_PATH="${2:?Usage: $0 <version> <harbor-repo-path>  e.g. $0 1.0.0 harbor.internal.example/pgmonitor/pg-ai-monitor}"
IMAGE_REF="${REPO_PATH}:${VERSION}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$REPO_ROOT/charts/pg-ai-monitor"
CHART_VERSION="$(awk '/^version:/{print $2}' "$CHART_DIR/Chart.yaml")"
# The OCI push target is the Harbor project, plus a "/charts" sub-path --
# deliberately NOT the same repository the image lives in. Helm's OCI push
# appends the chart's own name (from Chart.yaml) under this target, so
# pushing straight to the project would land the chart at the exact same
# repository path as the image (only the tag would differ: 1.1.0 vs 1.0.0).
# Confirmed by testing against a real Harbor + Rancher ClusterRepo: Rancher's
# OCI tag-scanner then picks up the image's tag as a second, broken "chart
# version" with no metadata, selectable in the install UI but not a real
# chart. A distinct sub-path avoids the collision entirely.
CHART_OCI_TARGET="${REPO_PATH%/*}/charts"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
BUNDLE_NAME="pg-ai-monitor-airgap-${VERSION}.tar.gz"

echo "==> Building ${IMAGE_REF}"
docker build -t "$IMAGE_REF" "$REPO_ROOT"

echo "==> Saving image to ${WORKDIR}/image.tar"
docker save "$IMAGE_REF" -o "$WORKDIR/image.tar"

echo "==> Packaging chart v${CHART_VERSION}"
helm package "$CHART_DIR" -d "$WORKDIR"
CHART_NAME="$(awk '/^name:/{print $2}' "$CHART_DIR/Chart.yaml")"
CHART_FILE="${CHART_NAME}-${CHART_VERSION}.tgz"

cat > "$WORKDIR/bundle.env" <<EOF
# Sourced by deploy-bundle.sh -- do not rename these keys.
IMAGE_REF="${IMAGE_REF}"
CHART_NAME="${CHART_NAME}"
CHART_FILE="${CHART_FILE}"
CHART_OCI_TARGET="oci://${CHART_OCI_TARGET}"
CHART_VERSION="${CHART_VERSION}"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUILT_ON="$(hostname)"
EOF

cp "$SCRIPT_DIR/deploy-bundle.sh" "$WORKDIR/deploy-bundle.sh"
chmod +x "$WORKDIR/deploy-bundle.sh"

(
  cd "$WORKDIR"
  sha256sum image.tar "$CHART_FILE" bundle.env deploy-bundle.sh > SHA256SUMS
)

cat > "$WORKDIR/MANIFEST.txt" <<EOF
pg-ai-monitor air-gapped deployment bundle
===========================================
Image:          ${IMAGE_REF}
Chart:          ${CHART_FILE} (v${CHART_VERSION})
Chart target:   oci://${CHART_OCI_TARGET}/${CHART_NAME}  (Rancher repo URL -- the chart's own path, not the project root)
Built:          $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname)

Contents:
  image.tar        docker save of the image above
  ${CHART_FILE}
  bundle.env       machine-readable values deploy-bundle.sh reads
  deploy-bundle.sh run this, inside the air gap, to load + push both
  SHA256SUMS       checksums of every file above (verified automatically
                   by deploy-bundle.sh before it loads or pushes anything)

Usage once this tarball is on the other side of the boundary:
  tar xzf ${BUNDLE_NAME}
  cd pg-ai-monitor-airgap-${VERSION}
  ./deploy-bundle.sh
EOF

echo "==> Writing ${BUNDLE_NAME}"
tar czf "$BUNDLE_NAME" -C "$WORKDIR" .
sha256sum "$BUNDLE_NAME" > "${BUNDLE_NAME}.sha256"

echo
echo "Done. Carry these two files across the boundary:"
echo "  $(pwd)/${BUNDLE_NAME}"
echo "  $(pwd)/${BUNDLE_NAME}.sha256"
