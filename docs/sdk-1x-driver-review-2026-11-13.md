# Ouster ROS and SDK 1.x architecture review

Reviewed November 13, 2026, against the pinned service and current official upstream sources. The hardware measurements and limitations are in the [CPU profile report](cpu-profile-2026-11-13.md).

A custom C++ ROS driver could plausibly use less CPU by eliminating work identified in the existing ROS pipeline. **An SDK upgrade alone has no demonstrated CPU benefit here.** The safest next step is to optimize the current fork, retain its correctness fixes, and benchmark a bounded direct-PointCloud2 prototype before committing to a replacement driver. No replacement implementation or SDK 1.x hardware benchmark was performed in this review.

## Versions and upstream changes

| Component | Reviewed version | Implication |
| --- | --- | --- |
| Deployed ROS fork | `40f57c34ef6a36d797d1cf6e3c0e7624313d435f` | Includes local timestamp, raw recording and mask-loading fixes. |
| Deployed SDK fork | `8aa86b94b1c75dfcb99159a4147b44d973e52323` | Based on SDK 0.16.2, with local packet-safety fixes. |
| Official ROS latest release tag | [`0.15.1`](https://github.com/ouster-lidar/ouster-ros/releases/tag/0.15.1), `73cf03684ae3576b40a263ae31bb64a617268aca` | Release tags differ from the current development branch. |
| Official ROS `ros2` branch | [`bbe9a890012226f7d9354250bbe212852b2443f2`](https://github.com/ouster-lidar/ouster-ros/tree/bbe9a890012226f7d9354250bbe212852b2443f2), September 24, 2026 | Its package declares 0.15.3, but it still pins SDK 0.16.2 (`358c256d93592b9002d26315873269a2d4523c5d`). It is not an SDK 1.x port. |
| Official SDK latest release | [`1.0.1`](https://github.com/ouster-lidar/ouster-sdk/releases/tag/v1.0.1), `46b7bceee7995914e2f3f3facf4d78418335b2d7` | API migration required; performance must be established separately. |

Official ROS now replaces the per-point destagger modulo with a normalized row shift and conditional addition. Backport both [the optimization](https://github.com/ouster-lidar/ouster-ros/commit/f4c83a6115c006d872526a21b738f5e7ae606a03) and [the subsequent negative-shift correctness fix and tests](https://github.com/ouster-lidar/ouster-ros/commit/bbe9a890012226f7d9354250bbe212852b2443f2). The current fork's measured ARM64 binary performs two integer divisions per point; moving the organized row-index calculation outside the inner loop is another focused opportunity. The latest upstream loop still contains that row-index expression.

Wholesale replacement with official ROS would lose local behavior that matters under load: [timestamps stored with queued scans](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/src/lidar_packet_handler.h#L90), [bounded raw publication workers](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/include/ouster_ros/raw_packet_publisher.h), configurable reliable raw QoS and replay support, and mask decoding without OpenCV. Current official ROS still publishes raw packets from its receive callback and keeps estimated scan timestamps in shared handler members. Port these local fixes and their regression tests before any upstream rebase or replacement.

## Where the measured opportunity is

The default Fast DDS service used about **47% of one core**, including **42% in the driver**, without external subscribers. With a point observer and Zenoh SHM, service/driver/supervisor CPU was **58.3% / 46.8% / 10.9%**; disabling IMG, RAW and SCAN reduced that to **46.8% / 35.3% / 11.0%** while preserving point delivery. The supervisor cost remains unless addressed separately.

Baseline perf attributed 24.33% of **sampled driver user cycles** to cloud assembly plus Cartesian calculation and 27.68% to named image paths. These are not service CPU percentages. Inclusive telemetry samples overlap middleware functions. See the [measurement report](cpu-profile-2026-11-13.md) for A/B results and limitations.

The current cloud path calculates a full float XYZ array, assembles a PCL cloud, and copies it into PCLPointCloud2 before moving its data into the ROS message. [That staging copy is explicit in the source](https://github.com/christomaszewski/ouster-ros/blob/40f57c34ef6a36d797d1cf6e3c0e7624313d435f/ouster-ros/src/point_cloud_processor.h#L78). The final move is not another bulk copy. The default `original` format is 48 bytes per point: 128×2048 produces 12 MiB per frame, or 120 MiB/s at 10 Hz before transport overhead. LEGACY `native` is also 48 bytes and changes field names/types; it does not transparently reduce this payload. Compact formats require an explicit consumer contract change.

Reducing the sensor azimuth window already reduced CPU in the hardware test, but organized clouds retained all 128×2048 slots and the same 12 MiB payload. Missing columns reduce incoming packet work and some valid-pixel processing; full-frame cloud/image passes remain. A custom path can skip calculations for invalid slots, but must still initialize those slots and publish the existing layout. Cropping or compacting the cloud changes that layout.

## What SDK 1.0.1 changes

Source comparison found the Cartesian loop, scalar/grayscale auto-exposure implementation and beam-uniformity correction bodies unchanged from the pinned 0.16.2 base after normalizing names and formatting. Selective field parsing and float Cartesian calculations were already available. This is evidence against assuming a version-driven speedup, not a benchmark of every SDK path.

The [official migration guide](https://github.com/ouster-lidar/ouster-sdk/blob/46b7bceee7995914e2f3f3facf4d78418335b2d7/docs/migration/migration-0.16.2-1.0.rst) describes module-specific headers, `ouster_client` becoming `ouster_core`, `LidarScan` becoming `LidarFrame`, and replacement of the old client polling APIs with `SensorPacketSource`. Build files, packet handling, IMU access and batching code need migration; changing a dependency pin is insufficient.

SDK documentation still lists OS0 and firmware 2.4 or later as supported, covering this sensor's model and firmware 2.5.3; SDK 1.x was not hardware-tested here.

Convenience APIs also require care. [`XYZLutT::operator()`](https://github.com/ouster-lidar/ouster-sdk/blob/46b7bceee7995914e2f3f3facf4d78418335b2d7/ouster_core/include/ouster/core/xyzlut.h#L139) allocates an output matrix on each call. The in-place Cartesian helper is in an implementation namespace. [`get_packet()`](https://github.com/ouster-lidar/ouster-sdk/blob/46b7bceee7995914e2f3f3facf4d78418335b2d7/ouster_sensor/src/sensor_packet_source.cpp#L722) returns a packet backed by source-owned, reused storage, while [the packet iterator copies it](https://github.com/ouster-lidar/ouster-sdk/blob/46b7bceee7995914e2f3f3facf4d78418335b2d7/ouster_sensor/src/sensor_packet_source.cpp#L798). The higher-level [frame source allocates a new LidarFrame after each completion](https://github.com/ouster-lidar/ouster-sdk/blob/46b7bceee7995914e2f3f3facf4d78418335b2d7/ouster_sensor/src/sensor_frame_set_source.cpp#L205). Wrapping those APIs without examining ownership can introduce allocations or copies that the current driver avoids.

## A bounded design to evaluate

1. **Retain the supervisor and ROS contracts:** lifecycle, standby/wakeup, configuration, metadata, TF, reconnect and health. Adapt SDK 1.x acquisition and batching without simultaneously redesigning the wrapper.
2. **Use bounded buffers with explicit ownership.** Queue frame, metadata and sensor/header timestamps together. Raw workers must own packets beyond the next receive call. Preserve receive-thread isolation, overflow counters and shutdown behavior.
3. **Construct PointCloud2 directly, preserving the original schema first.** Precompute row shifts/indices and write fields into final offsets using appropriately owned buffers. Remove PCL staging; separately evaluate fused XYZ calculation and assembly. Preserve names, types, offsets, stride, units, timestamps, organization and invalid points. This does not guarantee zero-copy middleware publication.
4. **Select fields and outputs explicitly.** Original clouds require range, signal/intensity, reflectivity, near-IR/ambient and timing; dual returns need corresponding fields. Make optional processing configurable or subscriber-driven, with always-on recording modes and defined image-exposure warmup. Asynchronous discovery must not gate acquisition, health or required raw recording.
5. **Add a compatible telemetry policy:** retain per-packet publication by default; offer heartbeat plus prompt status-change publication. Preserve stream-liveness detection even when values are unchanged, and coordinate supervisor subscriptions/timeouts.

Scope the prototype to the measured OS-0-128 LEGACY configuration; label unsupported profiles explicitly. General sensor support requires further acceptance testing.

## Regression and benchmark gates

Before production use, require the following:

- **Data:** compare fields, types, offsets, strides, frames, returns and invalid points; test masks, limits, destaggering, row reduction and extrinsics. Specify numerical tolerance if arithmetic order changes.
- **Timing/loss:** test partial, reordered and dropped packets, missing edge columns, wrapping azimuth windows and completeness thresholds. Preserve per-point `t` and header timestamps during backlog in sensor, PTP-offset and ROS-time modes.
- **Safety/recording:** retain truncated/oversized UDP and 48-byte LEGACY IMU tests, including buffer reuse; verify reliable raw QoS, slow recorders, bounded overflow, metadata and bag/PCAP replay without blocking reception.
- **Lifecycle/ownership:** exercise standby, reconnect, metadata/mode changes and shutdown with occupied queues; detect premature buffer reuse.
- **Measurements:** use the same Mac build/Orin deployment, sensor settings, outputs, RMW, QoS and subscriber graph. Compare current fork, backports and prototype separately; isolate SDK version when claiming SDK gains. Repeat no-consumer, production-consumer and raw-recording cases, including thermal steady state.
- **Acceptance:** report driver/supervisor/router/subscriber CPU, allocations, memory, drops, cadence, latency, UDP counters, frequency and temperature. Require equivalent data and requested cadence, including 10 Hz clouds. Gains must exceed run variation without hidden output loss or lifecycle/recording regressions; no numerical saving is guaranteed.

Prioritize corrected row-shift backports, invariant loop work, optional outputs, telemetry policy and direct PointCloud2 benchmarking. SDK 1.x provides a newer dependency foundation; the ROS pipeline and publication changes are the concrete performance opportunities.
