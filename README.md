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
| `ouster-up` | The rig launcher contract (`up`/`down`/`status`/`logs`/`config`) over one sensor config. |
| `rigging.yaml` | Tells `rig` how to drive `ouster-up` (verbs, build, host ports, metadata volume). |

Upstream `os_driver` is a single `rclcpp_lifecycle` node; `driver.launch.py` auto-transitions it
configure → activate. The container healthcheck reports healthy once `/<ns>/os_driver` reaches the
`active` state.

## Layout

```
ouster/
├── ouster.repos              # upstream pin (vcstool)
├── rigging.yaml              # rig descriptor
├── ouster-up                 # rig launcher (networked; no serial branch)
├── justfile                  # dev tasks: vendor / build / image / run / replay / certify / dev
├── sensors/ouster.example.yaml
├── tools/
│   ├── render_params.py      # generic rig config -> ouster driver_params.yaml
│   └── build_image.sh        # rig build phase: build + push runtime image
├── docker/
│   ├── Dockerfile.runtime    # multi-stage: vcs import + rosdep + colcon (Release)
│   ├── Dockerfile.dev/.ci
│   ├── entrypoints/{ros-entrypoint,healthcheck}.sh
│   └── compose/{compose.deploy,compose.dev,compose.replay}.yaml
└── src/                      # GITIGNORED — upstream fetched here by `just vendor` / the build
```

## Quick start (local dev, no rig)

```bash
just vendor                              # vcs import upstream into src/ (gitignored)
just build                               # colcon build (needs a ROS 2 lyrical env)
#   …or build the deployable image instead:
just image                               # -> ouster_driver:latest

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

## Updating upstream

1. Bump `version:` in [`ouster.repos`](ouster.repos) to a newer upstream release tag.
2. Rebuild: `tools/build_image.sh <registry> [tag]` (or `just image`).
3. Skim upstream `CHANGELOG.rst` for renamed launch args / params.
4. Commit the `ouster.repos` change (e.g. `vendor ouster-ros 0.x.y`).

The only coupling to upstream is its launch CLI + param names. If `driver.launch.py` args
(`params_file`, `ouster_ns`, `viz`) or `driver_params.yaml` keys are renamed, update
`tools/render_params.py`, `docker/compose/compose.deploy.yaml`, and the healthcheck node name.

**LAN mirror (later):** change only `url:` in `ouster.repos` (keep `version:`). For a *fully* offline
build you also need an apt/rosdep mirror — the dependency install is a separate network dependency
from the source fetch.

## License

[Apache 2.0](LICENSE) for this wrapper. Upstream `ouster-ros` ships under its own license, fetched
with the source at build time.
