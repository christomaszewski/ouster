#!/usr/bin/env bash
# Sources the ROS environment and the workspace overlay (if present), applies the opt-in zenoh
# shared-memory session config (rmw_zenoh only), then execs the given command. Used as the
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

# Opt-in zenoh shared memory (sensor config `zenoh:` block -> compose env ZENOH_SHM_ENABLE /
# ZENOH_SHM_POOL_BYTES). rmw_zenoh ships SHM support compiled in but DISABLED, and the only
# switch is a session config file: ZENOH_SESSION_CONFIG_URI REPLACES the shipped default
# WHOLESALE, so we patch a COPY of the shipped file — never a hand-written minimal config, which
# would drop rmw_zenoh's ROS defaults (peer mode, connect tcp/localhost:7447). SHM engages per
# link only where the peer session ALSO enabled it and both containers share the host IPC
# namespace (the deploy compose sets ipc: host); anything unmet falls back to TCP loopback
# SILENTLY. The rmw_zenoh_cpp guard keeps the knob inert under Fast DDS (the default RMW).
if [[ "${RMW_IMPLEMENTATION:-}" == "rmw_zenoh_cpp" && "${ZENOH_SHM_ENABLE:-0}" == "1" ]]; then
  shipped="/opt/ros/${ROS_DISTRO}/share/rmw_zenoh_cpp/config/DEFAULT_RMW_ZENOH_SESSION_CONFIG.json5"
  patched="/tmp/zenoh_session_shm.json5"
  if [[ -n "${ZENOH_SESSION_CONFIG_URI:-}" ]]; then
    echo "ros-entrypoint: ZENOH_SESSION_CONFIG_URI already set (${ZENOH_SESSION_CONFIG_URI}); skipped the zenoh SHM patch" >&2
  elif [[ ! -f "${shipped}" ]]; then
    # [ -f ] guard: a non-zenoh image must not crash the entrypoint here under set -e
    echo "ros-entrypoint: WARNING: ${shipped} not found; zenoh SHM NOT enabled" >&2
  else
    # first `enabled: false` inside the `shared_memory: {` block -> true; first `pool_size:`
    # there -> ZENOH_SHM_POOL_BYTES (pool=0 = keep the shipped pool size)
    awk -v pool="${ZENOH_SHM_POOL_BYTES:-0}" '
      /shared_memory: \{/ && !d {s=1}
      s && /enabled: false/ {sub(/enabled: false/, "enabled: true"); if (pool+0 == 0) {s=0; d=1}}
      s && pool+0 > 0 && /pool_size:/ {sub(/pool_size: *[0-9]+/, "pool_size: " pool); s=0; d=1}
      {print}' "${shipped}" > "${patched}"
    if cmp -s "${shipped}" "${patched}"; then
      # never half-enable: if the patch found nothing to change (upstream layout moved?), do NOT
      # point the session at an unpatched copy as if SHM were on
      echo "ros-entrypoint: WARNING: patching ${shipped} changed nothing; zenoh SHM NOT enabled" >&2
    else
      export ZENOH_SESSION_CONFIG_URI="${patched}"
      echo "ros-entrypoint: zenoh SHM enabled via ${patched} (pool_size=${ZENOH_SHM_POOL_BYTES:-0} bytes; 0 = shipped default)" >&2
    fi
  fi
fi

exec "$@"
