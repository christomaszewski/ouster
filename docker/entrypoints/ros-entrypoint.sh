#!/usr/bin/env bash
# Sources the ROS environment and the workspace overlay (if present), applies the launcher-
# rendered zenoh session overrides (rmw_zenoh only), then execs the given command. Used as the
# ENTRYPOINT for both Dockerfiles (runtime + dev).
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

# Opt-in zenoh session overrides (sensor config `zenoh:` block -> compose env
# OUSTER_ZENOH_OVERRIDE, a launcher-rendered "path=json;..." pair string). rmw_zenoh applies
# ZENOH_CONFIG_OVERRIDE pairs ON TOP of whatever session config it loads — its shipped ROS
# default, or a deployment-set ZENOH_SESSION_CONFIG_URI file — so untouched keys keep their
# loaded values. Appended AFTER any pre-existing container-level override: ours later = ours
# wins on key conflict. A bad pair is a WARN in the driver log and ignored (it cannot hard-fail
# the driver); the echo below is the field-debugging record of what actually applied. The
# rmw_zenoh_cpp guard keeps the knob inert under Fast DDS (the default RMW).
if [[ "${RMW_IMPLEMENTATION:-}" == "rmw_zenoh_cpp" && -n "${OUSTER_ZENOH_OVERRIDE:-}" ]]; then
  export ZENOH_CONFIG_OVERRIDE="${ZENOH_CONFIG_OVERRIDE:+${ZENOH_CONFIG_OVERRIDE};}${OUSTER_ZENOH_OVERRIDE}"
  echo "ros-entrypoint: ZENOH_CONFIG_OVERRIDE=${ZENOH_CONFIG_OVERRIDE}" >&2
fi

exec "$@"
