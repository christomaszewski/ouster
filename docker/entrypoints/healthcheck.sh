#!/usr/bin/env bash
# Liveness probe for the runtime image: healthy when THIS container's os_driver node is
# reachable and holds the lifecycle `active` state. We probe the lifecycle state rather than
# sampling absolute topics: on a host-networked vehicle every node shares one graph, so a
# per-container topic probe could read a neighbor's data.
set -eo pipefail

ROS_DISTRO="${ROS_DISTRO:-lyrical}"
# shellcheck disable=SC1090
source "/opt/ros/${ROS_DISTRO}/setup.bash" 2>/dev/null || true
if [[ -f "${OUSTER_DRIVER_WORKSPACE:-/opt/ouster_driver}/setup.bash" ]]; then
  # shellcheck disable=SC1090,SC1091
  source "${OUSTER_DRIVER_WORKSPACE:-/opt/ouster_driver}/setup.bash" 2>/dev/null || true
fi

# The launcher exports OUSTER_NAMESPACE (e.g. /top) and the deploy compose passes it through, so the
# probe asks its own instance; bare `docker run` defaults to the root namespace. Upstream names the
# lifecycle node `os_driver` under ouster_ns.
NODE="${OUSTER_NAMESPACE:-}/os_driver"
state="$(timeout 8 ros2 lifecycle get "$NODE" 2>/dev/null)" || exit 1
[[ "$state" == active* ]] || exit 1
exit 0
