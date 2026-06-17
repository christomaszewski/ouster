#!/usr/bin/env bash
# Sources the ROS environment and the workspace overlay (if present), then
# execs the given command. Used as the ENTRYPOINT for all three Dockerfiles.
#
# NOTE: ROS setup.bash scripts reference variables that may not be set yet
# (AMENT_TRACE_SETUP_FILES, AMENT_PYTHON_EXECUTABLE, etc.) — we cannot enable
# `set -u` around the sourcing or those references abort the shell.
set -eo pipefail

ROS_DISTRO="${ROS_DISTRO:-lyrical}"
ROS_SETUP="/opt/ros/${ROS_DISTRO}/setup.bash"

if [[ -f "${ROS_SETUP}" ]]; then
  # shellcheck disable=SC1090
  source "${ROS_SETUP}"
fi

# Source workspace overlay if present. Two conventions:
#   1. OUSTER_DRIVER_WORKSPACE points to an installed prefix (Dockerfile.runtime).
#   2. /workspace/install exists (dev image after a colcon build).
for overlay in \
    "${OUSTER_DRIVER_WORKSPACE:-/opt/ouster_driver}/setup.bash" \
    "/workspace/install/setup.bash"; do
  if [[ -f "${overlay}" ]]; then
    # shellcheck disable=SC1090
    source "${overlay}"
    break
  fi
done

exec "$@"
