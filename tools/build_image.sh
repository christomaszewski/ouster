#!/usr/bin/env bash
# rig's BUILD-phase entrypoint (rigging.yaml `build.command`): build the self-contained runtime
# image and push it to the fleet registry. rig invokes it as
#   tools/build_image.sh <registry> [tag]        (cwd = repo root, tag defaults to latest)
# and the deploy compose pulls the result as ${RIG_IMAGE_REGISTRY}/ouster_driver:${RIG_IMAGE_TAG}.
# Works standalone too:  tools/build_image.sh 192.168.1.10:5000 mytag
#
# rig build-env contract (rig exports these; scripts opt in; all rig-owned vars are set-or-popped,
# so the ${VAR:+...}/${VAR:-...} expansions below are safe under set -u):
#   RIG_BUILD_NO_CACHE  set => --no-cache --pull. --pull matters: a deliberate `rig build
#                       --no-cache` re-pulls the parent image, so the fleet's ros-* version
#                       authority advances for all images together (see docker/Dockerfile.runtime).
#   ROS_DISTRO          fleet distro (vehicle.yaml ros.distro); default lyrical.
#   RIG_BASE_IMAGE      the deployment's one runtime base image (fleet-ros), forwarded to every
#                       dependent build; empty/absent falls back to the standalone ros-core parent.
#   RIG_BUILD_PLATFORMS comma-separated Docker platforms; buildx publishes one image index.
#   RIG_BUILD_RESULT    optional JSONL path for the published image/index digest.
#
# A plain-HTTP local registry needs the daemon's insecure-registries on this host (with the :port).
# Cross-arch vehicle: export OUSTER_BUILD_PLATFORM=linux/<arch> (default: host-native). The heavy
# C++/PCL/SDK compile is slow under qemu — prefer a native arm64 host/runner for Jetson images.
# RIG_BUILD_PLATFORMS takes precedence over OUSTER_BUILD_PLATFORM under rig.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=tools/rig-build.sh
. "$REPO/tools/rig-build.sh"

REGISTRY="${1:?usage: build_image.sh <registry> [tag]}"
TAG="${2:-latest}"
IMAGE="${REGISTRY}/ouster_driver:${TAG}"

set --
if [ -z "${RIG_BUILD_PLATFORMS:-}" ] && [ -n "${OUSTER_BUILD_PLATFORM:-}" ]; then
  set -- --platform "$OUSTER_BUILD_PLATFORM"
fi
rig_build "$IMAGE" "$@" \
  --build-arg "ROS_DISTRO=${ROS_DISTRO:-lyrical}" \
  --build-arg "BASE_IMAGE=${RIG_BASE_IMAGE:-ros:${ROS_DISTRO:-lyrical}-ros-core}" \
  -f "$REPO/docker/Dockerfile.runtime" "$REPO"
