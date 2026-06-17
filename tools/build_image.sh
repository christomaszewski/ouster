#!/usr/bin/env bash
# rig's BUILD-phase entrypoint (rigging.yaml `build.command`): build the self-contained runtime
# image and push it to the fleet registry. rig invokes it as
#   tools/build_image.sh <registry> [tag]        (cwd = repo root, tag defaults to latest)
# and the deploy compose pulls the result as ${RIG_IMAGE_REGISTRY}/ouster_driver:${RIG_IMAGE_TAG}.
# Works standalone too:  tools/build_image.sh 192.168.1.10:5000 mytag
#
# A plain-HTTP local registry needs the daemon's insecure-registries on this host (with the :port).
# Cross-arch vehicle: export OUSTER_BUILD_PLATFORM=linux/<arch> (default: host-native). The heavy
# C++/PCL/SDK compile is slow under qemu — prefer a native arm64 host/runner for Jetson images.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

REGISTRY="${1:?usage: build_image.sh <registry> [tag]}"
TAG="${2:-latest}"
IMAGE="${REGISTRY}/ouster_driver:${TAG}"

docker build ${OUSTER_BUILD_PLATFORM:+--platform "${OUSTER_BUILD_PLATFORM}"} \
  -f "$REPO/docker/Dockerfile.runtime" -t "$IMAGE" "$REPO"
docker push "$IMAGE"
echo "build_image.sh: pushed $IMAGE" >&2
