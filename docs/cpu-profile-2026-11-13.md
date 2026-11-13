# Ouster CPU profile on Jetson Orin AGX

On November 13, 2026, the latest Ouster wrapper was built on an Apple Silicon Mac and transferred to the Orin at `10.160.1.21`. Tests used the real OS-0-128 at `10.160.1.51`, firmware 2.5.3, at **2048×10 with LEGACY packets**. The default pipeline consumed about **47% of one CPU core** without external subscribers; keeping point clouds, IMU and telemetry reduced that to about **34%**. This workload did not saturate the 12-core Orin.

The main avoidable costs are unconditional image generation and per-packet telemetry publication. Cloud generation is substantial required work when clouds are needed; its indexing and staging can still be optimized. A separate large-cloud delivery problem appeared in the tested default Fast DDS path; lower processing CPU did not eliminate its subscriber gaps.

## Build and hardware

| Item | Value |
| --- | --- |
| Wrapper | `c36c75088479b6dc0e0d492bae34c964fe336a04`, fetched from origin and fast-forwarded to latest `origin/main` |
| ROS driver | `40f57c34ef6a36d797d1cf6e3c0e7624313d435f` |
| SDK | `8aa86b94b1c75dfcb99159a4147b44d973e52323` |
| Local image | `ouster_driver:cpu-profile-c36c750`, Linux ARM64 |
| Image digest | `sha256:62c4f6969ded1b88e26c61c48ec494ad3e185741fb84507f9b4e1a6f5479c1f2` |
| Fleet base digest | `sha256:bf5921ad3fb5fd74bff0dda1bc1666e7da4e292ee6948a4d11fcdd485d5a63d9` |
| Build mode | Release; exact matching Docker build and compiler layers reused; all 14 runtime shared libraries passed the independent ABI load check |
| Orin | 12 CPU cores, Ubuntu 24.04, kernel `6.8.12-1021-tegra` |
| Power and governor | Existing MAXN / `schedutil`, unchanged |
| Test isolation | ROS domain 83, namespace `/cpu_profile`, dedicated Compose project and test router |

The sensor started in standby, with no Ouster driver running. After testing, all original active settings were verified identical, including the full 360° azimuth window, the sensor was confirmed in STANDBY, and the test driver, observer and router containers were removed. The azimuth experiment temporarily changed the active window; no settings were persisted to sensor flash, and all original active settings were restored afterward. Only the isolated test deployment was started; existing application deployments were not changed.

## Processor measurements

**100% means one fully occupied core**, not the entire machine. Each row is a 40-second Docker cgroup CPU measurement after a ten-second settling period, with sensor startup complete. Process CPU is measured separately from `/proc`; container totals also include transient healthcheck processes. The first baseline overlapped low-rate perf sampling; the repeat did not.

| Enabled processors | Entire service | C++ driver | Supervisor and health |
| --- | ---: | ---: | ---: |
| Default `IMU\|PCL\|SCAN\|IMG\|RAW\|TLM` | 47.02% | 41.91% | 4.05% |
| Default without `IMG` | 36.70% | 32.09% | 3.98% |
| `IMU\|PCL\|TLM` | 33.75% | 29.15% | 3.96% |
| `IMU\|TLM` | 18.73% | 14.42% | 3.69% |
| `IMU` | 5.01% | 3.57% | 0.89% |
| Default repeated | 47.31% | 41.93% | 4.75% |

These are short, isolated single-sensor runs, not a full vehicle workload with raw recording, multiple consumers or thermal steady state. These are observed costs under dynamic CPU frequency scaling, not frequency-normalized instruction benchmarks. The default repeat was within 0.3 percentage points of the initial measurement. All rows kept the sensor at 2048×10 LEGACY. Removing PCL, TLM or other processors removes those outputs; those rows isolate costs rather than describe equivalent output configurations.

## Point cloud delivery and transport

A local raw-CDR Python subscriber used sensor-data best-effort QoS, depth 5, in a separate container sharing host networking and IPC. Subscriber CPU is excluded from service CPU below. The observer counted messages and header timestamp gaps over approximately 65 seconds; the service CPU window was 40 seconds after settling. Zenoh SHM was configured on the driver, subscriber and router, with a 256 MiB session pool and unlimited memlock. TCP used the same router with session SHM disabled.

