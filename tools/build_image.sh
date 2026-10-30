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
#   RIG_BUILD_PULL      0 => no --pull (`rig build --offline` sets it, with RIG_OFFLINE=1).
#   RIG_BUILD_SOURCES   rig >= v0.2.74: rigging.yaml `build.sources` exported from rig's local git
#                       mirrors (one directory per source plus sources.tsv: name, repo, ref,
#                       commit). Passed as the Dockerfile's ouster-ros/ouster-sdk named build
#                       contexts, so the image build fetches no git. Each export must be exactly the
#                       repo and commit ouster.repos / ouster-sdk.repos pin. Unset = clone in the build.
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
if [ -n "${RIG_BUILD_NO_CACHE:-}" ]; then
  # Docker --no-cache bypasses layers, but leaves compiler-cache mounts intact.
  set -- --build-arg OUSTER_DISABLE_CCACHE=1 "$@"
fi
if [ -z "${RIG_BUILD_PLATFORMS:-}" ] && [ -n "${OUSTER_BUILD_PLATFORM:-}" ]; then
  set -- --platform "$OUSTER_BUILD_PLATFORM" "$@"
fi
# rig's exports of build.sources. rig resolves the RIGGING's refs, so a rigging that drifted from
# the .repos manifests fails here instead of building one commit while provenance records another.
if [ -n "${RIG_BUILD_SOURCES:-}" ]; then
  for pair in ouster-ros:ouster.repos ouster-sdk:ouster-sdk.repos; do
    name="${pair%%:*}" manifest="${pair#*:}"
    url="$(awk '$1 == "url:" {print $2}' "$REPO/$manifest")"
    pin="$(awk '$1 == "version:" {print $2}' "$REPO/$manifest")"
    row="$(awk -F '\t' -v n="$name" '$1 == n' "$RIG_BUILD_SOURCES/sources.tsv" 2>/dev/null || true)"
    if [ -z "$row" ] || [ ! -d "$RIG_BUILD_SOURCES/$name" ]; then
      echo "build_image.sh: RIG_BUILD_SOURCES carries no '$name' export;" \
           "declare it in rigging.yaml build.sources" >&2
      exit 1
    fi
    IFS=$'\t' read -r _ repo _ commit <<<"$row"
    if [ "$repo" != "$url" ] || [ "$commit" != "$pin" ]; then
      echo "build_image.sh: rig exported $name as $repo @ $commit, but $manifest pins" \
           "$url @ $pin; keep rigging.yaml build.sources matched to $manifest" >&2
      exit 1
    fi
    set -- "$@" --build-context "$name=$RIG_BUILD_SOURCES/$name"
  done
fi
rig_build "$IMAGE" "$@" \
  --build-arg "ROS_DISTRO=${ROS_DISTRO:-lyrical}" \
  --build-arg "BASE_IMAGE=${RIG_BASE_IMAGE:-ros:${ROS_DISTRO:-lyrical}-ros-core}" \
  -f "$REPO/docker/Dockerfile.runtime" "$REPO"
