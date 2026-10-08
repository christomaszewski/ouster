# Docker images for the ouster wrapper

Two images over the same ROS 2 Lyrical base. Unlike the in-house drivers in this workspace, the
**upstream `ouster-ros` and the SDK fork are fetched at build time** from
[`../ouster.repos`](../ouster.repos) and [`../ouster-sdk.repos`](../ouster-sdk.repos).
The shared `tools/fetch_upstream.py` helper applies the SDK override and records both commits;
the runtime retains that record at `/opt/ouster_driver/share/upstream-sources.json`.

| Image | What | When |
|---|---|---|
| `Dockerfile.runtime` | Multi-stage. Fetches + builds the pinned upstream (Release), then ships only `install/` on `BASE_IMAGE` (standalone default `ros:lyrical-ros-core`; under rig, the deployment's shared fleet-ros base) with exec-only deps (resolved by rosdep). No compilers. | Production deploy + `rig`; CI builds it as the compile gate. |
| `Dockerfile.dev` | Full toolchain + vcstool + rosdep + rviz2 + rosbag2, non-root user matched to the host UID/GID. | Day-to-day dev + replay; also drives `.devcontainer.json`. |

The runtime image resolves deps with `rosdep` (from the vendored `package.xml`) rather than a
hand-maintained apt list, so it tracks upstream automatically — with two slimming carve-outs:
apt Recommends are off stage-wide, and `pcl_conversions`/`cv_bridge` are skipped at exec time
because the driver uses them header-only (their debs hard-depend on `libpcl-dev`/`libopencv-dev`,
which would drag ~5 GB of compilers, VTK, Java and Boost -dev into the image); the three OpenCV
runtime libs the binaries do link are derived from cv_bridge's own deb metadata at build time.
Optional SDK features (pcap/osf/viz/mapping) are built OFF, keeping the exec-only runtime
correct and slim.
Where upstream's manifests under-declare (libzip: linked unconditionally by `ouster_client` at
0.15.1 but declared build-only), [`runtime_extra_deps/package.xml`](runtime_extra_deps/package.xml)
patches the gap through the same rosdep pass. The image build checks missing libraries with
`ldd`, then eagerly loads the ROS libraries with `check_runtime.py` to catch undefined symbols
in generated type support too. A matching library filename alone does not establish ABI
compatibility. This matters under `rmw_zenoh_cpp` as well: it uses the Fast RTPS type-support
serialization callbacks, even though the transport is Zenoh.

## Build the runtime image

```bash
tools/build_image.sh <registry> [tag]   # build + push to the fleet registry (rig's build phase)
docker build -f docker/Dockerfile.runtime -t ouster_driver:latest .   # local build, no push
# Jetson / arm64 (build on an arm64 host — qemu is painfully slow for the C++/PCL compile):
docker buildx build --platform linux/arm64 -f docker/Dockerfile.runtime -t ouster_driver:jp7 .
```

