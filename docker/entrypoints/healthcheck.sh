#!/usr/bin/env bash
# Readiness probe for the runtime image (operational-state contract): healthy ⇔ THIS
# container's os_driver node is SETTLED in lifecycle `active` OR `inactive` (= standby) AND
# could run — never whether data flows, because a healthy standby instance produces nothing
# by design. Probing the lifecycle state (not topics) also keeps the probe per-instance on a
# host-networked vehicle where every node shares one graph.
#   active     -> healthy: the driver holds a live sensor session, and its own reconnect /
#                 self-reset machinery drops out of `active` when the sensor goes away.
#   inactive   -> standby holds NO live sensor session, so reachability must be proven here:
#                 the sensor's HTTP API (which answers in its STANDBY mode too) — a parked
#                 vehicle surfaces a dead/unplugged sensor while parked, not at activation.
#   everything else (unconfigured, finalized, *ing transitions) -> fail this round; compose
#                 retries absorb a real transition, a stuck one goes unhealthy — desired.
set -eo pipefail

ROS_DISTRO="${ROS_DISTRO:-lyrical}"
if [[ -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]]; then
  # shellcheck disable=SC1090
  source "/opt/ros/${ROS_DISTRO}/setup.bash" 2>/dev/null || true
fi
if [[ -f "${OUSTER_DRIVER_WORKSPACE:-/opt/ouster_driver}/setup.bash" ]]; then
  # shellcheck disable=SC1090,SC1091
  source "${OUSTER_DRIVER_WORKSPACE:-/opt/ouster_driver}/setup.bash" 2>/dev/null || true
fi

# The launcher exports OUSTER_NAMESPACE (e.g. /top) and the deploy compose passes it through, so the
# probe asks its own instance; bare `docker run` defaults to the root namespace. Upstream names the
# lifecycle node `os_driver` under ouster_ns.
NODE="${OUSTER_NAMESPACE:-}/os_driver"
state="$(timeout 8 ros2 lifecycle get "$NODE" 2>/dev/null)" || exit 1

case "$state" in
  active*) exit 0 ;;
  inactive*)
    # standby: prove the sensor is still there. The launcher-rendered params file carries
    # sensor_hostname; a plain GET against the sensor's own status endpoint answers in
    # STANDBY. Absent hostname/params = unconfigured instance = unhealthy.
    exec python3 - "${OUSTER_PARAMS_FILE:-/etc/ouster_driver/params.yaml}" <<'PY'
import sys
import urllib.request

import yaml

try:
    with open(sys.argv[1]) as fh:
        doc = yaml.safe_load(fh) or {}
    host = None
    for node in doc.values():
        rp = (node or {}).get("ros__parameters") or {}
        if "sensor_hostname" in rp:
            host = str(rp["sensor_hostname"])
            break
    if not host:
        sys.exit(1)
    urllib.request.urlopen(
        f"http://{host}/api/v1/sensor/metadata/sensor_info", timeout=5)
except Exception:
    sys.exit(1)
PY
    ;;
  *) exit 1 ;;
esac
