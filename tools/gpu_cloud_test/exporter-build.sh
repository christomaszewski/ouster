#!/bin/sh
set -eu
# Usage: exporter-build.sh [prototype_directory] [output_directory]
# The image must contain matching production ROS/SDK headers and archives.
# Override OUSTER_GPU_EXPORT_IMAGE, OUSTER_GPU_ROS_SOURCE, and
# OUSTER_GPU_SDK_BUILD to use another retained build image/layout.
prototype=${1:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}
prototype=$(CDPATH= cd -- "$prototype" && pwd)
output=${2:-${OUSTER_GPU_OUTPUT_DIR:-$prototype/bin}}
mkdir -p "$output"
output=$(CDPATH= cd -- "$output" && pwd)
image=${OUSTER_GPU_EXPORT_IMAGE:-ouster_driver:cpu-demand-combined-v1-tests}
ros_source=${OUSTER_GPU_ROS_SOURCE:-/workspace/src/ouster-ros/ouster-ros}
sdk_build=${OUSTER_GPU_SDK_BUILD:-/workspace/build/ouster_ros/ouster-sdk}
docker run --rm --platform linux/arm64 --network none \
  -v "$prototype:/prototype:ro" -v "$output:/output" \
  -e "ROS_SOURCE=$ros_source" -e "SDK_BUILD=$sdk_build" \
  --entrypoint bash "$image" -lc '
    set -e
    source /opt/ros/lyrical/setup.bash
    source /workspace/install/setup.bash
    cmake -S /prototype -B /tmp/gpu-export-build \
      -DBUILD_EXPORTER=ON -DCMAKE_BUILD_TYPE=Release \
      -DROS_SOURCE="$ROS_SOURCE" -DSDK_BUILD="$SDK_BUILD"
    cmake --build /tmp/gpu-export-build -j 4
    cp /tmp/gpu-export-build/export_scan /tmp/gpu-export-build/cpu_production_benchmark /output/
  '
