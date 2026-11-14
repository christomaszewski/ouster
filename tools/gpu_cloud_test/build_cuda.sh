#!/usr/bin/env bash
set -euo pipefail
prototype_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_dir="${1:-${prototype_dir}/build}"
mkdir -p "$output_dir"
cuda_root="${OUSTER_CUDA_ROOT:-/usr/local/cuda-13.2}"
"$cuda_root/bin/nvcc" -ccbin "${OUSTER_CUDA_HOST_COMPILER:-g++-13}" -std=c++17 -O3 -arch=sm_87 --cudart=static \
  --ftz=false --prec-div=true --prec-sqrt=true \
  -Xcompiler=-O3,-DNDEBUG \
  "$prototype_dir/cloud_benchmark.cu" -o "$output_dir/cloud_benchmark"
"$cuda_root/bin/nvcc" --version > "$output_dir/nvcc-version.txt"
sha256sum "$prototype_dir/cloud_common.hpp" "$prototype_dir/cloud_benchmark.cu" "$output_dir/cloud_benchmark" > "$output_dir/sha256.txt"
