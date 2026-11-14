#!/usr/bin/env bash
# Use a fresh Docker container with --network none --shm-size 256m for each call.
set -eo pipefail
source "/opt/ros/${ROS_DISTRO:-lyrical}/setup.bash"
set -u
if [[ $# != 5 ]]; then
  echo "Usage: $0 BUILD_DIR NEW_RESULT_DIR LABEL MODE EXPECTATION" >&2
  echo "MODE: 0|1; EXPECTATION: heap|shm|exhausted|overestimate" >&2
  exit 2
fi
build_dir=$(cd "$1" && pwd)
result_dir=$2
label=$3
mode=$4
expectation=$5
tools_dir=$(cd "$(dirname "$0")" && pwd)
[[ $label =~ ^[a-zA-Z0-9_-]+$ && $mode =~ ^[01]$ ]] || { echo 'Invalid label or mode' >&2; exit 2; }
case "$expectation" in heap|shm|exhausted|overestimate) ;; *) echo 'Invalid expectation' >&2; exit 2;; esac
[[ ! -e "$result_dir" ]] || { echo "Result directory already exists: $result_dir" >&2; exit 2; }
[[ -x "$build_dir/shm_probe" && -f "$build_dir/librmw_zenoh_cpp.so" ]]
mkdir -p "$result_dir"
result_dir=$(cd "$result_dir" && pwd)
export ROS_DOMAIN_ID=87 RMW_IMPLEMENTATION=rmw_zenoh_cpp
# Keep the router and observer on the installed library, not the experiment.
unset OUSTER_EXPERIMENT_CPU_GROUP_SHM
pool=134217728
[[ $expectation != exhausted ]] || pool=1048576
export ZENOH_CONFIG_OVERRIDE="listen/endpoints=[\"tcp/127.0.0.1:17451\"];scouting/multicast/enabled=false;transport/shared_memory/enabled=true;transport/shared_memory/transport_optimization/pool_size=$pool"
router= observer=
cleanup() {
  for pid in "$observer" "$router"; do
    if [[ -n $pid ]]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
/opt/ros/lyrical/lib/rmw_zenoh_cpp/rmw_zenohd > "$result_dir/$label.router.log" 2>&1 &
router=$!
sleep 1
kill -0 "$router"
export ZENOH_CONFIG_OVERRIDE="connect/endpoints=[\"tcp/127.0.0.1:17451\"];scouting/multicast/enabled=false;transport/shared_memory/enabled=true;transport/shared_memory/transport_optimization/pool_size=$pool"
python3 "$tools_dir/observe.py" "$result_dir/$label.json" > "$result_dir/$label.sub.log" 2>&1 &
observer=$!
OUSTER_EXPERIMENT_CPU_GROUP_SHM=$mode LD_LIBRARY_PATH="$build_dir:${LD_LIBRARY_PATH:-}" \
  timeout --signal=TERM --kill-after=3s 25s "$build_dir/shm_probe" --ros-args --log-level debug \
  > "$result_dir/$label.pub.log" 2>&1
wait "$observer"
observer=
python3 "$tools_dir/check_result.py" "$result_dir/$label.json" "$result_dir/$label.pub.log" "$expectation"
