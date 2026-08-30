#!/usr/bin/env bash
# In-container implementation of the rig operational-state contract for the ouster driver:
#   statectl.sh state     -> machine JSON on stdout: {"state": ..., "detail": ...}
#   statectl.sh standby   -> park: lifecycle deactivate + sensor operating_mode STANDBY
#   statectl.sh activate  -> wake: sensor operating_mode NORMAL + lifecycle activate
# Invoked by `ouster-up <config> standby|activate|state` via `docker compose exec -T driver`.
# Stdout discipline: ONLY the `state` JSON goes to stdout; every human line goes to stderr.
#
# Why both layers: upstream's on_deactivate only stops the UDP-reading thread — the sensor
# itself keeps spinning at full power — so the power/heat win of standby requires driving the
# device's own STANDBY operating mode over its HTTP config API. Ordering is load-bearing:
#   standby:  deactivate FIRST, then sensor STANDBY (an active node with a silent sensor
#             poll-error-loops into a self reset that never wakes it)
#   activate: sensor NORMAL FIRST (wait for RUNNING), then lifecycle activate; the driver's
#             init_id-change detection then performs one clean self reset that refreshes
#             metadata — upstream's own designed sensor-reinit path. `state` may report
#             `transitioning` for a few seconds while it runs; compose healthcheck retries
#             absorb it.
# The mode change is applied NOT persisted (no flash wear): a sensor power cycle while parked
# boots back into its persisted mode — re-run standby to re-park; activate is unaffected
# (it ensures NORMAL regardless).
set -eo pipefail

VERB="${1:?usage: statectl.sh state|standby|activate}"

ROS_DISTRO="${ROS_DISTRO:-lyrical}"
if [[ -f "/opt/ros/${ROS_DISTRO}/setup.bash" ]]; then
  # shellcheck disable=SC1090
  source "/opt/ros/${ROS_DISTRO}/setup.bash" 2>/dev/null || true
fi
if [[ -f "${OUSTER_DRIVER_WORKSPACE:-/opt/ouster_driver}/setup.bash" ]]; then
  # shellcheck disable=SC1090,SC1091
  source "${OUSTER_DRIVER_WORKSPACE:-/opt/ouster_driver}/setup.bash" 2>/dev/null || true
fi

# Same instance addressing as the healthcheck: the launcher exports OUSTER_NAMESPACE and the
# deploy compose passes it through, so we always talk to OUR node on the shared host graph.
NODE="${OUSTER_NAMESPACE:-}/os_driver"
PARAMS_FILE="${OUSTER_PARAMS_FILE:-/etc/ouster_driver/params.yaml}"

LC_TIMEOUT=8        # one ros2 lifecycle CLI call (covers the CLI cold start)
SETTLE_TIMEOUT=20   # waiting out a transient configuring/activating/deactivating
LC_SET_TIMEOUT=30   # a transition completing (on_activate fetches metadata over HTTP)

log() { echo "statectl: $*" >&2; }
die() { log "$*"; exit 1; }

# First token of `ros2 lifecycle get` ("inactive [2]" -> "inactive"); fails when the node is
# unreachable (process starting/dead) — callers surface that as probe-cannot-answer.
lc_get() {
  local raw
  raw="$(timeout "$LC_TIMEOUT" ros2 lifecycle get "$NODE" 2>/dev/null)" || return 1
  echo "${raw%% *}"
}

# Wait until the node reports a SETTLED lifecycle state (not *ing) and echo it. The driver
# transitions itself (launch auto-activate at startup, reconnect/self-reset cycles), so a
# transient transitional read just needs a short wait; a stuck one is the caller's error.
lc_settle() {
  local deadline=$((SECONDS + SETTLE_TIMEOUT)) state
  while :; do
    state="$(lc_get)" || return 1
    case "$state" in
      active|inactive|unconfigured|finalized) echo "$state"; return 0 ;;
    esac
    [ "$SECONDS" -ge "$deadline" ] && { echo "$state"; return 0; }
    sleep 1
  done
}

# Request one lifecycle transition and verify the node actually reached the expected state
# (the CLI's exit code is not a reliable transition verdict).
lc_set() {
  local transition="$1" expect="$2" deadline state
  timeout "$LC_SET_TIMEOUT" ros2 lifecycle set "$NODE" "$transition" >&2 || true
  deadline=$((SECONDS + LC_SET_TIMEOUT))
  while :; do
    state="$(lc_get)" || return 1
    [ "$state" = "$expect" ] && return 0
    [ "$SECONDS" -ge "$deadline" ] && { log "'$transition' did not reach '$expect' (node reports '$state')"; return 1; }
    sleep 1
  done
}

