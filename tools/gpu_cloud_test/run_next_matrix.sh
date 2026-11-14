#!/usr/bin/env bash
# Offline only. No sensor, service, clock, governor, or power-setting changes.
set -euo pipefail
if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 BINARY FIXTURES NEW_OUTPUT_DIR validate|burst|paced [METHODS]" >&2
  exit 2
fi
binary="$1"; fixtures="$2"; output="$3"; phase="$4"
[[ -x "$binary" && -d "$fixtures" ]] || { echo "Missing executable or fixtures" >&2; exit 2; }
[[ ! -e "$output" ]] || { echo "Output must be a new directory" >&2; exit 2; }
case "$phase" in validate|burst|paced) ;; *) echo "Unknown phase" >&2; exit 2;; esac
mkdir -p "$output"
output="$(cd -- "$output" && pwd)"
controls=cpu_fused,gpu_mapped_io,gpu_soa_256
groups=(cpu_fused,gpu_mapped_io,gpu_soa_128,gpu_soa_256,gpu_soa_512,gpu_tiled_256,gpu_graph_256
        "$controls,gpu_registered_output" "$controls,gpu_registered_all" "$controls,gpu_register_each_frame")
if [[ $# == 5 ]]; then groups=("$5"); fi
sha256sum "$binary" > "$output/binary.sha256"
uname -a > "$output/host.txt"
awk '/Cpus_allowed_list/ {print}' /proc/self/status >> "$output/host.txt"
printf 'phase=%s\niterations=%s\nrounds=%s\n' "$phase" "${ITERATIONS:-128}" "${ROUNDS:-7}" > "$output/run-config.txt"
run() {
  local name="$1"; shift
  printf '%q ' "$binary" "$@" >> "$output/commands.sh"; printf '\n' >> "$output/commands.sh"
  "$binary" "$@" > "$output/$name.csv" 2> "$output/$name.log"
}
if [[ "$phase" == validate ]]; then
  for i in "${!groups[@]}"; do
    run "group${i}-synthetic" --synthetic --methods "${groups[$i]}" --validate-only
    run "group${i}-full" --input "$fixtures" --methods "${groups[$i]}" --validate-only
    run "group${i}-wrap" --input "$fixtures" --methods "${groups[$i]}" --crop-start 1474 --crop-width 637 --validate-only
  done
else
  # Reverse group and ROI order on the repeat; implementations rotate within
  # each process. Compare registration variants with their same-process controls.
  for repeat in 1 2; do
    for ((k=0;k<${#groups[@]};k++)); do
      i=$k; [[ "$repeat" == 1 ]] || i=$((${#groups[@]}-1-k))
      rois=(full crop); [[ "$repeat" == 1 ]] || rois=(crop full)
      for roi in "${rois[@]}"; do
        args=(--input "$fixtures" --methods "${groups[$i]}" --iterations "${ITERATIONS:-128}" --rounds "${ROUNDS:-7}")
        [[ "$roi" != crop ]] || args+=(--crop-start 1474 --crop-width 637)
        [[ "$phase" != paced ]] || args+=(--period-ms 100)
        run "group${i}-${roi}${repeat}" "${args[@]}" --output-dir "$output/group${i}-${roi}${repeat}-clouds"
      done
    done
  done
fi
