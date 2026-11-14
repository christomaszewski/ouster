# Organized azimuth crop and GPU experiment

This study adds an opt-in organized cloud crop and prepares a separate CUDA cloud-processing test. The production driver remains CPU based. The crop builds on the [image/cloud optimizations](cpu-demand-optimization-2026-11-13.md); earlier [azimuth-only measurements](cpu-profile-2026-11-13.md#azimuth-window-comparison) retained the full cloud and are not the baseline numbers for this study.

**Real-sensor result:** cropping reduces driver CPU by **8.96% at 180°** and **21.91% at 90°**, compared with full-width organized output at the same sensor window. Payload falls **43.90%** and **68.90%** respectively. Both retain organized rows and the original point fields.

## Organized crop contract

```yaml
driver_params:
  organized: true
  destagger: true
  organized_cloud_crop: true
  azimuth_window_start: 0
  azimuth_window_end: 180000
```

`organized_cloud_crop` defaults to false and is read when the cloud handler is configured. It exists in both the combined driver and the split cloud component; it requires `organized: true`. It does not set the sensor azimuth window. It changes cloud width, row stride and byte length while retaining point fields/types/offsets, the 48-byte original point stride, coordinates, per-point timestamps, ring indices and header semantics. Consumers must accept the smaller dimensions. Images, LaserScan, raw packets and SDK scan dimensions keep their existing behavior.

The crop is the complement of the longest empty gap in the union of the selected rows' metadata column windows after destaggering. This yields one stable circular column interval for all rows. It retains every configured source column and NaN holes at staggered edges; it does not independently compact rows. Selection does not depend on the current scene, missing packets, or whether a particular range is zero. Full azimuth retains the original dimensions and column origin zero.

PointCloud2 does not contain a crop-origin field. The driver logs the original column origin and width; consumers needing original-column indices must derive them from sensor metadata plus destagger and vertical-reduction settings. Calibration, masking, timestamp lookup and field reads continue to use the original full-scan indices.

For this sensor, selected rows have shifts from -62 to +62. Inclusive wrapping sensor column windows explain why the crop is larger than exactly one-half or one-quarter:

| Azimuth | Sensor column window | Output origin | Organized dimensions | Row bytes | Data bytes | MiB |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| 360° | full | 0 | 128×2048 | 98,304 | 12,582,912 | 12.000 |
| 180° | [1024, 0] | 962 | 128×1149 | 55,152 | 7,059,456 | 6.732 |
| 90° | [1536, 0] | 1474 | 128×637 | 30,576 | 3,913,728 | 3.732 |

The implementation skips Cartesian/mask arithmetic and cloud packing outside the selected rows/columns. It retains the full XYZ scratch allocation, SDK scan arrays and calibration tables. Cropping after constructing a full cloud would save publication bandwidth but miss this processing reduction.

## Source, builds and validation

- ROS: [`3bf8744cedeaecfe21fd726898be6eb617229577`](https://github.com/christomaszewski/ouster-ros/commit/3bf8744cedeaecfe21fd726898be6eb617229577), based on `36baee96d0d9f4147e7adcc25db502e7dd812e19` in the ROS fork.
- SDK unchanged: `71b8c20df39141f5ec5154ec8ce9afc69f00ae52`.
- Mac-built native Linux ARM64 runtime: `ouster_driver:cpu-crop-organized-v1`, digest `sha256:c3807d4036dae677cd3e3f38b0d8895c6fe320b13999416d6db296bf2feab518`.
- Regression image: `ouster_driver:cpu-crop-organized-v1-tests`, digest `sha256:1e958d2087a79c8c14cfcf4b238003bb3af101b8aa475b139122cbdc69621d98`.
- Same fleet base `sha256:bf5921ad3fb5fd74bff0dda1bc1666e7da4e292ee6948a4d11fcdd485d5a63d9`; Release `-O3 -DNDEBUG`, no production fast-math, unroll, GPU or target-tuning changes. Runtime build took 73.59 seconds; the regression build took 122.67 seconds.

**235 C++ and 8 Python replay tests pass**, with no failures/errors/skipped/disabled tests. All 14 runtime shared libraries load and build/runtime ROS package versions match. The regression build reused all 75 production objects and left installed production artifacts unchanged. An additional 25 wrapper checks, shell syntax and Git whitespace checks pass. [Build verification](../var/crop-build-2026-11-13/organized-v1/independent-test-summary.json), [runtime verification](../var/crop-build-2026-11-13/organized-v1/independent-runtime-summary.json), [wrapper checks](../var/crop-build-2026-11-13/wrapper-checks/summary.json).

The main cloud suite has 132 tests. Seven new crop groups include an independent exhaustive minimum-window oracle over 2,048 small-grid configurations, metadata rejection, 10,560 full-output-slice comparisons across 35 point/profile combinations and 64 crop/layout/mask configurations over three frames, retained-message behavior, Cartesian sentinel checks proving excluded slots are untouched, and full 128×2048 examples. They cover wrapping/nonwrapping/full/single-column windows, negative shifts, destagger on/off, row reduction, masks/range filters/transforms, dropped columns, timestamps, NaNs/infinities, dual returns and all supported point formats. Both combined and split components compile.

Fourteen focused writer/crop groups also pass ASan/UBSan. The harness uses `EIGEN_MALLOC_ALREADY_ALIGNED=1` to match existing SDK/PCL allocation ABI; production flags and dependency instrumentation are unchanged. No sanitizer suppression is used. [Focused validation](../var/organized-crop-test-20261113/summary.json).

## Hardware comparison protocol

The Orin AGX at 10.160.1.21 runs the same Mac-built candidate image with cropping disabled or enabled against the real OS-0-128 at 10.160.1.51. Each angle uses A–B–B–A order. The fixed observer requests points and all four LEGACY images. Each primary trial samples CPU and perf counters for approximately 60 seconds after 15 seconds of observer settling, with about 90 seconds of stream observation.

Other settings remain 2048×10, LEGACY, internal-oscillator timestamps, original organized/destaggered points, frame `os_sensor`, all `IMU|PCL|SCAN|IMG|RAW|TLM` processors, Zenoh SHM with 256 MiB pool, host network/IPC and unlimited memlock. The isolated namespace is `/cpu_crop`, ROS domain 86, router endpoint 127.0.0.1:17450. The same fixed pre-crop observer image is used for every trial. The expected changes are only sensor azimuth and the opt-in cloud crop; image dimensions remain full resolution.

**100% CPU means one occupied core.** Service CPU includes the driver and supervisor; observer/router CPU is recorded separately and excluded. Power mode MAXN and schedutil governors are unchanged; frequency remains adaptive. Initial recorded CPU temperature was 44.4°C. GPU tests, host compilation and large transfers are kept out of the CPU profiling windows. Counters are aggregate process events, not attribution of a gain to a particular loop or cache level.

## Completed real-sensor results

| Window | Driver CPU, full → crop | Relative reduction | Service CPU, full → crop | Relative reduction | Cloud data, full → crop |
| --- | --- | ---: | --- | ---: | --- |
| 180° | 32.273% → 29.380% | **8.96%** | 40.016% → 36.897% | **7.79%** | 12.000 → 6.732 MiB |
| 90° | 25.419% → 19.849% | **21.91%** | 30.326% → 25.023% | **17.49%** | 12.000 → 3.732 MiB |

Each mean uses two approximately 60-second samples per condition. Primary sequences are `a1_180`, `b1_180`, `b2_180`, `a2_180` and the replacement 90° sequence `a3_90`, `b3_90`, `b4_90`, `a4_90`. All accepted trials delivered 901 clouds and 900 of each image over their observation windows, approximately 10 Hz, with no header timestamp gap above 150 ms. The cloud fields/stride/frame and all four full-resolution mono16 image schemas stayed unchanged; image pixel values naturally vary with the live scene. Host UDP errors and NoPorts deltas were zero.

| Aggregate counter reduction | 180° crop | 90° crop |
| --- | ---: | ---: |
| Task clock | 9.13% | 20.95% |
| Cycles | 9.59% | 21.46% |
| Instructions | 15.77% | 29.56% |
| Cache references | 18.08% | 32.27% |
| Cache misses | 24.09% | 44.54% |

Every perf event ran 100% of its interval. These counters support reduced processing and memory access, without proving a specific DRAM-bandwidth change. The original 360° grid still appeared with cropping enabled in a final live smoke test, at approximately 10 Hz with no timestamp gaps; its short CPU sample has no matched control and is not used for a speedup claim.

**Excluded attempt:** the first 90° cropped observer (`b1_90`) wrote a complete 90-second result (901 clouds, 900 of each image, no timestamp gaps) and then exited with status 1 during teardown. The driver remained healthy. Docker events preserve that exit; the original harness checked status before saving observer stderr, so the underlying teardown error is unknown. Both initial 90° attempts (`a1_90`, `b1_90`) are excluded from primary means. The harness now saves observer logs/status before asserting, and the entire 90° A–B–B–A comparison was repeated with successful observer exits. No driver or observer processing code was changed for the repeat.

[Raw comparison and invariants](../var/cpu-crop-2026-11-13/results/comparison.json) include all trial values, exact means, source identities and 229 passing checks. An [independent audit](../var/cpu-crop-2026-11-13/results/independent-analysis-audit.json) passed 77 further checks against raw records. The numerical comparison does not combine these results with the older azimuth-only or pre-optimization baselines.

After the final full-azimuth smoke, the sensor's complete active configuration exactly matched the initial snapshot, including STANDBY and [0, 360000]. Lifecycle state was absent, the driver container exited zero without OOM, and the test driver/router were removed; only the preexisting registry container remained before GPU testing. [Restored configuration](../var/cpu-crop-2026-11-13/results/final-sensor-config.json), [final state](../var/cpu-crop-2026-11-13/results/final-state.json).

## Standalone GPU test

The reusable test lives in [tools/gpu_cloud_test](../tools/gpu_cloud_test/README.md). It is a decoded-scan CUDA experiment for single-return LEGACY, original 48-byte organized clouds, every row and no input mask. It is not integrated into the production ROS driver. Packet reception/decoding, images, lifecycle management and ROS publication/serialization remain outside this benchmark.

A settled four-second full-azimuth capture contains 5,120 real lidar packets (127,467,520 payload bytes) and 12 published clouds. Offline SDK batching exports eight complete frames after skipping an initial partial scan. The exporter calls the actual production Cartesian and cloud-writer headers from ROS `36baee96`/SDK `71b8c20`, using the same float calibration tables, lidar-to-sensor transform, min/max limits 0/1,000,000 mm and column-zero scan timestamp. All eight fixtures match their captured published clouds and recorded field layout/dimensions; the external checker requires every fixture to match. [Published equivalence](../var/cpu-crop-2026-11-13/gpu-published-equivalence.json).

The Orin has L4T R39.2.1, Ubuntu 24.04.5, NVIDIA driver 595.78 and CUDA 13.2.2 (nvcc/runtime 13.2.86). Its integrated compute-capability-8.7 GPU reports 16 SMs, mapped-host/UVA/managed-memory support, and no concurrent managed or pageable-memory access. A CUDA kernel built locally on the Mac ran successfully on it. No host OS or CUDA installation was changed. [Environment and probe](../var/crop-gpu-build-2026-11-13/summary.json).

The CUDA binary is built natively in a Linux ARM64 Ubuntu 24.04/GCC 13.3 container on the Mac, using a read-only export of the compatible installed toolkit, static cudart and `sm_87`. It uses precise math, explicit float32 FMA and no `--use_fast_math` or `-Ofast`. Persistent LUTs and buffers avoid per-frame allocation/registration. The setup retains CPU implementations as comparisons:

- `cpu_staged`: full Cartesian scratch followed by field packing.
- `cpu_fused`: fused Cartesian and final packing for selected columns.
- `gpu_device_copy`: pinned host staging plus device input/output and explicit transfers.
- `gpu_mapped_io`: mapped pinned input/output with device-resident LUTs.
- A separate executable invokes the actual production CPU headers. Its optional full-then-crop path is not the optimized production crop implementation and must not be described as such.

GPU timings include pageable-to-pinned input preparation, submission, transfers/coherence, a blocking completion wait, density reporting and a complete copy into an ordinary host-owned cloud. Allocation, LUT/context setup, validation and hashes are outside timing. CPU time is process CPU time. Timings replay the captured frames as quickly as possible; they are not whole-service CPU, sensor-to-publication latency, 10 Hz behavior or energy measurements. A wrapped 637-column GPU ROI, if measured, selects columns from the full-azimuth fixtures and is not a separately captured 90° sensor workload.

The benchmark checks exact fields, finite float bits, invalid NaNs, timestamps, homogeneous coordinate, initialized padding and density before timing and after each round. Synthetic checks include small/odd grids, partial CUDA blocks, both shift signs, wraparound, range boundaries, all-valid/all-invalid inputs, uint32 conversion and overflowing relative timestamps. The host completion policy uses blocking CUDA events rather than an intentional polling wait. Future integration must still preserve ownership through completion and evaluate publication, competing GPU workloads, sensor cadence and power.

## Completed GPU measurements

The Mac-built CUDA executable ran successfully on the real Orin. Synthetic, real full-frame and real wrapped-ROI validation all passed for both GPU memory modes and both standalone CPU implementations. Every emitted host cloud hash agrees across implementations within each ROI. Full-frame timed checksums also agree with the separate executable using actual production headers. The sensor remained in standby throughout these offline GPU tests.

The process was pinned to CPU 2 (`taskset`, or Docker's CPU set for the production-header executable), with existing governors/power mode unchanged. Run order was production CPU, full1, crop1, crop2, full2, production CPU. Each full/crop run rotates the four implementations across seven rounds of 128 frames after warmup. The following numbers are pooled medians of 14 round-average per-frame times from two runs; they are not per-frame latency percentiles or 14 independent hardware trials.

| Input | Implementation | Wall ms/frame | Process CPU ms/frame |
| --- | --- | ---: | ---: |
| Full 128×2048 | **Actual production CPU headers** | **3.493** | **3.486** |
| Full 128×2048 | Standalone staged CPU | 5.066 | 5.054 |
| Full 128×2048 | Standalone fused CPU | 4.347 | 4.337 |
| Full 128×2048 | GPU, explicit copies | 4.029 | 2.304 |
| Full 128×2048 | **GPU, mapped I/O** | **2.790** | **2.103** |
| Wrapped 128×637 ROI | Standalone staged CPU | 1.895 | 1.890 |
| Wrapped 128×637 ROI | **Standalone fused CPU** | **1.252** | **1.249** |
| Wrapped 128×637 ROI | GPU, explicit copies | 2.074 | 1.017 |
| Wrapped 128×637 ROI | GPU, mapped I/O | 1.344 | 1.092 |

For the full cloud, mapped GPU I/O used **20.13% less wall time and 39.65% less process CPU time** than the actual-header CPU comparator. The explicit-copy GPU path used less CPU but took 15.35% more wall time. The actual-header executable was built with GCC 15 in the retained ROS runtime; the standalone CPU controls and CUDA executable use GCC 13. The standalone fused CPU is slower than the actual production headers here, so comparing CUDA only with that generic baseline would overstate its advantage over the existing driver.

For the 637-column ROI, mapped GPU I/O used 12.60% less process CPU but took 7.36% more wall time than the standalone fused CPU. The explicit-copy mode further reduced process CPU while taking substantially longer. This is a latency/CPU tradeoff, not a demonstrated win over the optimized cropped ROS driver: that production path is not the ROI CPU comparator, and the ROI is selected from full-azimuth captured input rather than a new 90° scan capture.

**Decision:** retain the CPU production backend and the optional organized crop. The validated mapped-GPU prototype is promising for full clouds, but should undergo an integrated ROS/cadence/competing-GPU/power comparison before production adoption. These measurements do not establish a 39.65% reduction in whole-driver CPU. For smaller output, ordinary CPU fusion remains a useful control and GPU synchronization/input/output costs matter more.

[GPU analysis](../var/cpu-crop-2026-11-13/gpu-results/gpu-analysis.json) retains every run/round, ranges, quartiles, ratios, input CSV hashes and comparison qualifications. [Execution records](../var/cpu-crop-2026-11-13/gpu-results/runs.json), [output hashes](../var/cpu-crop-2026-11-13/gpu-results/output-hashes.json), and [final host state](../var/cpu-crop-2026-11-13/gpu-results/final-host-state.json) record successful commands and exact sensor restoration after GPU testing. CPU temperature afterward was 45.1°C; MAXN and schedutil remained unchanged. Only the preexisting registry container remained, and task SSH sessions were closed.

## Reproduction and retained evidence

- CPU harness and raw results: `var/cpu-crop-2026-11-13/`, retained on the Orin under `/home/uxv/ouster-crop-profile-20261113/`. The initial sequence and replacement 90° sequence have separate summaries; retain the excluded attempt's evidence.
- Mac runtime/test builds: `var/crop-build-2026-11-13/`; focused tests: `var/organized-crop-test-20261113/`.
- GPU source and portable build/run instructions: [tools/gpu_cloud_test/README.md](../tools/gpu_cloud_test/README.md). Source is tracked; fixtures, binaries, toolkit export and measurements are deliberately local artifacts.
- Local GPU preparation: `var/crop-gpu-build-2026-11-13/`; fixture/export/prototype provenance: `var/gpu-cloud-prototype-2026-11-13/`.
- Actual GPU results: `var/cpu-crop-2026-11-13/gpu-results/`; remote bundle, fixtures and outputs: `/home/uxv/ouster-crop-profile-20261113/gpu-test/`.

No passwords or private keys are stored in these scripts or reports. Sensor settings were not persisted to flash. No production deployment, host CUDA installation, power policy or default GPU backend was changed.