Under rig, `build_image.sh` follows the rig build-env contract: `RIG_BASE_IMAGE` re-parents the
build and runtime stages onto the deployment's shared base (fleet-ros), `ROS_DISTRO` forwards the fleet
distro, and `RIG_BUILD_NO_CACHE` maps to `--no-cache --pull` — the deliberate way the parent
image (the fleet's ros-* version authority) advances. Both stages use `apt-mark hold` for the
parent's ROS packages: `APT::Get::Upgrade "false"` alone does not prevent dependency-driven
upgrades. A dependency requiring newer held ROS packages fails the build; refresh the fleet
base deliberately in that case. The final check also compares installed ROS packages against
`/opt/ouster_driver/share/build-ros-packages.tsv`, captured from the compiler environment.
`rig image audit` checks agreement between final images and source provenance; it does not
check the discarded compiler environment or resolve the symbols inside custom libraries.

## Dev container

```bash
docker compose -f docker/compose/compose.dev.yaml up -d
docker compose -f docker/compose/compose.dev.yaml exec dev bash
# inside the container:
python3 tools/fetch_upstream.py src                  # ROS + patched SDK; fresh checkout only
rosdep install --from-paths src --ignore-src -y        # resolve upstream deps
colcon build --base-paths src --merge-install \
  --cmake-args -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="-Wno-deprecated-declarations" \
    -DCMAKE_PROJECT_ouster_ros_INCLUDE="$PWD/docker/ament_target_dependencies_compat.cmake"
```

Or via VS Code: `F1` → "Dev Containers: Reopen in Container" (uses `.devcontainer.json`).

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

## RMW selection / Zenoh

Both images bake two RMWs: `rmw_fastrtps_cpp` (the default) and `rmw_zenoh_cpp`. Every compose
file passes `RMW_IMPLEMENTATION` through, so switching is an env var — no rebuild:

```bash
RMW_IMPLEMENTATION=rmw_zenoh_cpp ./ouster-up sensors/ouster_top.yaml up -d
```

**The driver never starts a Zenoh router.** The router is host-level infrastructure — under rig
it is a rig-managed infrastructure service, exactly like the registry; bringing this service up
or down never touches it. Each driver instance only opens a *session* that connects out to the
existing router — by default at zenoh's `tcp/localhost:7447`, which host networking makes
reachable. If the rig-managed router listens elsewhere, point the session at it with the
rig-injectable override the deploy compose passes through (no rebuild, no compose edit):

```bash
ZENOH_CONFIG_OVERRIDE='connect/endpoints=["tcp/192.168.1.1:7447"]' \
RMW_IMPLEMENTATION=rmw_zenoh_cpp ./ouster-up sensors/ouster_top.yaml up -d
```

rmw_zenoh applies the `path=json5;…` pairs **on top of** whatever session config it loads (its
shipped ROS default, or a deployment-set `ZENOH_SESSION_CONFIG_URI` file), so untouched keys
keep their loaded values. A bad pair is a WARN in the driver log ("Ignore the invalid
configuration key-value pair") and is ignored — it cannot hard-fail the driver, so check the
log after config changes.

**Session tuning / shared memory (opt-in).** The sensor config's `zenoh:` block (see the
commented catalog entry in [`../sensors/ouster.example.yaml`](../sensors/ouster.example.yaml))
is rendered by the launcher into the same mechanism: one `ZENOH_CONFIG_OVERRIDE` pair string
(`shared_memory`/`shm_pool_mb` sugar plus a generic `overrides:` subtree, flattened to leaf
pairs) that the entrypoint appends to any container-level `ZENOH_CONFIG_OVERRIDE` — appended
*after* it, so the config's pairs win on key conflict — and echoes to the log, only under
`rmw_zenoh_cpp`. No session-config file is written or replaced, and the knob composes with a
deployment-set `ZENOH_SESSION_CONFIG_URI` (the pairs apply on top of that file too; before
v0.1.3 a pre-set URI skipped the knob entirely). rmw_zenoh ships SHM support compiled in but
disabled; SHM engages per link, only where the subscriber's session also enables it and both
containers share the host IPC namespace (`ipc: host`); anything unmet falls back to TCP
loopback **silently** — verify by watching `lo` traffic, not config. The driver publishes the
big `/<ns>/points` clouds, so this is the side where SHM pays off; size `shm_pool_mb` ≥ QoS
depth × cloud size (256 recommended — the 48 MB default holds only ~8–16 expanded clouds, and
a full pool falls back to TCP per message). Never set the `ZENOH_SHM_ALLOC_SIZE` env — it
aliases the pool-size key and rmw inserts it *after* `ZENOH_CONFIG_OVERRIDE`, silently
overriding the knob. Inert under Fast DDS.

Zenoh also **locks** its SHM mappings. Compose raises `memlock` for the driver; subscribers
that map its pools need the same allowance, even if their own publishing pool is small.
`ipc: host` supplies shared `/dev/shm` but does not raise locked-memory limits. With Docker's
8 MiB default, a 256 MiB pool fails with `Unable to create POSIX shm segment: OS error 12`
even when `/dev/shm` has ample space. Use `ulimits: {memlock: {soft: -1, hard: -1}}` for each
SHM participant (or an explicitly budgeted limit covering its own and peer mappings).

**Rig-less dev only:** with no rig to provide the router, the runtime image doubles as the router
image since it ships `rmw_zenoh_cpp` — disable the baked healthcheck (it probes the supervisor's
`os_driver` node, which is meaningless for a router and would leave the container permanently
unhealthy):

```bash
docker run -d --restart unless-stopped --network host --no-healthcheck \
  --name zenoh_router ouster_driver:latest ros2 run rmw_zenoh_cpp rmw_zenohd
```

The driver's own healthcheck inherits the same `RMW_IMPLEMENTATION` and zenoh config, so health
tracks the RMW actually in use — with the router down or unreachable the driver reports
unhealthy, by design.

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
./ouster-up sensors/ouster_top.yaml standby   # park: driver stopped + sensor STANDBY mode
./ouster-up sensors/ouster_top.yaml activate  # wake: sensor NORMAL + driver active
./ouster-up sensors/ouster_top.yaml state     # {"state": ...} JSON on stdout (read-only)
./ouster-up sensors/ouster_top.yaml down
```

Each sensor becomes its own compose project (the rig-injected `COMPOSE_PROJECT_NAME`, or
`ouster_<name>` standalone) under ROS namespace `/<name>`, so multiple instances never collide.
Needs the Docker Compose v2 plugin and host PyYAML (`apt install python3-yaml`).

The runtime supervisor keeps the container running in standby and stops the ROS driver
process. Use the launcher state verbs rather than direct ROS lifecycle commands. Standby
startup does not briefly wake the sensor. See the root README for state/restart semantics.
Bare runtime `docker run` invocations must use `--init` and mount the sensor params file at
`/etc/ouster_driver/params.yaml`; compose supplies both automatically.

Container shutdown parks the sensor by default (`shutdown_state: standby` in the sensor
YAML, passed as `OUSTER_SHUTDOWN_STATE`). `unchanged` opts out of the sensor mode change.
Compose's 120-second stop grace covers lifecycle cleanup, process reaping, and standby
confirmation. For bare `docker run`, set `--stop-timeout 120` too. The policy applies to
Docker restarts as well as `rig down`; shutdown never replaces the saved startup target.

| File | Role |
|------|------|
| `ouster-up` | Per-sensor launcher (verbs up/down/status/logs/config + standby/activate/state; forwards extra args to compose). |
| `tools/render_params.py` | Generic config -> upstream ROS 2 params (`/**`-keyed); `--env` emits the instance identity. |
| `tools/build_image.sh` | Build + push the runtime image: `build_image.sh <registry> [tag]` (rig's `build:` entrypoint). |
| `sensors/ouster.example.yaml` | Example sensor config (copy + edit per instance; CI certifies against it). |
| `docker/compose/compose.deploy.yaml` | Deploy compose: host net/ipc, params bind-mount, metadata volume, `driver.launch.py` + `ouster_ns` + `viz:=false`. |
| `rigging.yaml` | rig descriptor: service / launcher / verbs / build phase / host_ports / external_volumes (metadata only). |

At deploy time the compose resolves the image as `OUSTER_IMAGE` (full per-service override) ->
`RIG_IMAGE_REGISTRY`-prefixed `ouster_driver:${RIG_IMAGE_TAG:-latest}` (rig injects both from fleet
policy; `rig build` pushes the same ref) -> bare local `ouster_driver:latest`.

The launcher contract is executable: `rig certify --repo . --config sensors/ouster.example.yaml`
— run it from a sibling `rig` checkout (e.g. `../bringup/rig certify …`); CI checks out the rig
repo and runs it on every push.