| Transport and processors | Service CPU | Received clouds | Timestamp gaps over 150 ms |
| --- | ---: | ---: | ---: |
| Fast DDS, default processors | 53.25% | 6.34 Hz | 83 |
| Fast DDS, IMU/PCL/TLM | 39.68% | 4.52 Hz | 128 |
| Zenoh SHM, default processors | 58.30% | 10.01 Hz | 0 |
| Zenoh SHM, IMU/PCL/TLM | 46.80% | 10.01 Hz | 0 |
| Zenoh TCP, default processors | 70.22% | 10.00 Hz | 0 |

Every received cloud had a 128×2048 layout, 48-byte point stride and 12,582,912 data bytes. All Zenoh cases sustained about 10 Hz without timestamp gaps over 150 ms in these observation windows. The default Fast DDS path dropped deliveries to this observer even when optional processing was disabled. This comparison isolates a delivery problem in the tested DDS path; it does not identify its exact cause or establish that every DDS configuration behaves this way. DDS and Zenoh CPU figures are not equal-throughput comparisons when the observer delivery rates differ.

Host UDP error counters did not increase, and captured driver measurement logs showed no scan-throttling or packet-drop warnings. Those checks do not prove lossless sensor acquisition. No DDS transport tuning was attempted. The same observer received the full cadence over Zenoh, so a universal observer throughput limit is unlikely.

## Azimuth window comparison

Reducing azimuth alone reduced service CPU, with all other settings held constant: 2048×10, LEGACY, default processors, original point type, organized clouds, Zenoh SHM, and the same local subscriber. Active sensor snapshots differed from the initial configuration only in the requested azimuth window and NORMAL versus the original STANDBY operating mode.

| Azimuth | Entire service | C++ driver | Received clouds | Host UDP datagrams per second |
| --- | ---: | ---: | ---: | ---: |
| 360° | 58.30% | 46.83% | 10.01 Hz | 1389 |
| 180° | 41.42% | 33.68% | 10.01 Hz | 762 |
| 90° | 31.86% | 27.11% | 10.01 Hz | 439 |
| 360° repeat | 58.25% | 46.89% | 10.01 Hz | 1389 |

The 180° window reduced total service CPU by about 29%; 90° reduced it by about 45%, relative to the 360° measurement. The final 360° repeat returned within 0.05 percentage points of the first. UDP counts are host-wide and include IMU and incidental traffic; they demonstrate the reduced incoming workload, not an exact lidar-only packet count. The sensor reported column windows `[1024, 0]` at 180° and `[1536, 0]` at 90°, with wraparound and inclusive endpoints, so packet counts are not exactly one-half or one-quarter of a full revolution.

**The organized cloud did not shrink.** Every case published 128×2048 points, a 48-byte point stride and 12,582,912 data bytes. The SDK zeros missing range/channel data outside the window. The ROS Cartesian conversion maps zero range to **NaN XYZ**, while retaining the organized slots; these are not zero-position points. Other fields are not necessarily all zero, such as the retained ring index. All cases sustained approximately 10 Hz with no header timestamp gaps over 150 ms in the observation windows.

Narrowing the window reduces UDP parsing, raw-packet copying and telemetry publication. Full-size scan arrays, cloud indexing, staging copies and payload transport remain. Some image/Cartesian work also becomes cheaper because zero ranges take simpler paths and image algorithms ignore zero values in parts of their calculations. This explains useful savings that are smaller than the angular reduction.

Setting `organized: false` is a separate option: invalid XYZ points are omitted and the cloud becomes an unorganized list. It can reduce payload size, but changes the layout expected by consumers and was not part of the azimuth-only comparison. Keep `min_scan_valid_columns_ratio` at its default zero when narrowing the window unless a compatible threshold is chosen; the current driver compares valid columns with the full frame width, so an overly high threshold can reject narrowed scans.

## What consumes CPU

