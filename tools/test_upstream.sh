#!/usr/bin/env bash
# Run the pinned upstream regressions inside the Docker build stage. No sensor
# or external network is needed; the SDK receive tests use loopback sockets.
set -eo pipefail

workspace="${1:-/workspace}"
test_root="${workspace}/upstream-tests"
ros_source="${workspace}/src/ouster-ros/ouster-ros"
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
# colcon supplies make's CPU-count default unless MAKEFLAGS is set.
export MAKEFLAGS="-j${build_jobs}"
mkdir -p "${test_root}/results"

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
  -DCMAKE_CXX_FLAGS="-fsanitize=address -fno-omit-frame-pointer" \
  -DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address \
  -DBUILD_TESTING=ON -DOUSTER_INTERNAL_TESTS=OFF \
  -DBUILD_SENSOR=ON -DBUILD_EXAMPLES=OFF \
  -DBUILD_PCAP=OFF -DBUILD_OSF=OFF -DBUILD_VIZ=OFF -DBUILD_MAPPING=OFF
cmake --build "${test_root}/sdk-asan" \
  --target client_receive_test imu_packet_test --parallel "${build_jobs}"
run_gtest "${test_root}/sdk-asan/tests/client_receive_test" client_receive_test
run_gtest "${test_root}/sdk-asan/tests/imu_packet_test" imu_packet_test

# Use a separate build tree so production artifacts stay uninstrumented and
# BUILD_TESTING remains OFF in the runtime build. This target exercises the
# actual threaded LidarPacketHandler and scan-to-cloud path.
colcon --log-base "${test_root}/ros-log" build \
  --base-paths "${ros_source}" --packages-select ouster_ros \
  --build-base "${test_root}/ros-build" \
  --install-base "${test_root}/ros-install" --merge-install \
  --event-handlers console_direct+ \
  --cmake-target ouster_ros_lidar_packet_handler_test \
  --cmake-args -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_CXX_FLAGS=-Wno-deprecated-declarations \
    "-DCMAKE_PROJECT_ouster_ros_INCLUDE=${workspace}/ament_compat.cmake" \
    -DBUILD_TESTING=ON \
    -DBUILD_PCAP=OFF -DBUILD_OSF=OFF -DBUILD_VIZ=OFF -DBUILD_MAPPING=OFF
run_gtest \
  "${test_root}/ros-build/ouster_ros/ouster_ros_lidar_packet_handler_test" \
  ouster_ros_lidar_packet_handler_test
