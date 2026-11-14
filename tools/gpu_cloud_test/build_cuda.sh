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
"$cuda_root/bin/nvcc" -ccbin "${OUSTER_CUDA_HOST_COMPILER:-g++-13}" -std=c++17 -O3 -arch=sm_87 --cudart=static \
  --ftz=false --prec-div=true --prec-sqrt=true \
  -Xcompiler=-O3,-DNDEBUG \
  "$prototype_dir/cloud_benchmark_next.cu" -o "$output_dir/cloud_benchmark_next"
"$cuda_root/bin/nvcc" -ccbin "${OUSTER_CUDA_HOST_COMPILER:-g++-13}" -std=c++17 -O3 -arch=sm_87 --cudart=static \
  --ftz=false --prec-div=true --prec-sqrt=true -shared -Xcompiler=-O3,-DNDEBUG,-fPIC \
  "$prototype_dir/native_backend.cu" -o "$output_dir/libouster_gpu_cloud.so"
"${OUSTER_CUDA_HOST_COMPILER:-g++-13}" -std=c++17 -O3 -DNDEBUG \
  "$prototype_dir/native_benchmark.cpp" -L "$output_dir" -louster_gpu_cloud \
  '-Wl,-rpath,$ORIGIN' -o "$output_dir/native_benchmark"
"$cuda_root/bin/nvcc" --version > "$output_dir/nvcc-version.txt"
sha256sum "$prototype_dir/cloud_common.hpp" "$prototype_dir/cloud_benchmark.cu" "$prototype_dir/cloud_benchmark_next.cu" "$prototype_dir/native_backend.h" "$prototype_dir/native_backend.cu" "$prototype_dir/native_benchmark.cpp" "$output_dir/cloud_benchmark" "$output_dir/cloud_benchmark_next" "$output_dir/libouster_gpu_cloud.so" "$output_dir/native_benchmark" > "$output_dir/sha256.txt"