1. **Images are generated without image subscribers.** The [default processor mask](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/src/os_driver_node.cpp#L41) enables every processor. [Image processing](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/src/image_processor.h#L111) copies channels, destaggers, adjusts exposure, and corrects beam uniformity. Named image paths accounted for 27.68% of baseline sampled driver user cycles. Removing IMG saved about ten percentage points of one core in the A/B run.

2. **Telemetry publishes once per lidar packet.** At 2048×10 and 16 columns per packet, that is 1,280 publications per second. [Publication occurs directly in the lidar packet callback](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/src/os_driver_node.cpp#L241). The IMU-only A/B isolated about 13.7 percentage points of total service CPU from TLM. Perf attributed 19.54% of sampled driver user cycles inclusively to telemetry publication, versus 0.32% to converting packet fields into a telemetry message. Publication, middleware and wakeups dominate that cost. The latest [health adapter](../docker/entrypoints/health_ros.py) already samples a raw depth-one subscription once per heartbeat; the earlier per-packet Python callback problem is fixed. Dropping TLM disables its stream-health check.

3. **Cloud assembly processes 2.62 million points per second.** The original point format has a 48-byte stride: a 128×2048 cloud is 12 MiB, or 120 MiB/s at 10 Hz before serialization overhead. [Assembly](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/src/point_cloud_compose.h#L136) performs two integer divisions per point in the tested binary: destagger modulo and the organized output-row index. ARM64 disassembly confirmed both survive optimization. Cloud assembly plus Cartesian calculation accounted for 24.33% of sampled driver user cycles. The [PCL conversion](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/src/point_cloud_processor.h#L80) adds a whole-cloud copy into PCLPointCloud2; the subsequent move into the ROS message is not another bulk copy.

4. **RAW and SCAN add work even without consumers.** Dropping these after IMG saved about three more percentage points. RAW copies each packet into a bounded worker queue; SCAN copies full range and signal fields to produce one ring. These outputs are useful when explicitly required, especially RAW for packet recording and replay.

The sampled percentages describe **driver user cycles**, excluding kernel and Python CPU. Inclusive telemetry publication overlaps middleware functions, so it must not be added to middleware self-cycle totals. No normal-path busy loop was identified: UDP reads block in `select`, scan and raw workers use condition variables, and the supervisor uses bounded waits.

## Practical changes

If consumers need clouds and IMU but not images, LaserScan or raw packets, use:

```yaml
driver_params:
  proc_mask: IMU|PCL|TLM
```

This retains the current cloud schema, acquisition mode and telemetry-based health check. Do not apply it to deployments that record raw packets or consume the omitted topics.

For code optimization, prioritize configurable publication of unchanged telemetry at a bounded heartbeat rate while publishing status changes promptly. Keep the existing per-packet cadence as the compatibility default: changing it changes the documented topic timing and may affect recorders or other consumers. Independently, backport upstream ROS conversion optimization `f4c83a6` together with correctness fix `bbe9a89`, then assess further row-index hoisting without changing output fields. See [the SDK 1.x architecture review](sdk-1x-driver-review-2026-11-13.md). A later direct PointCloud2 construction path could remove the PCL staging copy. Cloud changes need output-equivalence tests; telemetry changes need cadence and health tests. Their additional savings were not measured here.

Changing `point_type` is a data-contract decision. `native` still uses 48 bytes per point for LEGACY and changes field names/types, so it is not a transparent size reduction. `xyz` uses 16 bytes but drops intensity, per-point timestamps, ring and other fields. `v_reduction` skips rows during cloud assembly but still computes XYZ for the full scan.

## Reproduction and evidence

The Mac's local evidence directory is `var/cpu-profile-2026-11-13/` in this checkout; it is ignored by Git. It contains the build log and provenance, CPU sampler, case runners, point observer, configuration, perf reports, and JSON measurements. The Orin retains the isolated study under `/home/uxv/ouster-cpu-profile-20261113/`, including an extracted perf binary; no OS profiling packages, governor changes or sysctl changes were installed/applied.

To reproduce on this Orin with the retained image and scripts:

```sh
cd /home/uxv/ouster-cpu-profile-20261113
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
unset ZENOH_CONFIG_OVERRIDE PROFILE_SHM
python3 run_case.py baseline_new
python3 run_case.py pcl_new '{"proc_mask":"IMU|PCL|TLM"}'
python3 run_sub_case.py subscriber_new '{"proc_mask":"IMU|PCL|TLM"}'
```

The scripts are specific to this study's paths, host, sensor, image and ROS domain. Use new labels to keep prior evidence. The case runners deliberately keep the sensor running between cases; finish by putting the test service into standby and stopping it. Credentials are not stored in these artifacts.
