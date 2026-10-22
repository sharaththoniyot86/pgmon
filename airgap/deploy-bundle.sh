#!/usr/bin/env bash
set -euo pipefail

# Run this INSIDE the air gap, on a host with Docker and Helm (3.8+, for OCI
# push) that can reach Harbor -- after extracting the bundle this script
# ships inside of. It verifies everything, then loads the image and pushes
# both the image and the chart to Harbor. One command instead of the
# half-dozen you'd otherwise type by hand.
#
# Usage:
#   tar xzf pg-ai-monitor-airgap-<version>.tar.gz
#   cd pg-ai-monitor-airgap-<version>
#   ./deploy-bundle.sh
#
# Harbor host and robot-account credentials are asked for interactively so
# they never sit inside the bundle itself. To automate (e.g. from a CI
# runner on the landing host), export HARBOR_HOST / HARBOR_ROBOT_USER /
# HARBOR_ROBOT_TOKEN first and this script will use those instead of
# prompting.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

for bin in docker helm sha256sum; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found on this host." >&2; exit 1; }
done

echo "==> Verifying bundle integrity"
sha256sum -c SHA256SUMS

# shellcheck source=/dev/null
source ./bundle.env
echo "==> Bundle: ${IMAGE_REF}  +  ${CHART_FILE} (chart v${CHART_VERSION})"
echo "    built ${BUILT_AT} on ${BUILT_ON}"

HARBOR_HOST="${HARBOR_HOST:-}"
if [ -z "$HARBOR_HOST" ]; then
  read -r -p "Harbor host (e.g. harbor.internal.example): " HARBOR_HOST
fi

HARBOR_ROBOT_USER="${HARBOR_ROBOT_USER:-}"
if [ -z "$HARBOR_ROBOT_USER" ]; then
  read -r -p "Robot account username (robot\$<project>+<name>): " HARBOR_ROBOT_USER
fi

HARBOR_ROBOT_TOKEN="${HARBOR_ROBOT_TOKEN:-}"
if [ -z "$HARBOR_ROBOT_TOKEN" ]; then
  read -r -s -p "Robot account token: " HARBOR_ROBOT_TOKEN
  echo
fi

echo "==> Loading image into Docker"
docker load -i image.tar

echo "==> Logging in to Harbor (docker)"
echo "$HARBOR_ROBOT_TOKEN" | docker login "$HARBOR_HOST" -u "$HARBOR_ROBOT_USER" --password-stdin

echo "==> Pushing ${IMAGE_REF}"
docker push "$IMAGE_REF"

echo "==> Logging in to Harbor (helm OCI)"
echo "$HARBOR_ROBOT_TOKEN" | helm registry login "$HARBOR_HOST" -u "$HARBOR_ROBOT_USER" --password-stdin

# Harbor should be serving TLS (the normal case, and what containerd's own
# image pulls already assume) -- but helm's OCI client refuses a plain-HTTP
# registry outright, unlike docker, which just needs the host listed in the
# daemon's insecure-registries. Set HARBOR_PLAIN_HTTP=1 only if this Harbor
# genuinely has no TLS in front of it.
HELM_PUSH_FLAGS=()
if [ "${HARBOR_PLAIN_HTTP:-}" = "1" ]; then
  HELM_PUSH_FLAGS+=(--plain-http)
fi

echo "==> Pushing ${CHART_FILE} to ${CHART_OCI_TARGET}"
helm push "$CHART_FILE" "$CHART_OCI_TARGET" "${HELM_PUSH_FLAGS[@]}"

echo
echo "Done. In Rancher:"
echo "  - Apps > Repositories, confirm/create an OCI repo at exactly:"
echo "      ${CHART_OCI_TARGET}/${CHART_NAME}"
echo "    (the chart's own path, NOT just the project -- pointing at the project root"
echo "    makes Rancher enumerate it via the registry-wide catalog API, which a"
echo "    project-scoped robot account can't reach; re-sync if this repo already exists)"
echo "  - Install/upgrade with image.repository=${IMAGE_REF%:*}, image.tag=${IMAGE_REF##*:}"
