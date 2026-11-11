# ouster — rig-integrated wrapper for the official Ouster ROS 2 driver

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A thin, rig-compatible packaging of the upstream
[`ouster-ros`](https://github.com/ouster-lidar/ouster-ros) lidar driver (the `ros2` lineage),
targeting **ROS 2 Lyrical** and the in-house **rig** orchestrator.

This repo packages the official driver with narrowly scoped fixes maintained in our ROS and SDK
forks. Exact source commits are fetched at build time; C++ sources are not vendored here. The
service provides the rig-integration shell: a launcher, a params mapper, a Python operational-state
supervisor, Docker, compose, and the `rig` descriptor.

## How it works

| Piece | Role |
|-------|------|
| `ouster.repos` | Pins our ROS 0.15.1-based fork with per-scan timestamp ownership and regression tests. |
| `ouster-sdk.repos` | Pins the ROS-compatible SDK fork containing legacy IMU bounds and UDP receive-buffer fixes. |
| `tools/fetch_upstream.py` | Fetches both pins into a fresh checkout (or takes rig's exports of them), replaces the bundled SDK, and records their actual revisions and the fleet-msgs provenance. |
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
├── ouster.repos              # ROS driver pin: our fork, full commit SHA
├── ouster-sdk.repos          # explicit SDK override (full commit SHA)
├── rigging.yaml              # rig descriptor
├── ouster-up                 # rig launcher (networked; no serial branch)
├── sensors/ouster.example.yaml
├── tools/
│   ├── render_params.py      # generic rig config -> ouster driver_params.yaml
│   ├── fetch_upstream.py     # fetch both pins (or take rig's exports), record what was built
│   ├── runtime_images.py     # launcher helper: the compose model's images and rig's digest pins
│   ├── build_image.sh        # rig build phase: build + push runtime image
│   ├── rig-build.sh          # rig build contract helper sourced by build_image.sh
│   ├── test_build_contract.py  # build_image.sh's docker arguments, checked without a daemon
│   └── test_upstream.sh      # upstream regressions (the Dockerfile's upstream-tests target)
├── docker/
│   ├── Dockerfile.runtime    # multi-stage: fetch_upstream.py + rosdep + colcon (Release)
│   ├── Dockerfile.dev        # dev toolchain; .devcontainer.json mirrors it
│   ├── check_runtime.py      # image-build gate: build/runtime ROS packages agree, libraries load
│   ├── ament_target_dependencies_compat.cmake  # Lyrical shim for upstream's CMake
│   ├── runtime_extra_deps/package.xml          # exec deps upstream's manifests leave out
│   ├── entrypoints/{ros-entrypoint,healthcheck,statectl}.sh
│   ├── entrypoints/{operational_state,sensor_http}.py   # supervisor and its sensor HTTP client
│   ├── entrypoints/{health,health_ros,lifecycle_ros}.py # diagnostics, the health node, lifecycle calls
│   └── compose/{compose.deploy,compose.dev,compose.replay}.yaml
├── docs/SERVICE_HEALTH.md    # sensor and service health specification shared with the dashboard
├── tests/                    # python3 -m unittest discover -s tests (ROS parts run in the image)
└── src/                      # GITIGNORED — upstream fetched here by tools/fetch_upstream.py
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

## Portable Rig builds and replay

With Rig v0.2.61 or later, `rig build` publishes `ouster_driver` for both
`linux/arm64` and `linux/amd64` under one tag. The driver uses CPU code and needs no
`jp6m`, `jp7`, or `dev` runtime variants. For a faster single-architecture build,
use `rig build --arch arm64 --tag <new-tag>` (or `amd64`). Use a new tag when
narrowing a previously published multiarch release.

Standalone builds remain host-native by default:

```bash
tools/build_image.sh ghcr.io/your-org mytag
RIG_BUILD_PLATFORMS=linux/arm64,linux/amd64 tools/build_image.sh ghcr.io/your-org mytag
```

Multiarch publication needs a Docker Buildx builder supporting both architectures
and registry push access. Native build nodes are preferable for the C++/PCL build;
emulation is slower. `OUSTER_BUILD_PLATFORM=linux/<arch>` still selects one
architecture for standalone builds; Rig's `RIG_BUILD_PLATFORMS` takes precedence.
`RIG_BUILD_NO_CACHE=1` passes `--no-cache --pull` on both build paths, and the build
reports the published digest through `RIG_BUILD_RESULT`. Rig uses `OUSTER_IMAGE`
to select the catalog image by digest during deployment and reconstruction.
A `--no-cache` build runs every step again, from empty cache mounts, and changes no build
argument, so the ordinary build after it reuses exactly those refreshed layers. See
[docker/README.md](docker/README.md#build-the-runtime-image) for the cache mounts and for
`OUSTER_DISABLE_CCACHE=1`.

With Rig v0.2.74 or later the image build clones nothing itself. The rigging's
`build.sources` declares the two pinned repositories, rig serves them from its local git
mirrors, and `build_image.sh` passes them to the Dockerfile as named build contexts. To build
without a network, mirror them once while online, then build offline:

```bash
rig src sync
rig build --offline
```

Image layers still come from the build cache, so an offline build needs one earlier online build
of the same apt and rosdep steps. The Dockerfile names no `# syntax=` frontend, so a cached build
resolves nothing on Docker Hub either. Under rig the SDK's build header reports its git hash as
`unknown` because exported trees carry no `.git`. The exact commits remain in
`/opt/ouster_driver/share/upstream-sources.json` and `/opt/fleet-msgs/provenance.yaml`.

Ordinary Rig bag replay republishes the recorded Ouster topics through the bag
player and does not start this sensor-tier driver. `rigging.yaml` declares that as
`replay: {start: false}`: replay plans list the instance as omitted with its bag topics
still eligible, and `rig build --scope replay` no longer builds this image for every replay
architecture. Selecting the live driver explicitly (an instance name, `--live`,
`--include-service ouster`, or a version-2 recipe's `session.instances`) still works and
still requires a reachable sensor; build the image for that host with
`rig build ouster --arch <cpu>`. The separate raw-packet replay
compose workflow remains available for regenerating points from packets.

CI builds the runtime on native arm64 and amd64 runners and runs
`rig certify --repo . --config sensors/ouster.example.yaml --require-build-check`
to verify the launcher and no-cache build contract without starting a sensor.

## Configuration

One generic rig config per sensor instance (see `sensors/ouster.example.yaml`):

- `connection.type: lidar` with `connection.lidar.{sensor_hostname, udp_dest, lidar_port, imu_port}`
  — mapped onto the upstream params by `render_params.py`. `sensor_hostname` is required: the
  launcher refuses `up` and `config` without it, before any container is created.
- `ros.namespace` — becomes `ouster_ns` (so topics are `/<ns>/points`, `/<ns>/imu`, …).
  It defaults to the instance name, which is also the namespace rig assumes when it associates
  topics with instances (`rig graph`, replay topic ownership). A per-config namespace cannot be
  declared in `rigging.yaml`, so when you set a different one, give replay recipes
  `instances.<name>.recorded_namespace: /<ns>`.
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
sample, and the `temperature` component on `/diagnostics` says why (`measurement unavailable`,
or `health.error` while the request is failing); polling continues without affecting the
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
  It exits 0 with one JSON object whatever the project is doing. When a container is running
  but its supervisor does not answer (starting, stopping or hung), the launcher answers
  `{"state": "transitioning", "detail": "supervisor unreachable"}`; read it next to the
  container health, which asks the same socket. A listed container that is not running, for
  example one in a restart loop, is `down` with `container <status>` as its detail.
- Requests are serialized and idempotent. If another transition remains busy for 20 seconds,
  the command fails with an explicit retry message. A failed accepted request retains its
  target; the supervisor retries recovery every five seconds. The healthcheck and `state`
  are read-only. Sensor mode writes never intentionally persist config to flash.
- `standby` and `activate` wait up to 372 seconds for the supervisor's reply. That is 30
  seconds more than the slowest request the supervisor still answers, an activation that uses
  every budget and then fails: 20 waiting for a transition in progress, 37 observing, 31
  stopping the old driver, 120 for the sensor to reach NORMAL, 103 for the driver to become
  active, and 31 stopping it again. A success that uses every budget answers after about 308
  seconds; with the previous 300-second wait the client could report it as a timeout. The
  numbers are named constants in `operational_state.py`, and the wait is derived from them.
  For scale, on an OS-0-128 (firmware 2.5.3) and a Jetson Orin a change to standby took about
  10 seconds, a change to active 35 to 38, and `up` from a parked sensor about 43.
- Each five-second observation is two sensor HTTP requests and one lifecycle request. While the
  target is `active` and the driver process is alive, an observation that gets no answer (one
  of those requests timing out or failing in transit) is repeated twice, two seconds apart,
  before the driver is stopped, so one lost reply does not cost a driver restart. An answer
  that contradicts the target, or a driver process that exited, starts recovery at once.
  Nothing is written to the sensor while the supervisor is asking again. On an OS-0-128
  (firmware 2.5.3) driven from a Jetson Orin, a driver process frozen for 8 seconds was asked
  again once and left running, and one frozen for good was replaced and active again 43 seconds
  after it stopped answering, about 14 of them spent asking again. No observation went
  unanswered on its own in a 16-minute run there, so how often the repeats
  (`PROBE_RETRIES`, `PROBE_RETRY_PAUSE`) are needed in the field is still unmeasured.

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
The budget is judged against a fresh state probe: a transition request that is still running
when it ends is allowed to finish, and counts as a success if the instance then reports the
requested state.

Runtime requests are saved in `/var/lib/ouster/target-state` inside the container, so a Docker
restart retains the latest request. Recreating the container uses the configured initial state.
The file is synced to disk before it replaces the previous one. If it is unreadable or holds
anything but `active` or `standby` (for example after a power cut), the supervisor logs a
warning and starts with the configured initial state instead of failing every restart.
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
kill therefore makes `rig down` return nonzero. If the stop command itself fails, the launcher
reports that and still removes the container. Direct `docker compose down` bypasses that
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

The stream sampling test (`tests/test_health_stream_ros.py`) runs in the image under its
default RMW and again under `rmw_zenoh_cpp`, the RMW deployments use, through a router it starts
on a loopback port of its own.

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

1. Rebase the ROS fixes onto a compatible upstream release, run their regressions, and publish
   the tested commit to our fork. Update `url:` and `version:` in [`ouster.repos`](ouster.repos)
   to that repository and full commit SHA. Retain a branch containing every pinned commit.
2. Update both `msgs.source.repo` and `msgs.source.ref` in [`rigging.yaml`](rigging.yaml) to match — they pin
   `ouster_sensor_msgs` (same repo) for the deployment's fleet-ros-msgs overlay; a drifted pin
   means silently skewed schemas in recorded bags. Update the matching `build.sources` entry
   too (and the `ouster-sdk` one when `ouster-sdk.repos` changes): rig exports exactly those
   refs, and `build_image.sh` refuses an export that differs from the manifests.
3. Run the upstream regression target before rebuilding:
   `docker build --target upstream-tests -f docker/Dockerfile.runtime .`.
   Rebuild: `tools/build_image.sh <registry> [tag]` (or, local-only:
   `docker build -f docker/Dockerfile.runtime -t ouster_driver:latest .`).
4. Skim upstream `CHANGELOG.rst` for renamed launch args / params.
5. Commit the manifests and message-source declaration together.

The ROS fork keeps each completed scan's sensor timestamp and ROS message timestamp in the same
queue slot as its data. Its regression blocks the consumer while subsequent scans complete and
checks scan/header/per-point timing across the supported timestamp modes. Complete 128-beam,
2048-column scans also check dimensions and frame-specific range values while queued, using
sensor and PTP time. These tests do not cover the separate upstream complete-frame origin
calculation in `TIME_FROM_ROS_TIME`. Keep this patch until an upstream release provides
equivalent ownership and regression coverage.

Raw packet publishers and subscribers use an explicit reliable, volatile KeepLast
profile, with `raw_packet_qos_depth: 1024` and `raw_packet_qos_reliability: reliable`
by default. These startup parameters are independent of processed-output QoS. The
replay launch supplies matching rosbag QoS overrides for old recordings and custom
namespaces. The burst regression pauses the actual cloud component's executor,
queues 0.3 seconds of maximum-rate legacy packets, then checks every point's range
and relative timestamp plus each IMU timestamp. Both `2048x10` and `1024x20` are
covered. Live raw publication uses separate lidar and IMU workers with preallocated
queues (`raw_packet_queue_depth: 1024` per stream, independent of DDS depth). A slow
raw subscriber can fill that queue and cause counted raw packet drops, but cannot
block UDP reception through the publish call. Regressions block publication while
checking actual sensor callbacks, SDK loopback reception, packet ownership, queue
overflow, independent IMU publication, and shutdown/reactivation. See
[replay notes](docker/README.md#replay-no-hardware) for capacity and timing limits.

The SDK is independently pinned in [`ouster-sdk.repos`](ouster-sdk.repos). Our fork starts at
upstream `sdk-0.16.2/ouster-ros-0.15.0`, backports the legacy IMU bounds correction from SDK 1.0,
and receives the oversized-datagram detection byte into separate scratch storage. The UDP fix
preserves packet lengths and oversized-packet rejection without requiring spare capacity in
callers' buffers, including buffers exchanged by raw ROS publication. ASan regressions cover
48-byte legacy IMU packets and both 64- and 128-beam legacy lidar packets, including malformed
lengths, recovery, and preserving buffers already handed to raw publication. The SDK API remains
compatible with this ROS driver. An SDK-only update does not change
`ouster_sensor_msgs`, so leave the two ROS source references matched to each other.
The legacy IMU backport is submitted as [Ouster SDK PR #727](https://github.com/ouster-lidar/ouster-sdk/pull/727).

The fork reads the optional mask image (`mask_path`) with a bundled single-header decoder
instead of OpenCV, which the driver used for nothing else, so neither the build nor the runtime
image installs OpenCV. PNG, JPEG, BMP, GIF, TGA, PSD and binary PGM/PPM masks are accepted; TIFF
and WebP, which only OpenCV could read, are refused with a load error that lists the supported
formats, and the service then reports `transitioning` while the driver keeps failing to start.
The fork's tests decode each format and fit a mask to the scan; the regression target runs them.
Keep this patch until upstream no longer needs OpenCV.

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

**LAN mirror:** under rig, `rig src sync` keeps both source pins in rig's local git mirrors (see
[Portable Rig builds and replay](#portable-rig-builds-and-replay)). For standalone builds, change
only `url:` in the manifests (keep `version:`). A *fully* offline build from a cold cache also
needs an apt/rosdep mirror: the dependency install is a separate network dependency from the
source fetch.

## Releasing

The service is published in the public rig registry as `public/ouster`
([rig-registry-public](https://github.com/christomaszewski/rig-registry-public)). To cut a release:

```bash
gh release create v0.4.0 --generate-notes    # tag + GitHub release in one step
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

Stream health samples the driver's `telemetry` topic once per heartbeat instead of handling
every message. The driver publishes one message per lidar packet (640 to 1,280 per second); the
health node keeps only the newest in a depth-1 subscription that its executor never waits on,
and each heartbeat takes it without deserializing. Measured in the runtime image on a 2.3 GHz
x86-64 core with Fast DDS, a Python callback per packet cost 20% of a core at 640 packets/s and
32% at 1,280; sampling costs 2% and 3%. With `rmw_zenoh_cpp` and a router the callback cost 22%
and 39% and sampling costs 5% and 9%: that middleware still receives every message in the
process, which only dropping the subscription would avoid. `packets.received` therefore counts
heartbeats that found a new packet, not packets, and a stopped stream is reported one heartbeat
later at most.

`alerts.history_gap` turns true when alert history may have been lost, and stays true until the
supervisor restarts: the sensor's identity changed outside a mode change (a reboot, a power
cycle, another client reinitializing it, or the running driver resetting the sensor on its own,
as it does after sustained packet errors and for its `reset` and `set_config` services), the
alert cursor went backwards, or events were skipped between two polls. The supervisor's own mode
changes are not such a loss. Each one reinitializes the sensor and changes its
`initialization_id`, as does the driver when it applies a different config while it is being
activated; the supervisor knows which reinitializations belong to its transitions and reports
only the others to the health reporter. After one of its own, a cursor that carried on is
checked for skipped events as usual and keeps its `alerts.epoch`; a cursor that started again
from zero gets a new `alerts.epoch` without a gap. Polling pauses during a mode change, so
alerts the sensor logs inside one are not followed. Seen on an OS-0-128 with firmware 2.5.3:
`initialization_id` rises on every reinitialization (by two for a change to standby, by one for
a change to normal or an outside `reinitialize`), and the alert cursor carries on across all of
them, so a reinitialization by itself lost no alert history there. A reboot or power cycle was
not tried.

Export `VEHICLE_ID` for fleet identity. Optional sensor YAML `health:` configures `interval_s`
(default 1), `poll_interval_s` (5), `stale_after_s` (15), and `limits`. To reach the dashboard,
use the deployment's rmw_zenoh router or supported DDS bridge. See the complete
[shared health specification](docs/SERVICE_HEALTH.md) for adoption, alert history,
configuration, tests and remaining hardware validation.
