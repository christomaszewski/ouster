#!/usr/bin/env bash
# Run the pinned upstream regressions inside the Docker build stage. No sensor
# or external network is needed; the SDK receive tests use loopback sockets.
set -eo pipefail

workspace="${1:-/workspace}"
test_root="${workspace}/upstream-tests"
ros_source="${workspace}/src/ouster-ros/ouster-ros"
ros_build="${workspace}/build/ouster_ros"
sdk_source="${ros_source}/ouster-sdk"
build_jobs="${BUILD_JOBS:-2}"

# ROS environment hooks may reference unset variables, so enable nounset after
# sourcing them. The build stage already provides the interface package.
# shellcheck disable=SC1090,SC1091
source "/opt/ros/${ROS_DISTRO:-lyrical}/setup.bash"
# shellcheck disable=SC1090,SC1091
source "${workspace}/install/setup.bash"
set -u
export CMAKE_BUILD_PARALLEL_LEVEL="${build_jobs}"
# Also bound parallelism when the inherited tree uses Makefiles.
export MAKEFLAGS="-j${build_jobs}"
mkdir -p "${test_root}/results"

check_build_reuse() {
  python3 - "$1" "${ros_build}" "${ros_source}" "${workspace}/install" \
    "${test_root}/build-reuse.json" <<'PY'
import hashlib
import json
from pathlib import Path
import stat
import sys

phase, build, source, install, report = sys.argv[1:]
build, source, install, report = map(Path, (build, source, install, report))
cache = {}
for line in (build / "CMakeCache.txt").read_text().splitlines():
    if not line.startswith(("//", "#")) and ":" in line and "=" in line:
        key, value = line.split("=", 1)
        cache[key.split(":", 1)[0]] = value
if cache.get("CMAKE_BUILD_TYPE") != "Release":
    raise SystemExit("Regression reuse requires the production Release build")
if Path(cache.get("CMAKE_HOME_DIRECTORY", "")).resolve() != source.resolve():
    raise SystemExit("Production CMake cache points at a different source tree")
configuration = {key: value for key, value in cache.items()
                 if key.startswith(("CMAKE_C_FLAGS", "CMAKE_CXX_FLAGS"))
                 or key in ("CMAKE_BUILD_TYPE", "CMAKE_GENERATOR",
                            "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER",
                            "CMAKE_C_COMPILER_LAUNCHER", "CMAKE_CXX_COMPILER_LAUNCHER")}

def fingerprint(path):
    if path.is_symlink():
        return {"symlink": str(path.readlink())}
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(path.stat().st_mode)}

def installed_files():
    if not (install / "setup.bash").is_file():
        raise SystemExit("Production install is missing setup.bash")
    return {str(path.relative_to(install)): fingerprint(path)
            for path in sorted(install.rglob("*"))
            if path.is_symlink() or path.is_file()}

def object_file(path):
    # mtime catches recompilation even when ccache restores identical bytes.
    return dict(fingerprint(path), mtime_ns=path.stat().st_mtime_ns)

if phase == "snapshot":
    objects = {str(path.relative_to(build)): object_file(path)
               for path in sorted(build.rglob("*.o"))}
    if not objects:
        raise SystemExit("No production objects available for regression reuse")
    report.write_text(json.dumps({"configuration": configuration,
                                 "objects": objects,
                                 "install": installed_files()}, indent=2) + "\n")
elif phase == "verify":
    before = json.loads(report.read_text())
    if configuration != before["configuration"]:
        raise SystemExit("Regression build changed production compiler settings")
    rebuilt = [name for name, value in before["objects"].items()
               if not (build / name).is_file() or object_file(build / name) != value]
    if rebuilt:
        raise SystemExit(f"Production objects were rebuilt: {rebuilt[:5]}")
    after = installed_files()
    changed = sorted(name for name in before["install"].keys() | after.keys()
                     if before["install"].get(name) != after.get(name))
    if changed:
        raise SystemExit(f"Regression build changed production install: {changed[:5]}")
    before["verification"] = {"production_objects_reused": len(before["objects"]),
                              "production_install_unchanged": True}
    report.write_text(json.dumps(before, indent=2) + "\n")
    print(f"Reused {len(before['objects'])} production objects; install unchanged")
else:
    raise SystemExit(f"Unknown reuse check: {phase}")
PY
}

# This stage inherits the completed production build. Record its objects and
# install before compiling tests, so a lost reuse or accidental install fails CI.
check_build_reuse snapshot

