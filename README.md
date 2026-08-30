# ouster — rig-integrated wrapper for the official Ouster ROS 2 driver

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A thin, rig-compatible packaging of the upstream
[`ouster-ros`](https://github.com/ouster-lidar/ouster-ros) lidar driver (the `ros2` lineage),
targeting **ROS 2 Lyrical** and the in-house **rig** orchestrator.

Unlike the `sbg`/`vectornav` drivers in this workspace (which reimplement their devices in-house),
this repo **wraps the official Ouster driver unmodified** so upstream releases can be pulled in with
a one-line version bump. We add **no C++ of our own** — only the rig-integration shell: a launcher,
a params mapper, Docker, compose, and the `rig` descriptor.

## How it works

| Piece | Role |
|-------|------|
| `ouster.repos` | Pins the upstream `ouster-ros` release. Fetched with `vcs import` at image-build time — nothing upstream is committed here, so the repo stays tiny. |
| `tools/render_params.py` | Maps a generic rig sensor config (`connection` + `driver_params`) → upstream's `driver_params.yaml` keys. Keyed by the `/**` wildcard so it binds at any namespace. |
| `docker/compose/compose.deploy.yaml` | Runs upstream's own launch: `ros2 launch ouster_ros driver.launch.py params_file:=… ouster_ns:=… viz:=false`. |
| `ouster-up` | The rig launcher contract (`up`/`down`/`status`/`logs`/`config`, plus the operational-state verbs `standby`/`activate`/`state`) over one sensor config. |
| `rigging.yaml` | Tells `rig` how to drive `ouster-up` (verbs, build, host ports, metadata volume). |

Upstream `os_driver` is a single `rclcpp_lifecycle` node; `driver.launch.py` auto-transitions it
configure → activate. The container healthcheck reports healthy while `/<ns>/os_driver` is
**settled in `active` or `inactive` (standby) and ready to run** — in standby it additionally
probes the sensor's HTTP API, so a parked vehicle still surfaces a dead sensor. It never
measures data flow: a healthy standby instance produces nothing by design.

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

Beyond up/down, a running instance has two operational states, transitioned at runtime without
touching containers (the rig operational-state contract; declared in `rigging.yaml`):

```bash
./ouster-up sensors/ouster_top.yaml standby    # park it
./ouster-up sensors/ouster_top.yaml state      # -> {"state": "standby", "detail": "lifecycle:inactive"}
./ouster-up sensors/ouster_top.yaml activate   # wake it
```

- **`standby`** — the `os_driver` lifecycle node is deactivated to `inactive` (configured,
  metadata cached, not reading packets) **and** the sensor itself is put into its own `STANDBY`
  operating mode over its HTTP config API: motor stopped, laser off — the real power/heat win —
  UDP streaming stopped, HTTP API still answering. Both steps are needed because upstream's
  `on_deactivate` only stops the packet-reading thread; it leaves the sensor spinning.
- **`activate`** — restores `NORMAL` operating mode first and waits for the sensor to report
  `RUNNING` (re-init + spin-up, tens of seconds), then activates the lifecycle node. The
  driver's own init_id-change detection then performs one clean self-reset that refreshes
  metadata; `state` may briefly report `transitioning` while it runs.
- **`state`** — read-only probe: machine JSON on stdout (`active` | `standby` | `transitioning`
  | `down`; raw lifecycle state in `detail`), human chatter on stderr. Side-effect-free like
  `config`/`status`.
- Both transitions are idempotent (repeat = no-op success) and never create or destroy
  containers — on a down project they fail with a clear error (`state` reports `down`).

**Initial state:** a top-level `initial_state: standby | active` key in the sensor config
(absent = `active`) selects the state `up` leaves the instance in; the rig-owned
`RIG_TARGET_STATE` env, when set at `up`, overrides it ("bring the whole vehicle up parked"),
so precedence is `RIG_TARGET_STATE` > `initial_state` > `active`. Upstream's launch always
auto-activates, so `initial_state: standby` is up-then-park: expect a brief active blip, and use
a detached bring-up (`up -d`). Try it standalone:
`RIG_TARGET_STATE=standby ./ouster-up sensors/ouster_top.yaml up -d`.

Mission-layer software may also drive the node's ROS 2 lifecycle services directly (`standby` =
lifecycle `inactive`, `active` = `active`) without the launcher in the loop — that is expected;
just pair it with the sensor operating-mode restore the way `statectl.sh` does, or the sensor
stays in `STANDBY` and produces nothing.

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
