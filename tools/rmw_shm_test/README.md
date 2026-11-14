# Experimental CPU-group shared-memory serialization

This is a local `rmw_zenoh_cpp` experiment, outside the production build and runtime pins.
The patch applies to official [ros2/rmw_zenoh commit
4e17ccfb9e114c87ac87b802e705b60e283b4115](https://github.com/ros2/rmw_zenoh/tree/4e17ccfb9e114c87ac87b802e705b60e283b4115)
(tag `0.10.6`). It changes only the buffer-aware publisher's CPU-group endpoint.
`OUSTER_EXPERIMENT_CPU_GROUP_SHM=1` serializes into a Zenoh-owned shared-memory
allocation when available; the default `0` keeps the prior heap path. The variable
is read once per process. Compare **the same compiled library** with `0` and `1`.
Other endpoint groups and the ordinary publisher path are unchanged.

The SHM allocation remains owned by the transport until its readers release it.
Unavailable/undersized allocation falls back to heap. If serialization uses fewer
bytes than the SHM capacity, the patch clears the unused tail, copies only the
actual bytes into an owned heap allocation, and publishes that exact length.
This preserves the raw CDR length; publishing an oversized SHM buffer would not.

## Environment and source

Use a matching Linux ARM64 ROS Lyrical development container with CMake, GCC, Git,
Python/rclpy, `rclcpp`, `sensor_msgs`, and the development dependencies of
`rmw_zenoh_cpp` 0.10.6. The study used installed Debian package
`ros-lyrical-rmw-zenoh-cpp=0.10.6-1resolute.20260915.030033` and the retained wrapper
`upstream-tests` image ID
`sha256:6260860391baeaabf0048eb51e60f0f780148b9a1e86df395c11112698107467`.
That ID identifies a local artifact; it is not a published download location.
A corresponding development image can be built with this repository's
`docker/Dockerfile.runtime`, target `upstream-tests`, using the same immutable fleet
base as the runtime. Record the image ID and installed package versions. This
folder pins the middleware source, not every compiler/OS dependency; changed
binary dependencies require compatibility review and a fresh control comparison.

From the wrapper repository root, choose a **new** working directory:

```bash
repo_root="$PWD"
work="$repo_root/var/rmw-shm-reproduction"
mkdir -p "$work"
git clone https://github.com/ros2/rmw_zenoh.git "$work/source"
git -C "$work/source" checkout --detach 4e17ccfb9e114c87ac87b802e705b60e283b4115
git -C "$work/source" apply --check "$repo_root/tools/rmw_shm_test/cpu-group-shm.patch"
git -C "$work/source" apply "$repo_root/tools/rmw_shm_test/cpu-group-shm.patch"

# Select a locally available, matching development image by immutable ID.
test_image=sha256:6260860391baeaabf0048eb51e60f0f780148b9a1e86df395c11112698107467
docker image inspect "$test_image" > "$work/development-image.json"
for mode in candidate overestimate; do
  docker run --rm --platform linux/arm64 --network none \
    -v "$repo_root/tools/rmw_shm_test:/tools:ro" \
    -v "$work/source:/source:ro" -v "$work:/work" \
    --entrypoint /bin/bash "$test_image" \
    /tools/build.sh /source "/work/$mode" "$mode" > "$work/build-$mode.log" 2>&1
done
```

`build.sh` rejects a different source HEAD, a different tracked diff, untracked
source files, or an existing output directory. It builds without installing into
the container and records source/library checksums, compiler version, installed
middleware version, and resolved dependencies. `overestimate` adds 64 bytes to the
CPU-group size estimate in a disposable source copy only. It is a negative-path
fixture, never a production library or a runtime test switch.

## Four isolated local checks

Each invocation uses a fresh container with loopback only, domain 87, router port
17451 and a 256 MiB `/dev/shm`; do not use host networking. The publisher alone loads
the candidate library. Router and raw Python subscriber use the image's installed
middleware. The explicit router overrides disable multicast and external routes.
The scripts terminate their own children, bound the publisher run, and refuse
existing result directories.

```bash
for case in heap shm exhausted overestimate; do
  build=candidate
  mode=1
  if [ "$case" = heap ]; then mode=0; fi
  if [ "$case" = overestimate ]; then build=overestimate; fi
  docker run --rm --platform linux/arm64 --network none --shm-size 256m \
    -v "$repo_root/tools/rmw_shm_test:/tools:ro" -v "$work:/work" \
    --entrypoint /bin/bash "$test_image" \
    /tools/run-probe.sh "/work/$build" "/work/result-$case" "$case" "$mode" "$case"
done
```

The publisher emits 40 synthetic 12 MiB PointCloud2 payloads at about 10 Hz. The
subscriber requires the exact sequence, dimensions, XYZ field descriptors, payload
SHA-256 and raw serialized length **12,583,025 bytes**. The schema is deliberately
small (three XYZ descriptors with a 48-byte stride); this is not the Ouster point
schema or a live sensor fixture. The probe records `can_loan_messages=0`.

The checker also requires the intended branch: SHM allocations for `shm`, heap
for `heap` and the 1 MiB `exhausted` pool, and exact-length fallback for every SHM
allocation in `overestimate`. Discovery may cause initial messages to use heap;
there must be at least one verified SHM allocation in the two SHM scenarios.
The 2026-11-13 local run passed all four 40-frame checks. SHM became available after
the first message, producing 39 normal SHM allocations and 39 overestimate
fallbacks, respectively. That timing is an observation, not a fixed test assertion.

## Optional paired profiling images

After local correctness passes, build two isolated images from the **same candidate
output directory and same immutable runtime base**. Do not use the overestimate
library or replace a fleet image. These commands only build local images:

```bash
# Select the matching local runtime image, not the development image.
runtime_id=sha256:REPLACE_WITH_MATCHING_RUNTIME_IMAGE_ID
docker image inspect "$runtime_id" > "$work/runtime-base.json"
# BuildKit may resolve a bare image ID in FROM through a registry. Give the
# verified local image a content-derived tag and retain its inspected identity.
runtime_base="ouster-rmw-study-base:${runtime_id#sha256:}"
docker image tag "$runtime_id" "$runtime_base"
test "$(docker image inspect --format '{{.Id}}' "$runtime_base")" = "$runtime_id"
for mode in 0 1; do
  docker buildx build --platform linux/arm64 --load --pull=false --network none \
    --build-arg "BASE_IMAGE=$runtime_base" --build-arg "CPU_GROUP_SHM=$mode" \
    -f "$repo_root/tools/rmw_shm_test/Dockerfile" \
    -t "ouster-rmw-shm-study:$mode" "$work/candidate"
done

# Require both derived images to inherit every base layer and add only the
# replacement-library layer. This also detects an unintended base resolution.
docker image inspect "$runtime_base" ouster-rmw-shm-study:0 ouster-rmw-shm-study:1 \
  > "$work/runtime-images.json"
python3 - "$work/runtime-images.json" <<'PY'
import json, sys
base, *images = json.load(open(sys.argv[1]))
layers = base['RootFS']['Layers']
for image in images:
    assert image['RootFS']['Layers'][:-1] == layers
assert images[0]['RootFS']['Layers'] == images[1]['RootFS']['Layers']
PY
```

Before deployment, compare the embedded library SHA-256 in both images, inspect
`OUSTER_EXPERIMENT_CPU_GROUP_SHM`, and check dependencies after sourcing the ROS
setup. Keep sensor settings, source data, QoS, subscriber, and measurement method
fixed for an A/B test. The local probes do not establish live CPU savings,
long-duration stability, loss/reconnect behavior, compatibility with every message
type, remote transports, multiple endpoint groups, or exhausted-heap behavior.
They also do not establish end-to-end zero-copy: serialization still writes CDR,
and the subscriber still deserializes it. No binaries or raw run artifacts belong
in this folder; keep them under `var/`.