run_gtest() {
  local binary="$1" name="$2"
  local result="${test_root}/results/${name}.xml"
  if [[ ! -x "${binary}" ]]; then
    echo "Required regression executable is missing: ${binary}" >&2
    return 1
  fi
  rm -f "${result}"
  "${binary}" '--gtest_filter=*' --gtest_output="xml:${result}"
  # A successful process with no registered cases is not a passing regression.
  python3 - "${result}" <<'PY'
import sys
import xml.etree.ElementTree as ET

result = ET.parse(sys.argv[1]).getroot()
executed = [case for case in result.iter("testcase")
            if case.get("status", "run") == "run"
            and case.find("skipped") is None]
if not executed:
    raise SystemExit(f"No regression cases executed: {sys.argv[1]}")
if int(result.get("failures", "0")) or int(result.get("errors", "0")):
    raise SystemExit(f"Regression failures: {sys.argv[1]}")
PY
}

# Compile the actual SDK receive implementation with ASan. Building the two
# targets explicitly fails if a source pin drops either regression.
cmake -S "${sdk_source}" -B "${test_root}/sdk-asan" -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_FLAGS="-fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address \
  -DBUILD_TESTING=ON -DOUSTER_INTERNAL_TESTS=OFF \
  -DBUILD_SENSOR=ON -DBUILD_EXAMPLES=OFF \
  -DBUILD_PCAP=OFF -DBUILD_OSF=OFF -DBUILD_VIZ=OFF -DBUILD_MAPPING=OFF
cmake --build "${test_root}/sdk-asan" \
  --target client_receive_test imu_packet_test --parallel "${build_jobs}"
run_gtest "${test_root}/sdk-asan/tests/client_receive_test" client_receive_test
run_gtest "${test_root}/sdk-asan/tests/imu_packet_test" imu_packet_test

# Enable tests in the inherited Release tree, preserving its cached generator,
# flags and compiler launchers. These tests remain uninstrumented; ASan uses the
# separate SDK tree above. Never install from this stage: runtime copies the
# original build stage's install, which this check also verifies stays unchanged.
cmake -S "${ros_source}" -B "${ros_build}" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build "${ros_build}" \
  --target ouster_ros_lidar_packet_handler_test --parallel "${build_jobs}"
run_gtest \
  "${ros_build}/ouster_ros_lidar_packet_handler_test" \
  ouster_ros_lidar_packet_handler_test

# Exercise the real processing component through middleware with a paused
# executor, then drain a complete packet burst and check every point/IMU value.
cmake --build "${ros_build}" \
  --target ouster_ros_raw_packet_qos_test --parallel "${build_jobs}"
RMW_IMPLEMENTATION=rmw_fastrtps_cpp run_gtest \
  "${ros_build}/ouster_ros_raw_packet_qos_test" \
  ouster_ros_raw_packet_qos_test

# Hold raw publication blocked while SDK UDP reception and actual sensor-node
# callbacks continue. Verify bounded overflow, byte ownership, independent IMU
# publication, exceptions, and worker shutdown/reactivation.
cmake --build "${ros_build}" \
  --target ouster_ros_raw_packet_publisher_test ouster_ros_raw_packet_sensor_test \
  --parallel "${build_jobs}"
run_gtest \
  "${ros_build}/ouster_ros_raw_packet_publisher_test" \
  ouster_ros_raw_packet_publisher_test
LD_LIBRARY_PATH="${ros_build}:${LD_LIBRARY_PATH:-}" \
  RMW_IMPLEMENTATION=rmw_fastrtps_cpp run_gtest \
  "${ros_build}/ouster_ros_raw_packet_sensor_test" \
  ouster_ros_raw_packet_sensor_test

# Check QoS upgrades for older bags, namespace remaps, and generated-profile
# cleanup. Explicitly reject an empty suite if a future pin removes these tests.
python3 - "${ros_source}/test" "${test_root}/results/replay_packet_launch_test.json" <<'PY'
import json
from pathlib import Path
import sys
import unittest

report_path = Path(sys.argv[2])
report_path.unlink(missing_ok=True)
suite = unittest.defaultTestLoader.discover(
    sys.argv[1], pattern="test_replay_packet_launch.py")
if not suite.countTestCases():
    raise SystemExit("No replay launch regressions found")
result = unittest.TextTestRunner(verbosity=2).run(suite)
report_path.write_text(json.dumps({
    "tests": result.testsRun, "failures": len(result.failures),
    "errors": len(result.errors), "skipped": len(result.skipped),
}) + "\n")
if not result.wasSuccessful() or result.testsRun == len(result.skipped):
    raise SystemExit("Replay launch regressions failed")
PY

check_build_reuse verify
