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
# vendor upstream into src/ (gitignored; needs vcstool — apt install python3-vcstool):
mkdir -p src && vcs import src < ouster.repos
git -C src/ouster-ros submodule update --init --recursive 2>/dev/null || true   # some pins use one

# colcon build (needs a ROS 2 lyrical env; first time: rosdep install --from-paths src --ignore-src -y):
colcon build --base-paths src --merge-install \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="-Wno-deprecated-declarations"

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

### Thermal / telemetry status

The driver publishes `ouster_sensor_msgs/msg/Telemetry` on **`/<ns>/telemetry`**:
thermal-shutdown and shot-limiting status plus their countdowns, stamped from every lidar
packet's header (packet rate — ~640 msgs/s at 1024x10). It is **on by default** via the `TLM`
token in the `proc_mask` driver param (node default `IMU|PCL|SCAN|IMG|RAW|TLM`; drop `TLM` to
disable). `ouster_sensor_msgs` is already pinned in `rigging.yaml`'s `msgs:` block, so the
fleet bag logger can record it as-is. Two limits to know:

- It is derived from lidar packets, so it goes **silent in standby** — parked-vehicle health
  is the healthcheck's sensor HTTP probe, not this topic.
- It carries status, **not temperatures**. Actual readings (input voltage/current, and on
  newer FW/revisions `internal_temperature_deg_c`) live on the sensor's HTTP API
  (`GET /api/v1/sensor/telemetry`, FW ≥ 2.4; field availability varies by hardware revision).
  Publishing those for dashboards belongs to the fleet diagnostics layer, not this wrapper
  (no bespoke status topics here — see the operational-state contract).

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

Mission software must use the rig/launcher operational-state verbs. Direct ROS lifecycle changes
are not persistent operational-state requests: the supervisor will reconcile them to its target.
Only one service instance should own a physical sensor's configuration.

Regression tests run with `python3 -m unittest discover -s tests -v`. Before field deployment,
validate on the OS1-64/FW 2.4: boot with persisted standby, activate, repeat standby/activate,
restart the container while parked, and power-cycle the sensor while parked. Check both reported
state and sensor status; mock tests do not replace this hardware check.

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

The only coupling to upstream is its launch CLI + param names. If `driver.launch.py` args
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
