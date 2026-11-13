# Ouster image and cloud CPU optimizations

This follow-up implements the opportunities identified in the [initial CPU profile](cpu-profile-2026-11-13.md) and [SDK architecture review](sdk-1x-driver-review-2026-11-13.md): avoid unused image work, improve image memory access, and construct clouds directly in the ROS message buffer. It builds on the corrected conversion backport and row-index work described in the [backport report](cpu-backport-2026-11-13.md).

**Completed result:** with every image requested, the combined changes reduce driver CPU by **6.25%** and sampled cache misses by **23.02%**. With a cloud subscriber and no image subscribers, driver CPU falls **22.58%**. Both comparisons use matched real-sensor trials with unchanged settings and schemas. The combined regression gate and live subscriber/lifecycle checks pass. Forced loop unrolling remains experimental; production keeps normal `-O3`.

## Source and build identity

| Component | Revision | Contents |
| --- | --- | --- |
| ROS baseline | `82a46b870451d9dac2d9eb1db8a953a6bff1d3b9` | Corrected upstream conversion backport, equivalence tests, and cloud row-index hoisting |
| ROS image demand | `09b073a1df7d9b6c520fdc0d4101405ab9c13cbe` | Per-channel subscriber gating |
| ROS image memory/indexing | `b77c915fa9c1b5f1ec4e5bdaef833da12e5abdba` | Reused buffers, range view, and two contiguous destagger spans |
| Final integrated ROS | `36baee96d0d9f4147e7adcc25db502e7dd812e19` | Direct PointCloud2 construction on top of the preceding two commits |
| SDK baseline | `8aa86b94b1c75dfcb99159a4147b44d973e52323` | Existing pinned SDK fork |
| Final SDK | `71b8c20df39141f5ec5154ec8ce9afc69f00ae52` | Direct grayscale samples (`4e23898`), beam-correction row scratch (`986542c`), optional benchmark (`71b8c20`) |