# Drive the sensor's own operating mode over its HTTP config API (reachable in STANDBY too).
# ensure_sensor_mode STANDBY|NORMAL: idempotent — checks the active config first, applies the
# staged-config sequence (FW >= 3.1) with a fallback to the legacy cmd endpoints (older FW),
# then polls the sensor's OWN status endpoint until the mode physically took (STANDBY: motor
# stopped; NORMAL: back to RUNNING after re-init/spin-up) — never inferred from the ROS graph.
ensure_sensor_mode() {
  python3 - "$PARAMS_FILE" "$1" <<'PY'
import json, sys, time, urllib.error, urllib.request

import yaml

params_file, target = sys.argv[1], sys.argv[2]
SETTLED = {"STANDBY": "STANDBY", "NORMAL": "RUNNING"}[target]
# STANDBY entry is quick (motor stops); NORMAL re-init + spin-up takes tens of seconds.
WAIT_S = 60 if target == "STANDBY" else 120


def log(msg):
    print(f"statectl: {msg}", file=sys.stderr, flush=True)


with open(params_file) as fh:
    doc = yaml.safe_load(fh) or {}
host = None
for node in doc.values():
    rp = (node or {}).get("ros__parameters") or {}
    if "sensor_hostname" in rp:
        host = str(rp["sensor_hostname"])
        break
if not host:
    log(f"no sensor_hostname in {params_file}")
    sys.exit(1)


def http(path, body=None, timeout=10):
    req = urllib.request.Request(
        f"http://{host}/api/v1{path}",
        data=None if body is None else body.encode(),
        headers={} if body is None else {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def sensor_status():
    return json.loads(http("/sensor/metadata/sensor_info")).get("status", "")


try:
    mode = json.loads(http("/sensor/config")).get("operating_mode", "NORMAL")
    if mode == target:
        log(f"sensor {host}: operating_mode already {target}")
    else:
        log(f"sensor {host}: operating_mode {mode} -> {target}")
        body = json.dumps({"operating_mode": target})
        try:
            # stage the one changed key, then apply staged -> active (a sensor re-init)
            http("/sensor/config?staging=true&reinit=false&persist=false", body)
            http("/sensor/config?staging=true&reinit=true&persist=false", "{}")
        except urllib.error.HTTPError as err:
            if err.code not in (404, 405):
                raise
            # FW < 3.1 has no staging API
            http(f"/sensor/cmd/set_config_param?args=operating_mode+{target}")
            http("/sensor/cmd/reinitialize")
    deadline = time.monotonic() + WAIT_S
    while True:
        status = sensor_status()
        if status == SETTLED:
            log(f"sensor {host}: status {status}")
            break
        if status == "ERROR":
            log(f"sensor {host}: status ERROR")
            sys.exit(1)
        if time.monotonic() > deadline:
            log(f"sensor {host}: timed out waiting for {SETTLED} (status {status})")
            sys.exit(1)
        time.sleep(2)
except (urllib.error.URLError, OSError) as err:
    log(f"sensor {host}: HTTP config API unreachable: {err}")
    sys.exit(1)
PY
}

case "$VERB" in
  state)
    # Read-only, side-effect-free probe. Contract vocabulary on stdout; the raw lifecycle
    # state goes in `detail`. `unconfigured` maps to transitioning: it only occurs mid
    # startup or mid reconnect/self-reset cycle — if it sticks, the healthcheck goes
    # unhealthy, which is the signal for "stuck", not this probe. Exit nonzero only when
    # the probe itself cannot answer (node unreachable); `down` (no containers) is
    # reported by the launcher, which cannot exec into a container that does not exist.
    raw="$(lc_get)" || die "cannot reach lifecycle node $NODE"
    case "$raw" in
      active)   mapped=active ;;
      inactive) mapped=standby ;;
      *)        mapped=transitioning ;;
    esac
    printf '{"state": "%s", "detail": "lifecycle:%s"}\n' "$mapped" "$raw"
    ;;

  standby)
    state="$(lc_settle)" || die "cannot reach lifecycle node $NODE"
    case "$state" in
      inactive)
        log "already parked (lifecycle inactive)" ;;
      active)
        log "deactivating $NODE"
        lc_set deactivate inactive || die "lifecycle deactivate failed"
        ;;
      unconfigured|finalized)
        die "cannot standby from lifecycle '$state' — the driver is not up and configured (check health / logs)" ;;
      *)
        die "stuck in lifecycle transition '$state' — not settling within ${SETTLE_TIMEOUT}s" ;;
    esac
    # Only now quiet the device itself — the node no longer reads packets, so the stream
    # stopping cannot trip the driver's poll-error self reset.
    ensure_sensor_mode STANDBY
    log "standby complete: lifecycle inactive, sensor operating_mode STANDBY"
    ;;

  activate)
    state="$(lc_settle)" || die "cannot reach lifecycle node $NODE"
    case "$state" in
      active|inactive|unconfigured) ;;
      finalized)
        die "cannot activate from lifecycle 'finalized' — restart the service ('up')" ;;
      *)
        die "stuck in lifecycle transition '$state' — not settling within ${SETTLE_TIMEOUT}s" ;;
    esac
    # Wake the device FIRST and wait until it reports RUNNING: activating the node against a
    # STANDBY sensor only poll-error-loops (its self reset never changes the operating mode).
    ensure_sensor_mode NORMAL
    case "$state" in
      active)
        log "already active (lifecycle active, sensor NORMAL)" ;;
      inactive)
        log "activating $NODE"
        lc_set activate active || die "lifecycle activate failed"
        ;;
      unconfigured)
        # e.g. parked with an unreachable sensor until the driver's reconnect attempts ran
        # out; the sensor answers again, so drive the full bring-up.
        log "configuring + activating $NODE (was unconfigured)"
        lc_set configure inactive || die "lifecycle configure failed (sensor reachable but configure did not take — see logs)"
        lc_set activate active || die "lifecycle activate failed"
        ;;
    esac
    log "activate complete: lifecycle active, sensor operating_mode NORMAL"
    ;;

  *)
    die "unknown verb '$VERB' (state|standby|activate)" ;;
esac
