#!/usr/bin/env bash
# Run inside the matching ROS Lyrical development container; never installs files.
set -eo pipefail
source "/opt/ros/${ROS_DISTRO:-lyrical}/setup.bash"
set -u

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 PATCHED_SOURCE NEW_OUTPUT_DIR [candidate|overestimate]" >&2
  exit 2
fi
source_dir=$(cd "$1" && pwd)
output=$2
mode=${3:-candidate}
tools_dir=$(cd "$(dirname "$0")" && pwd)
base=4e17ccfb9e114c87ac87b802e705b60e283b4115
case "$mode" in candidate|overestimate) ;; *) echo "Unknown mode: $mode" >&2; exit 2;; esac
[[ ! -e "$output" ]] || { echo "Output already exists: $output" >&2; exit 2; }
[[ $(git -c safe.directory="$source_dir" -C "$source_dir" rev-parse HEAD) == "$base" ]] || {
  echo "Source must be pinned to $base" >&2; exit 2;
}
git -c safe.directory="$source_dir" -C "$source_dir" diff HEAD --binary | cmp - "$tools_dir/cpu-group-shm.patch"
[[ -z $(git -c safe.directory="$source_dir" -C "$source_dir" ls-files --others --exclude-standard) ]] || {
  echo "Unexpected untracked source files" >&2; exit 2;
}
package_version=$(dpkg-query -W -f='${Version}' ros-lyrical-rmw-zenoh-cpp)
[[ $package_version == 0.10.6-* ]] || { echo "Expected installed rmw_zenoh_cpp 0.10.6, got $package_version" >&2; exit 2; }
mkdir -p "$output"
output=$(cd "$output" && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
build_source="$source_dir"
if [[ $mode == overestimate ]]; then
  # This mutation exists only in a disposable test source copy; no production knob.
  mkdir "$scratch/source"
  cp -a "$source_dir/rmw_zenoh_cpp" "$scratch/source/"
  build_source="$scratch/source"
  python3 - "$build_source/rmw_zenoh_cpp/src/detail/rmw_publisher_data.cpp" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
old = 'const size_t max_data_length = type_support_->get_estimated_serialized_size(\n      ros_message, type_support_impl_);'
text = path.read_text()
assert text.count(old) == 2, 'Unexpected source; refuse test-only mutation'
text = text.replace(old, old[:-1] + ' + 64;\n    // Test-only overestimate for exact-length fallback coverage.', 1)
path.write_text(text)
PY
fi
cmake -S "$build_source/rmw_zenoh_cpp" -B "$scratch/rmw-build" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
cmake --build "$scratch/rmw-build" --target rmw_zenoh_cpp --parallel "${BUILD_JOBS:-3}"
cmake -S "$tools_dir" -B "$scratch/probe-build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$scratch/probe-build" --parallel "${BUILD_JOBS:-3}"
cp "$scratch/rmw-build/librmw_zenoh_cpp.so" "$scratch/probe-build/shm_probe" "$output/"
ldd "$output/librmw_zenoh_cpp.so" | tee "$output/dependencies.txt"
! grep -q 'not found' "$output/dependencies.txt"
{
  printf 'source=%s\nbase=%s\nmode=%s\ninstalled_rmw=%s\n' \
    'https://github.com/ros2/rmw_zenoh' "$base" "$mode" "$package_version"
  sha256sum "$tools_dir/cpu-group-shm.patch" \
    "$build_source/rmw_zenoh_cpp/src/detail/rmw_publisher_data.cpp" \
    "$output/librmw_zenoh_cpp.so" "$output/shm_probe"
  c++ --version
} > "$output/provenance.txt"