The final commits are retained on the user fork branches [ROS demand-driven-processing](https://github.com/christomaszewski/ouster-ros/tree/codex/demand-driven-processing) and [SDK optimize-sdk-image-processing](https://github.com/christomaszewski/ouster-sdk/tree/codex/optimize-sdk-image-processing). The wrapper ROS/message and SDK source pins select these exact revisions. The SDK remains on the existing version lineage. Its public image-processing header and class layouts are unchanged.

The combined Linux ARM64 image is `ouster_driver:cpu-demand-combined-v1`, built on the Mac from those two final revisions. Its recorded digest is `sha256:3cc43ada800edde4a590140630ee9ebc8875f25336e6cf295a22790eb200f894`. Build inputs and logs are in [combined-v1](../var/demand-build-2026-11-13/combined-v1/). Production compiler settings remain Release `-O3 -DNDEBUG`; the separate unrolling experiment below does not alter them.

## Image demand and observable behavior

With IMG enabled, the processor checks ordinary and intra-process subscriber counts for each supported image channel once per scan. It produces only requested channels. With no requested images, it returns before reading image fields, destaggering, or updating grayscale exposure state. This gate does not disable acquisition, clouds, IMU, telemetry, or the configured raw stream.

Each grayscale channel family has its own exposure history. If a family has no subscribers, its history pauses rather than resetting. On a new subscription or rejoin, discovery can delay the first frame, and exposure can adjust briefly as that history resumes. Both returns share the signal and reflectivity exposure objects: a subscriber to only `SIGNAL2` or `REFLECTIVITY2` still requires first-return processing to update the shared state once per scan. The unrequested first-return image is not published. Near-IR correction and exposure run only for requested near-IR output.

For continuous processing and exposure history, set this ROS driver parameter in the instance configuration:

```yaml
driver_params:
  image_always_process: true
```

The default is `false`. The value is captured when the image handler is created; changing the ROS parameter on an already-created handler does not switch its policy immediately. Apply the configuration through the service's normal recreation/reconfiguration flow. This parameter also exists on the split `OusterImage` component. Unsupported channels, such as RGB on a LEGACY sensor, do not cause work merely because a subscriber exists.

RGB requires a qualification: RGB conversion/tone mapping occurs upstream in `LidarPacketHandler`, where colored point clouds can also use it. The image gate skips RGB image assembly when unrequested, but does not gate that shared upstream RGB work. The real sensor's LEGACY configuration does not use this RGB path.

## Image memory access and SDK work

The ROS image processor now reads range through an immutable view into the current scan and reuses its typed channel buffers and float working images. Integer casts remain in the same order as before conversion to float, including reflectivity and near-IR truncation. Buffers belong to the native scan-processing thread and are overwritten before each use. Requested images retain their previous encodings, masks, dimensions, timestamps, and pixel ordering.

Destagger shifts are normalized once per row. A row is copied as two contiguous source spans into the original destination order, replacing a modulo in the pixel loop. Negative shifts, a shift of `-width`, and the wrap boundary are covered by equivalence tests. The focused ARM64 test binary contains no per-pixel division in the new image path; its remaining divisions are allocation/resize checks. This is assembly evidence about the tested binary, not a measurement of cache misses or overall CPU savings.

The SDK grayscale auto-exposure selector stores positive sample **values** contiguously instead of storing indices and repeatedly looking back into the image. It reserves exactly `ceil(pixel_count / 4)` entries. At 128×2048, sample capacity changes from 2 MiB of 64-bit indices to 256 KiB of float values, or 512 KiB for the double overload. The minimum of 100 positive samples, percentile positions, early returns, damping, affine transforms, clamping, and default three-frame state schedule are unchanged. Scratch remains a local allocation on sampled updates; no per-instance or thread-local cache is added.

Beam-uniformity correction fills one row of differences directly in the original ascending valid-column order. It removes the full `(height - 1) × valid_columns` difference matrix and subsequent row copy. For a full 128×2048 float image, the removed matrix is approximately 1 MiB; the row scratch is 8 KiB. Median selection, cumulative sums, the LU fit, damping, and the eight-frame schedule retain their original order. In particular, the BUC counter still advances when `update_state=false`, and a changed height still recomputes its state. Those behaviors differ from auto-exposure and are explicitly tested.

## Direct PointCloud2 construction

The factory now converts one correctly aligned stack point at a time and writes its registered fields into the final `PointCloud2::data` byte buffer. It retains PCL's field registration for names, types, counts, offsets, and `sizeof(PointT)` stride, while removing the full PCL point-cloud staging allocation and its copy into PCLPointCloud2. Cartesian calculation remains a separate existing step.

Fixed-size `memcpy` operations avoid treating byte storage as aligned live point objects. Compile-time checks bound each field within the point representation. The initialized fourth XYZ slot at byte offset 12 is also preserved: it is the PCL homogeneous coordinate for most supported types, and intensity for compact `o_xyzi`. Other unspecified alignment padding is initialized rather than copied from object padding. Tests compare this slot both directly and through `pcl::fromROSMsg`.

The output reserves the maximum valid frame size before caching its data pointer. Positive dimensions and a positive row step that divides the height are validated before allocation. Unorganized output grows only within that reservation and trims its size afterward. A message retained by a callback receives a fresh buffer on the next frame; exclusively owned messages can reuse storage. The installed ROS byte-buffer CPU backend uses `std::vector` storage and deep copying, consistent with these ownership assumptions.

The `original` organized contract remains 48 bytes per point: a 128×2048 cloud still contains 12,582,912 data bytes. This change does not compact the schema, crop the organized grid, fuse Cartesian calculations, or establish zero-copy middleware transport. Focused ARM64 assembly shows fixed-size field copies becoming ordinary stores, with no calls or divisions inside the tested original/LEGACY point-writing loop. Independent disassembly of the final integrated runtime confirms this: the organized original/LEGACY loop at `0x1df204–0x1df2bc` has direct field/homogeneous stores and no calls or divisions; the row divisions precede it at `0x1df1dc/0x1df1ec`.

## Completed focused validation

| Validation | Result and scope |
| --- | --- |
| ROS images | **28 passing tests**: full-byte/header comparison with the frozen old image processor over multiple exposure cycles; five profiles, two small widths, masks, and a full 2048×128 case; second-return-only demand, missing unrelated fields, RGB-only demand, pause/resume, and ordinary/intra-process subscribers |
| ROS cloud suite | **125 passing Release tests**, including the new writer matrix: 35 point-type/profile combinations, 32 layouts each, and three frames per layout, totaling 5,280 cloud-message comparisons when both returns are counted where present |
| Cloud sanitizers | **7 passing grouped cases** under ASan/UBSan: layouts, fields, NaNs, packed RGB, homogeneous slot, PCL roundtrip, retention/serialization, buffer reuse, and invalid dimensions |
| SDK grayscale/BUC | **14 passing tests, 4,232 old/new frame comparisons**, for both normal and unrolled builds; float/double, sparse thresholds, ties, zero, infinities/NaNs, tiny/large values, shapes, and state-update cadence |

SDK comparisons require bitwise equality for finite values, signed zero, and infinities, and matching NaN positions/classification. They do not claim a portable NaN payload contract. The frozen SDK oracle is independent of the changed implementation. The image-processor oracle still calls the linked SDK, so the independent SDK tests are necessary alongside whole-image tests.

The cloud sanitizer executable uses `EIGEN_MALLOC_ALREADY_ALIGNED=1` to match the allocation ABI of its existing SDK/PCL dependencies. The initial mismatched-harness failure is retained in the artifacts. This was a test-harness ABI correction, not a sanitizer suppression or production flag change; leak detection remained enabled.

The wrapper's [upstream gate](../tools/test_upstream.sh) includes SDK `image_processing_test` in the separate ASan tree and ROS `ouster_ros_image_processor_test` in the inherited Release tree. Cloud tests are part of `ouster_ros_test`.

The final combined runtime was built natively as Linux ARM64 on the Mac in 31.33 seconds, using the same immutable fleet base as the baseline (`sha256:bf5921ad3fb5fd74bff0dda1bc1666e7da4e292ee6948a4d11fcdd485d5a63d9`). Independent checks loaded all 14 shared libraries and matched build/runtime ROS packages and embedded source pins. The full regression build completed in 96.15 seconds: **228 C++ tests and 8 Python replay tests passed**, with no failures, errors, disabled or skipped cases. Counts are SDK ASan 9 receive + 6 IMU + 14 image; ROS 125 main + 28 image + 8 timestamp + 9 raw QoS + 7 raw publisher + 2 raw sensor + 20 mask. All 75 production objects were reused, compiler settings were unchanged, and the production installation was unchanged by test enabling.

The test image is `ouster_driver:cpu-demand-combined-v1-tests`, digest `sha256:b1d3ae8773d2972f5d708ebf24e82b59d4053dfaa545a1371e1aed548bcdef3e`. Runtime and test provenance select the same source commits. An additional **25 wrapper tests passed** (11 fetch/provenance, 4 runtime-Dockerfile, 10 build-reuse), as did Bash syntax and Git whitespace checks. See [independent runtime verification](../var/demand-build-2026-11-13/combined-v1/independent-runtime-summary.json), [full test verification](../var/demand-build-2026-11-13/combined-v1/independent-test-summary.json), and [wrapper checks](../var/demand-build-2026-11-13/wrapper-checks/summary.json).

## Compiler unrolling experiment

Two Mac-built AArch64 standalone benchmark binaries contain the changed SDK image-processing implementation. Both use GCC 15.2, `-O3 -DNDEBUG`, and no fast-math option. The second adds **`-funroll-loops` only to `image_processing.cpp`**; the benchmark and frozen reference retain the normal flags. Both pass the same 14 equivalence tests and run with the baseline container's standard runtime libraries.

GCC reports unrolling the grayscale sample loop seven times with that flag. It also unrolls the BUC fit-matrix setup loop; the report does not show explicit unrolling of the masked row-scratch loop. Normal `-O3` already vectorizes some loops and fully unrolls small fixed loops.

| Code-size measurement | Normal | `-funroll-loops` |
| --- | ---: | ---: |
| Image-processing object `.text` | 142,451 bytes | 307,966 bytes |
| Grayscale float AE symbol | 3,520 bytes | 8,904 bytes |
| Float dark-count computation symbol | 3,540 bytes | 8,560 bytes |

The larger code footprint can affect instruction-cache behavior. It is evidence that unrolling occurred, not evidence that it improves this workload. No manual unrolling or production compiler-flag change is adopted here.

The optional `image_processing_benchmark` CMake target is excluded from the default build and is not a timing-based pass/fail test. It uses eight deterministic float32 128×2048 source frames, full and half-width valid-column masks, 24 warmup frames, and alternating old/new order. Timings include identical fresh-image copying and preserve the normal three/eight-frame update cadence. Example runs inside a compatible ARM64 runtime:

```sh
./image_processing_benchmark 192 7 > normal.csv
./image_processing_benchmark_unroll 192 7 > unroll.csv
```

Local Mac/Linux-VM baseline-versus-changed medians, including that copy, reduced AE CPU time by 11.9% for full input and 9.6% for half input; BUC reductions were 53.3% and 37.2%. These are local algorithm microbenchmarks, not Orin sensor or ROS service CPU measurements. They also do not establish an unrolling benefit.

The actual Orin runs used 192 frames per round and seven rounds, with the sensor in STANDBY and the benchmark pinned to CPU 2. Run order was normal, unrolled, unrolled, normal; old/new implementation order alternated inside every run. Each number below is the pooled median of 14 timings from the two runs of that compiler configuration. CPU milliseconds include fresh-image copying and the default exposure/correction update cadence. These are descriptive measurements from two process runs per variant, not 14 independent hardware trials.

| Kernel/input | Old, normal flags (ms) | Optimized, normal flags (ms) | Optimized, unrolled (ms) | Source-change reduction | Additional unroll reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| AE, full | 125.839 | 101.243 | 99.231 | 19.55% | 1.99% |
| AE, half | 90.583 | 77.200 | 75.801 | 14.77% | 1.81% |
| BUC, full | 119.039 | 102.905 | 102.325 | 13.55% | 0.56% |
| BUC, half | 80.925 | 75.588 | 74.583 | 6.60% | 1.33% |

The source changes improved all four kernel cases. Forced unrolling showed a small additional AE benefit in both run pairs: 1.87–1.95% for full input and 1.53–2.20% for half input, comparing each pair's median. The BUC individual timing ranges overlap between compiler configurations; its smaller differences should not be treated as a general performance guarantee. The unchanged old reference also moved by up to 0.54% between the pooled normal/unrolled run groups. Raw timings, inclusive quartiles, run medians, paired ratios, and input hashes are retained in the [machine-readable analysis](../var/cpu-demand-2026-11-13/results/sdk-kernel-analysis.json).

All 224 emitted benchmark checksums agree between old/new and normal/unrolled implementations within each algorithm/input case. Scalar checksums are a benchmark sanity check; full-array behavior is covered by the separate equivalence tests. Standalone kernel cache counters were not collected.

**Decision: retain normal `-O3` production flags.** This experiment supports the source changes and records a small AE microbenchmark gain from forced unrolling, but does not establish a worthwhile whole-driver gain from that flag. Image-processing object text grows 116.19%, BUC gains are small relative to timing variation, and no end-to-end forced-unroll trial has been performed. No manual unroll or compiler default is changed.

## Real-sensor results

The Jetson Orin AGX at `10.160.1.21` ran the Mac-built Linux ARM64 images against the OS-0-128 at `10.160.1.51`, firmware 2.5.3. The sensor remained at 2048×10 LEGACY, full 360° azimuth, internal-oscillator timestamps, original points, organized/destaggered output and `IMU|PCL|SCAN|IMG|RAW|TLM`. No sensor settings or message schemas were tuned for performance. The test used ROS domain 85, namespace `/cpu_demand`, Zenoh SHM with a 256 MiB pool, host networking/IPC and unlimited memlock. A fixed baseline observer image requested the same cloud and selected image topics for both driver variants.

Each steady trial recorded a roughly 60.8-second service cgroup/driver process sample after 15 seconds of observer settling, alongside a nominal 60-second `perf stat` window and approximately 90 seconds of stream observation. **100% CPU means one occupied core**, not all 12 cores. Service CPU includes the driver and supervisor; router and observer CPU are measured separately and excluded. CPU governor/power settings were left unchanged (schedutil, MAXN); frequency remains adaptive, and this is not a thermal steady-state or fixed-frequency study. Sensor configuration, data format, generated parameters, image provenance, stream schemas and host UDP counters were saved per run. The only parameter exception is the explicitly identified always-process override trial.

| Comparison | Driver CPU | Service CPU | Output rate/schema | Status |
| --- | --- | --- | --- | --- |
| Matched baseline versus SDK-only, A–B–B–A | 49.526% → 48.014%; **3.05% relative reduction** | 61.286% → 59.425%; **3.04% relative reduction** | Cloud and four image topics ≈10 Hz; schemas unchanged | Measured; details below |
| Matched baseline versus integrated ROS + SDK, cloud subscriber/no image subscribers | 48.737% → 37.734%; **22.58% relative reduction** | 60.039% → 49.521%; **17.52% relative reduction** | Cloud ≈10 Hz; full schema unchanged | Measured A–B–B–A |
| Matched baseline versus integrated candidate, all four images requested | 49.481% → 46.389%; **6.25% relative reduction** | 61.288% → 58.552%; **4.46% relative reduction** | Cloud and all four images ≈10 Hz; schemas unchanged | Measured A–B–B–A |
| Integrated candidate with `image_always_process=true`, no image subscribers | 45.917% | 56.938% | Cloud ≈10 Hz; full schema unchanged | One override check; not a repeated comparison |

### Combined changes with every image requested

The sequence was `a1_all_images`, `b1_all_images`, `b2_all_images`, `a2_all_images`. Baseline driver CPU was 49.705% and 49.257%; candidate CPU was 46.552% and 46.226%. The **3.092 percentage-point driver reduction (6.25% relative)** therefore applies while producing the same requested outputs. It is separate from the savings for skipping unused images. The shared A1 sample also closes the SDK comparison; the comparisons are not independent statistical experiments, and their percentages must not be added.

Candidate point rates were 10.003–10.004 Hz and every requested image was approximately 10.001 Hz, with no header gaps over 150 ms. Full point fields/strides/dimensions, image encodings/strides/dimensions, sensor configuration and ROS parameters matched baseline.

| Driver counter | Baseline mean | Combined mean | Reduction |
| --- | ---: | ---: | ---: |
| Task clock | 31.401 s | 29.683 s | 5.47% |
| Cycles | 22.257 billion | 20.933 billion | 5.95% |
| Instructions | 33.785 billion | 33.778 billion | 0.02% |
| Cache references | 14.922 billion | 14.832 billion | 0.60% |
| Cache misses | 871.555 million | 670.913 million | **23.02%** |

The nearly unchanged instruction count and lower cache-miss count are consistent with improving memory access. These aggregate counters do not attribute a specific fraction of the gain to direct cloud writes versus image changes. Counter events ran 100% of their windows; all recorded host UDP errors remained zero.

### No image subscribers and the override

The matched sequence was `a1_no_images`, `b1_no_images`, `b2_no_images`, `a2_no_images`. Baseline driver CPU was 48.820% and 48.655%; optimized CPU was 37.975% and 37.494%. The **11.003 percentage-point reduction (22.58% relative)** includes the requested policy change of skipping unused image output, plus the other optimizations. Service CPU fell 10.518 points (17.52%). The same cloud subscriber stayed at approximately 10.004–10.006 Hz, with unchanged schema and no header gaps over 150 ms. Sensor settings and generated ROS parameters matched exactly.

Mean driver cycles fell 22.38%, instructions 30.84%, cache references 30.31%, and cache misses 29.97%. These changes reflect both skipped work and improved processing; they must not be described as a pure cache-loop gain. The earlier `a0_no_images` trial is retained as a preflight baseline but is not included in this matched comparison.

A separate `b3_always_no_images` run set only `image_always_process: true` on the candidate. Driver CPU was 45.917%, service CPU 56.938%, and point delivery remained approximately 10 Hz with no gaps. The option therefore restores continuous image work without requiring a subscriber. It is a one-run functional/performance check, not an independently repeated estimate. The current gate-default runs should not be compared to this override as though they were an additional A–B–B–A experiment.

### Live join, leave and rejoin

A continuous point subscriber stayed connected through five 15-second image-demand phases, without restarting the driver. Each phase allowed two seconds for discovery before rate/schema acceptance. All requested topics passed approximately 10 Hz, schema and timestamp-gap checks; the continuous point stream delivered 750 messages over the full run with no gap over 150 ms. Initial/rejoining image delivery began about 88–104 ms after subscription creation in this run. That observation is not a network-wide latency bound.

A separate sampler measured CPU over complete one-second intervals inside each settled phase:

| Image demand | Driver CPU | Service CPU | Settled sample duration |
| --- | ---: | ---: | ---: |
| None initially | 35.11% | 45.55% | 11.17 s |
| Range only | 37.79% | 48.93% | 12.17 s |
| All four | 44.19% | 55.19% | 12.17 s |
| None after leaving | 34.92% | 45.40% | 12.17 s |
| All four after rejoining | 44.21% | 54.25% | 11.15 s |

The CPU response demonstrates per-channel work selection and return to the idle-image workload after removal. These are short within-run observations using a different observer and no concurrent `perf stat`; their absolute CPU values must not be mixed with the steady-trial means above. Local subscription destruction was timed separately and does not acknowledge the exact instant remote computation stopped. The driver remained active throughout. See [transition checks](../var/cpu-demand-2026-11-13/results/demand-transition-smoke.json) and [time-aligned CPU summary](../var/cpu-demand-2026-11-13/results/transition-summary.json).

### SDK-only live-sensor comparison

The recorded sequence was `a0_all_images`, `s0_sdk_all_images`, `s1_sdk_all_images`, `a1_all_images`. Every run used ROS `82a46b8`; only the SDK revision changed from `8aa86b9` to `71b8c20`. Recorded runtime image digests were `sha256:6719bde37d7a052a7513cb67845187fa201ed05afa754ab93ee49ab7bf64edbc` for baseline and `sha256:6880c6dcee99bbab11791af3f7fa2c0fc2602d0507410537a2bae3255e16f83a` for SDK-only. The sensor stayed at full-azimuth `2048x10`, LEGACY, with identical active configuration, data format, and ROS parameter files. The observer requested the cloud plus range, signal, reflectivity, and near-IR images. Transport was Zenoh with shared memory, domain 85, namespace `/cpu_demand`.

After 15 seconds of observer warmup, the service sampler captured 60 intervals per case, totaling 60.83–60.86 seconds. The comparison uses the arithmetic mean of the two runs of each revision:

| Run | Driver CPU (% of one core) | Service CPU (% of one core) |
| --- | ---: | ---: |
| Baseline A0 | 49.348 | 60.880 |
| SDK S0 | 47.993 | 59.668 |
| SDK S1 | 48.034 | 59.181 |
| Baseline A1 | 49.705 | 61.692 |

Driver CPU fell by **1.513 percentage points**, or **3.05% relative**. Service CPU fell by 1.861 points, or 3.04%; its change also includes supervisor variation, so the entire service difference should not be attributed to SDK kernels. The supervisor mean changed from 11.052% to 10.699%. Both candidate driver measurements were below both baseline measurements, but two trials per variant do not establish a population confidence interval.

The separate nominal 60-second `perf stat` windows agree with the direction and scale of the driver change:

| Driver counter | Baseline mean | SDK-only mean | Reduction |
| --- | ---: | ---: | ---: |
| Task clock | 31.398 s | 30.492 s | 2.89% |
| Cycles | 22.202 billion | 21.544 billion | 2.96% |
| Instructions | 33.598 billion | 33.323 billion | 0.82% |
| Cache references | 14.832 billion | 14.476 billion | 2.40% |
| Cache misses | 871.385 million | 802.174 million | 7.94% |

Every counter reports 100% running time, without event multiplexing. Mean misses/references decreased from 5.875% to 5.541%, and mean IPC increased from 1.513 to 1.547. These generic ARM PMU events support reduced memory-access cost; they do not identify a particular cache level, prove a bandwidth reduction, or isolate AE from BUC. `perf` task-clock and `/proc` CPU use separate windows/accounting, so their absolute CPU values are not interchangeable. Cycles per task-clock second averaged 707.1 versus 706.6 million; the sampled median of average frequency across cores was 729.6 MHz in all four cases. Adaptive frequency scaling still limits broader extrapolation.

Each approximately 90-second observer run received 901 clouds and 900 images on each of the four topics. Cloud rates were 10.003–10.006 Hz and image rates 10.001–10.002 Hz, with no timestamp gap over 150 ms. Clouds retained the complete registered field layout, 128×2048 organization, 48-byte point stride, and 12,582,912-byte data buffer. All images retained 2048×128 `mono16`, 4,096-byte row stride, and 524,288 data bytes. Host UDP error counters remained zero. The live scene varied, so schema/rate checks are not a pixel-equality test; deterministic equivalence is established by the regression suite.

After the demand-transition check, the final candidate passed real standby/reactivation: the standby request took 9.26 seconds, no driver process remained while parked, and activation took 39.12 seconds including sensor startup. A subsequent 12-second observation received the cloud and all four image topics at approximately 10 Hz with unchanged schemas and no header gaps over 150 ms. These are functional checks, not steady latency benchmarks. The first stop logged a clean process exit after a 0.92-second lifecycle drain. The final teardown drained successfully in 1.00 second and logged a lifecycle destructor warning plus termination by SIGINT (exit −2); no SIGTERM/SIGKILL escalation or runtime abort was recorded. Both raw queues reported zero overflow, publication errors or discarded packets at that stop.

The sensor was then parked, every active configuration value was compared equal to the original snapshot, and all isolated driver/router/observer containers were removed. Final state is STANDBY with lifecycle absent; only the preexisting registry container remains. Images and local profiling artifacts are retained. See [final sensor configuration](../var/cpu-demand-2026-11-13/results/final-sensor-config.json), [final state](../var/cpu-demand-2026-11-13/results/final-state.json), and [container inventory](../var/cpu-demand-2026-11-13/results/final-containers.txt).

These findings cover one physical OS-0-128, firmware 2.5.3 and the specified full-azimuth mode/transport/subscriber graph. Deterministic tests cover additional formats, masks and point layouts, but this is not hardware validation of every profile, dual-return sensor, multi-sensor rig, network fault or long-duration thermal condition. Live scenes differ between trials; numerical equivalence is established by the frozen-oracle tests rather than by matching live pixels or returns. Exposure history while unsubscribed intentionally differs from continuous processing. No whole-driver forced-unroll trial or end-to-end zero-copy transport change is claimed.

## Local evidence

These study directories are ignored by Git and are retained locally:

- [Combined and SDK-only builds](../var/demand-build-2026-11-13/): exact source manifests, runtime build metadata, and logs.
- [SDK handoff](../var/demand-build-2026-11-13/sdk/sdk-image-processing-handoff.json): source and binary hashes, source patch, normal/unrolled regression XML, benchmark binaries and CSVs, compiler reports, and disassembly.
- [Image tests](../var/image-demand-test-20261113/): gate and cache/span results, logs, and assembly summary.
- [Cloud tests](../var/direct-cloud-build-2026-11-13/summary.json): Release and sanitizer results, matrix coverage, allocator-ABI note, and assembly evidence.
- [Hardware study](../var/cpu-demand-2026-11-13/): harnesses, raw per-run records, [matched comparison](../var/cpu-demand-2026-11-13/results/comparison.json), demand transitions, lifecycle checks and verified restoration.

The integrated source is in `var/ros-demand-integrated`; the SDK source is in `var/sdk-image-processing-optimization`. The earlier CPU, transport, azimuth, backport, and executor findings remain in the linked reports rather than being replaced by this experiment.
