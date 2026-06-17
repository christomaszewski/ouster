# Docker images for the ouster wrapper

Three images over the same ROS 2 Lyrical base. Unlike the in-house drivers in this workspace, the
**upstream `ouster-ros` source is fetched at build time** (`vcs import` from
[`../ouster.repos`](../ouster.repos)) — nothing upstream is committed here.

| Image | What | When |
|---|---|---|
| `Dockerfile.runtime` | Multi-stage. Fetches + builds the pinned upstream (Release), then ships only `install/` on `ros:lyrical-ros-core` with exec-only deps (resolved by rosdep). No compilers. | Production deploy + `rig`. |
| `Dockerfile.dev` | Full toolchain + vcstool + rosdep + rviz2 + rosbag2, non-root user matched to the host UID/GID. | Day-to-day dev; also drives `.devcontainer.json`. |
| `Dockerfile.ci` | Slim CI runner: toolchain + vcstool + rosdep, root, no GUI. | CI: vendor → rosdep → build → `rig certify`. |

The runtime image resolves deps with `rosdep` (from the vendored `package.xml`) rather than a
hand-maintained apt list, so it tracks upstream automatically. Optional SDK features
(pcap/osf/viz/mapping) are built OFF, keeping the exec-only runtime correct and slim.

## Build the runtime image

```bash
tools/build_image.sh <registry> [tag]   # build + push to the fleet registry (rig's build phase)
just image                              # local build -> ouster_driver:latest
# Jetson / arm64 (build on an arm64 host — qemu is painfully slow for the C++/PCL compile):
docker buildx build --platform linux/arm64 -f docker/Dockerfile.runtime -t ouster_driver:jp7 .
```

## Dev container

```bash
docker compose -f docker/compose/compose.dev.yaml up -d
docker compose -f docker/compose/compose.dev.yaml exec dev bash
# inside the container:
just vendor                                            # vcs import upstream into src/
rosdep install --from-paths src --ignore-src -y        # resolve upstream deps
just build                                             # colcon build (Release)
```

Or via VS Code: `F1` → "Dev Containers: Reopen in Container".

## Replay (no hardware)

Replays a recorded rosbag2 of **raw** Ouster packet topics (`lidar_packets` / `imu_packets` /
`metadata`, as captured by upstream's `record.launch.xml`) through the real `os_cloud`/`os_image`
processing — no decoder of ours involved — and opens rviz2:

```bash
OUSTER_DATA_DIR=/path/to/bags \
OUSTER_REPLAY_BAG=my_capture \
OUSTER_REPLAY_METADATA=my_capture_metadata.json \
  docker compose -f docker/compose/compose.replay.yaml up
```

## Deployment / `rig` integration

This driver plugs into the vehicle-level `rig` orchestrator as a first-class service — one-way: the
driver never depends on or knows about rig. Per-sensor deployment is driven by one generic config
(start from [`../sensors/ouster.example.yaml`](../sensors/ouster.example.yaml)):

```yaml
service: ouster
name: top
connection:
  type: lidar
  lidar: { sensor_hostname: 192.168.1.50, udp_dest: 192.168.1.10, lidar_port: 7502, imu_port: 7503 }
ros: { namespace: top }
driver_params: {}          # OPAQUE -> passed verbatim into ros__parameters
```

Bring it up with the launcher (it *selects + parameterizes* the static compose file — never
generates one):

```bash
./ouster-up sensors/ouster_top.yaml up -d     # detached
./ouster-up sensors/ouster_top.yaml status    # docker compose ps
./ouster-up sensors/ouster_top.yaml logs -f
./ouster-up sensors/ouster_top.yaml config    # render the merged compose (no run)
./ouster-up sensors/ouster_top.yaml down
```

Each sensor becomes its own compose project (the rig-injected `COMPOSE_PROJECT_NAME`, or
`ouster_<name>` standalone) under ROS namespace `/<name>`, so multiple instances never collide.
Needs the Docker Compose v2 plugin and host PyYAML (`apt install python3-yaml`).

| File | Role |
|------|------|
| `ouster-up` | Per-sensor launcher (verbs up/down/status/logs/config; forwards extra args to compose). |
| `tools/render_params.py` | Generic config -> upstream ROS 2 params (`/**`-keyed); `--env` emits the instance identity. |
| `tools/build_image.sh` | Build + push the runtime image: `build_image.sh <registry> [tag]` (rig's `build:` entrypoint). |
| `sensors/ouster.example.yaml` | Example sensor config (copy + edit per instance; CI certifies against it). |
| `docker/compose/compose.deploy.yaml` | Deploy compose: host net/ipc, params bind-mount, metadata volume, `driver.launch.py` + `ouster_ns` + `viz:=false`. |
| `rigging.yaml` | rig descriptor: service / launcher / verbs / build phase / host_ports / external_volumes (metadata only). |

At deploy time the compose resolves the image as `OUSTER_IMAGE` (full per-service override) ->
`RIG_IMAGE_REGISTRY`-prefixed `ouster_driver:${RIG_IMAGE_TAG:-latest}` (rig injects both from fleet
policy; `rig build` pushes the same ref) -> bare local `ouster_driver:latest`.

The launcher contract is executable: `rig certify --repo . --config sensors/ouster.example.yaml`
(CI runs it on every push).
