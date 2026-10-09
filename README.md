# ouster — rig-integrated wrapper for the official Ouster ROS 2 driver

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A thin, rig-compatible packaging of the upstream
[`ouster-ros`](https://github.com/ouster-lidar/ouster-ros) lidar driver (the `ros2` lineage),
targeting **ROS 2 Lyrical** and the in-house **rig** orchestrator.

Unlike the `sbg`/`vectornav` drivers in this workspace (which reimplement their devices in-house),
this repo **wraps the official Ouster driver unmodified** so upstream releases can be pulled in with
a one-line version bump. We add **no C++ of our own** — only the rig-integration shell: a launcher,
a params mapper, a Python operational-state supervisor, Docker, compose, and the `rig` descriptor.

## How it works

| Piece | Role |
|-------|------|
| `ouster.repos` | Pins the upstream `ouster-ros` release. Fetched with `vcs import` at image-build time — nothing upstream is committed here, so the repo stays tiny. |
| `ouster-sdk.repos` | Pins the ROS-compatible SDK fork containing the legacy IMU bounds fix. |
| `tools/fetch_upstream.py` | Fetches both pins into a fresh checkout, replaces the bundled SDK, and records their actual revisions. |
| `tools/render_params.py` | Maps a generic rig sensor config (`connection` + `driver_params`) → upstream's `driver_params.yaml` keys. Keyed by the `/**` wildcard so it binds at any namespace. |
| `docker/compose/compose.deploy.yaml` | Runs the supervisor, which starts upstream's unmodified headless launch only after the sensor is running. |
| `docker/entrypoints/operational_state.py` | Serializes mode changes, owns the driver process group, and reconciles the requested state after failures. |
| `ouster-up` | The rig launcher contract (`up`/`down`/`status`/`logs`/`config`, plus the operational-state verbs `standby`/`activate`/`state`) over one sensor config. |
| `rigging.yaml` | Tells `rig` how to drive `ouster-up` (verbs, build, host ports, metadata volume). |

Upstream `os_driver` is a single `rclcpp_lifecycle` node. The supervisor wakes the sensor
before launching it, and stops the entire driver process group before parking the sensor.
The container stays running in both states. Health requires agreement between the requested
state, sensor operating mode/status, and driver state; it does not measure data flow.

## Layout

```
ouster/
├── ouster.repos              # upstream pin (vcstool)
├── ouster-sdk.repos          # explicit SDK override (full commit SHA)
├── rigging.yaml              # rig descriptor
├── ouster-up                 # rig launcher (networked; no serial branch)
├── sensors/ouster.example.yaml
├── tools/
│   ├── render_params.py      # generic rig config -> ouster driver_params.yaml
│   └── build_image.sh        # rig build phase: build + push runtime image
├── docker/
│   ├── Dockerfile.runtime    # multi-stage: vcs import + rosdep + colcon (Release)
│   ├── Dockerfile.dev
│   ├── entrypoints/{ros-entrypoint,healthcheck,statectl}.sh
│   ├── entrypoints/{operational_state,sensor_http}.py
│   └── compose/{compose.deploy,compose.dev,compose.replay}.yaml
└── src/                      # GITIGNORED — upstream fetched here by `vcs import` / the build
```

## Quick start (local dev, no rig)

```bash
# fetch the ROS driver and patched SDK (needs python3-vcstool and python3-yaml):
python3 tools/fetch_upstream.py src

# colcon build (needs a ROS 2 lyrical env; first time: rosdep install --from-paths src --ignore-src -y):
colcon build --base-paths src --merge-install \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="-Wno-deprecated-declarations" \
    -DCMAKE_PROJECT_ouster_ros_INCLUDE="$PWD/docker/ament_target_dependencies_compat.cmake"

#   …or skip the local toolchain and build the deployable image instead:
docker build -f docker/Dockerfile.runtime -t ouster_driver:latest .

cp sensors/ouster.example.yaml sensors/ouster_top.yaml   # edit sensor_hostname / ports
./ouster-up sensors/ouster_top.yaml up                   # foreground (Ctrl-C to stop)
ros2 lifecycle get /top/os_driver                        # -> active
ros2 topic hz /top/points
```

## Configuration

One generic rig config per sensor instance (see `sensors/ouster.example.yaml`):

- `connection.type: lidar` with `connection.lidar.{sensor_hostname, udp_dest, lidar_port, imu_port}`
  — mapped onto the upstream params by `render_params.py`.
- `ros.namespace` — becomes `ouster_ns` (so topics are `/<ns>/points`, `/<ns>/imu`, …).
- `driver_params` — copied **verbatim** into `ros__parameters`; any key from upstream
  `config/driver_params.yaml` works (`lidar_mode`, `timestamp_mode`, `udp_profile_lidar`,
  `point_type`, `sensor_frame`/`lidar_frame`/`imu_frame`, `attempt_reconnect`, …). Do **not** put
  connection keys here — they come from the `connection` block.
- `shutdown_state: standby` (default) — physically park on container shutdown;
  `unchanged` stops the driver without changing the sensor's operating mode.

### Temperature and thermal status

The wrapper publishes **`/<ns>/temperature`** as `sensor_msgs/msg/Temperature` from the
`/<ns>/sensor_health` node. It reads `internal_temperature_deg_c` with a read-only
`GET /api/v1/sensor/telemetry` about every **5 seconds**, in both active and standby states.
It pauses polling during mode transitions and resumes afterward, independently of the
driver's `proc_mask`.

- `temperature`: internal sensor temperature in **degrees Celsius**.
- `header.frame_id`: `driver_params.sensor_frame` (upstream default: `os_sensor`).
- `header.stamp`: host receipt time; the sensor's FPGA timestamp is not assumed to be Unix time.
- `variance`: `0.0`, meaning unknown. QoS is reliable, volatile, depth 1.

```bash
ros2 topic echo /top/temperature sensor_msgs/msg/Temperature
```

**Hardware limit:** the [FW 2.4 manual, §13.4.3 (p. 126)](https://data.ouster.io/downloads/software-user-manual/firmware-user-manual-v2.4.0.pdf)
documents internal temperature for **Rev 06 and newer** sensors only. A Rev 4/5 OS1 therefore
cannot be assumed to provide readings. Missing/null/invalid values or HTTP failures skip the
sample and log a warning at most once per minute; polling continues without affecting the
operational state. No zero or previous reading is substituted. `sensor_msgs` is a standard ROS
interface, so recording this topic needs no additional fleet message package.

The driver publishes `ouster_sensor_msgs/msg/Telemetry` on **`/<ns>/telemetry`**:
thermal-shutdown and shot-limiting status plus their countdowns, stamped from every lidar
packet's header (packet rate — ~640 msgs/s at 1024x10). It is **on by default** via the `TLM`
token in the `proc_mask` driver param (node default `IMU|PCL|SCAN|IMG|RAW|TLM`; drop `TLM` to
disable). `ouster_sensor_msgs` is already pinned in `rigging.yaml`'s `msgs:` block, so the
fleet bag logger can record it as-is. This topic carries thermal status flags, not temperature
measurements, and goes **silent in standby** because it is derived from lidar packets.

## Operational states (`standby` / `activate` / `state`)

A running container has two operational states, managed through the launcher:

```bash
./ouster-up sensors/ouster_top.yaml standby
./ouster-up sensors/ouster_top.yaml state
# {"state": "standby", "detail": "target:standby; lifecycle:absent; sensor:STANDBY/STANDBY"}
./ouster-up sensors/ouster_top.yaml activate
```

- **`standby`** stops and reaps the upstream launch process group, then applies the sensor's
  `STANDBY` mode and waits for physical standby. The motor and laser stop. The container and
  control socket remain available, but **the ROS driver node and its services are absent**.
  Stopping the driver also stops queued reconnect/reset operations from waking the sensor.
- **`activate`** applies `NORMAL` and waits for sensor status `RUNNING` before starting the
  driver. Driver configuration then fetches fresh metadata; activation never depends on a
  ROS service responding while the sensor is asleep. Completion requires lifecycle `active`
  and a sensor reporting `NORMAL/RUNNING`.
- **`state`** prints machine JSON on stdout (`active` | `standby` | `transitioning` | `down`).
  Its detail includes the target, lifecycle state, and sensor mode/status. A mismatch or
  unreachable sensor is `transitioning` and unhealthy, including after a partial failure.
- Requests are serialized and idempotent. If another transition remains busy for 20 seconds,
  the command fails with an explicit retry message. A failed accepted request retains its
  target; the supervisor retries recovery every five seconds. The healthcheck and `state`
  are read-only. Sensor mode writes never intentionally persist config to flash.

Firmware below 3.1 (including 2.4) uses the legacy config commands followed by `reinitialize`;
newer firmware uses explicit nonpersistent staging. Temporary HTTP failures during spin-up
are retried within the deadline. The supervisor supports the metadata endpoints in FW 2.3+.
Keep `driver_params.operating_mode` omitted or `NORMAL`; use `initial_state` to request standby.
Leave upstream `persist_config` false if the sensor should retain its existing boot defaults.

**Initial state:** `RIG_TARGET_STATE` > top-level `initial_state` > `active` selects the target
for a new container. Standby startup does not launch the driver or briefly wake the sensor.
Both foreground `up` and `up -d` support standby. Detached `up -d` additionally waits for the
requested state, including when reusing an existing container. Its default budget is 300 seconds
(`OUSTER_SETTLE_TIMEOUT`; the older `OUSTER_STANDBY_SETTLE_TIMEOUT` also remains accepted).

Runtime requests are saved in `/var/lib/ouster/target-state` inside the container, so a Docker
restart retains the latest request. Recreating the container uses the configured initial state.
Foreground `up` attached to an existing container preserves its runtime target; use `activate`,
`standby`, or detached `up -d` to change it. A sensor power cycle while parked is detected and
the sensor is parked again when reachable.

**Shutdown state:** top-level `shutdown_state: standby` parks the physical sensor when
`rig down`, launcher `down`, Docker stop/restart, or foreground Ctrl-C stops the container.
The supervisor first requests lifecycle `deactivate` and `cleanup`, which drain upstream's
UDP and scan-processing threads before ROS/Zenoh closes. It then stops and reaps the entire
driver process group, applies nonpersistent STANDBY, and waits for the sensor to confirm it.
Lifecycle probes and transitions reuse service clients in the health node's existing ROS
session, avoiding repeated `ros2 lifecycle` CLI startup/discovery/teardown. Its executor stays
alive during driver cleanup even after SIGTERM stops health polling. These calls run immediately;
`health.interval_s` and `health.poll_interval_s` do not set shutdown cadence. Each lifecycle
request still has a five-second bound; failed requests fall through to process-group termination.
If ROS is unreachable, bounded signal escalation still stops the process group before the
sensor mode changes. Compose allows 120 seconds for shutdown, including up to 60 seconds
waiting for standby. These are maximum failure budgets, not fixed delays. Logs report lifecycle
drain time, driver stop time, signal escalation, and total shutdown sequence time so driver delay
can be distinguished from sensor parking. The launcher stops the container first, checks its exit status, and
prints the last 40 log lines if shutdown failed, before removing it. A failed park or forced
kill therefore makes `rig down` return nonzero. Direct `docker compose down` bypasses that
exit-status check; its success alone does not prove sensor standby.

Use `shutdown_state: unchanged` to stop the driver while leaving the sensor mode alone.
The shutdown policy is captured when the container is created; recreate it after changing
that YAML setting. Shutdown parking does **not** overwrite the saved runtime target, so a
restart of an active container wakes it again. A new container follows `initial_state` as
before. Firmware boot defaults are not written to flash. SIGKILL, host power loss, or a
disconnected sensor cannot guarantee standby. `rig down` stops Ouster before its Zenoh
router in the supplied deployment; keep that ordering for graceful ROS cleanup.

Rendered params use content-based filenames under `var/run/`. Changing driver or connection
parameters changes the bind-mount path, so the next `up` recreates the container and reloads
both driver and supervisor configuration. Old files are retained for existing containers.
`status`, `logs`, state verbs, and `down` never write those files; failed validation cannot
truncate a live container's params. `config` also stages validated params so `rig bake` can
capture the file bind, without changing the contents mounted by an existing container.

Mission software must use the rig/launcher operational-state verbs. Direct ROS lifecycle changes
are not persistent operational-state requests: the supervisor will reconcile them to its target.
Only one service instance should own a physical sensor's configuration.

Regression tests run with `python3 -m unittest discover -s tests -v`. Before field deployment,
validate on the OS1-64/FW 2.4: boot with persisted standby, activate, repeat standby/activate,
restart the container while parked, and power-cycle the sensor while parked. Check both reported
state and sensor status; mock tests do not replace this hardware check.

The runtime integration test uses real ROS lifecycle services when ROS is installed and verifies
deactivate/cleanup after SIGTERM without any lifecycle CLI calls. An isolated Orin/Zenoh benchmark
with a simulated lifecycle node measured driver stop at 3.50 seconds with CLI calls versus 0.11
seconds with persistent clients. This excludes physical sensor parking and whole-stack teardown;
it is not a hardware shutdown-time guarantee.

The images ship both `rmw_fastrtps_cpp` (default) and `rmw_zenoh_cpp`; export
`RMW_IMPLEMENTATION=rmw_zenoh_cpp` before `ouster-up` to switch. The driver never runs the Zenoh
router — that is rig-managed infrastructure the session connects out to (endpoint overridable via
`ZENOH_CONFIG_OVERRIDE`) — see [docker/README.md](docker/README.md) "RMW selection / Zenoh".

## Updating upstream

1. Bump `version:` in [`ouster.repos`](ouster.repos) to a newer upstream release tag.
2. Bump `msgs.source.ref` in [`rigging.yaml`](rigging.yaml) to the same value — it pins
   `ouster_sensor_msgs` (same repo) for the deployment's fleet-ros-msgs overlay; a drifted pin
   means silently skewed schemas in recorded bags.
3. Rebuild: `tools/build_image.sh <registry> [tag]` (or, local-only:
   `docker build -f docker/Dockerfile.runtime -t ouster_driver:latest .`).
4. Skim upstream `CHANGELOG.rst` for renamed launch args / params.
5. Commit both changes together (e.g. `vendor ouster-ros 0.x.y`).

The SDK is independently pinned in [`ouster-sdk.repos`](ouster-sdk.repos). Our fork starts at
upstream `sdk-0.16.2/ouster-ros-0.15.0` and backports the legacy IMU bounds correction from SDK 1.0
without changing the API expected by this ROS driver. An SDK-only update does not change
`ouster_sensor_msgs`, so leave the two ROS source references matched to each other.
The backport is submitted as [Ouster SDK PR #727](https://github.com/ouster-lidar/ouster-sdk/pull/727).

Use `python3 tools/fetch_upstream.py src` for both Docker and local source builds. It refuses to
replace an existing `src/ouster-ros`; preserve any local work and select a fresh source directory
when changing pins. Running `vcs import` against only `ouster.repos` omits the SDK override.
The runtime image records the requested repositories/refs and the actual commits at
`/opt/ouster_driver/share/upstream-sources.json`, independently of the fleet message provenance.
When upstream ROS adopts the fixed SDK, the override can be retired along with its fetch logic.

The wrapper relies on upstream's launch CLI, parameter names, and the ROS driver's SDK API.
Keep SDK overrides compatible with the selected ROS source. If `driver.launch.py` args
(`params_file`, `ouster_ns`, `viz`) or `driver_params.yaml` keys are renamed, update
`tools/render_params.py`, `docker/compose/compose.deploy.yaml`, and the healthcheck node name.

**LAN mirror (later):** change only `url:` in `ouster.repos` (keep `version:`). For a *fully* offline
build you also need an apt/rosdep mirror — the dependency install is a separate network dependency
from the source fetch.

## Releasing

The service is published in the public rig registry as `public/ouster`
([rig-registry-public](https://github.com/christomaszewski/rig-registry-public)). To cut a release:

```bash
gh release create v0.2.0 --generate-notes    # tag + GitHub release in one step
```

The `registry-release` workflow then updates the registry entry (version + pinned `source.rev`)
and regenerates its index automatically. One-time setup: a `RIG_REGISTRY_TOKEN` Actions secret in
this repo — a fine-grained PAT scoped to `rig-registry-public` with Contents read/write only.
Release tags must be exact `vX.Y.Z`.

## License

[Apache 2.0](LICENSE) for this wrapper. Upstream `ouster-ros` ships under its own license, fetched
with the source at build time.

## Standard service health

The supervisor publishes standard `diagnostic_msgs/msg/DiagnosticArray` on `/diagnostics`,
including operation, stream availability, temperature, input voltage/current/power and
sensor alerts. It polls read-only `/api/v1/sensor/telemetry` and `/api/v1/sensor/alerts` even
in standby, while skipping transitions. Power is input mV × mA / 1,000,000 watts; unavailable
readings are not zero. The existing temperature topic remains available.

Export `VEHICLE_ID` for fleet identity. Optional sensor YAML `health:` configures `interval_s`
(default 1), `poll_interval_s` (5), `stale_after_s` (15), and `limits`. To reach the dashboard,
use the deployment's rmw_zenoh router or supported DDS bridge. See the complete
[shared health specification](docs/SERVICE_HEALTH.md) for adoption, alert history,
configuration, tests and remaining hardware validation.
