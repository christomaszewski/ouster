#!/usr/bin/env bash
set -euo pipefail
prototype_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${OUSTER_CUDA_TOOLKIT:?Set OUSTER_CUDA_TOOLKIT to the host path containing bin/nvcc and targets/sbsa-linux}"
compiler_image="${OUSTER_CUDA_COMPILER_IMAGE:-ouster-cuda-compiler:ubuntu24-gcc13}"
output_dir="${1:-${prototype_dir}/build}"
mkdir -p "$output_dir"
output_dir="$(cd -- "$output_dir" && pwd)"
docker run --rm --platform linux/arm64 --network none \
  -v "$OUSTER_CUDA_TOOLKIT:/usr/local/cuda-13.2:ro" \
  -v "$prototype_dir:/prototype:ro" -v "$output_dir:/output" \
  -e OUSTER_CUDA_HOST_COMPILER="${OUSTER_CUDA_HOST_COMPILER:-g++-13}" \
  "$compiler_image" bash /prototype/build_cuda.sh /output
