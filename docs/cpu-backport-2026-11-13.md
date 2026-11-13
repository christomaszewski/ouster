# Cloud conversion optimizations on Jetson Orin AGX

The upstream cloud-indexing backport reduced measured C++ driver CPU from **50.10% to 48.18% of one core**, a **3.83% relative reduction**, in an A–B–B–A comparison with the real OS-0-128. Service CPU fell from 62.39% to 59.92%. The tests retained every enabled processor, the original organized cloud format, the sensor configuration and the subscriber workload.

A further small change moves the remaining organized row-index division outside the per-point loop. Both candidates pass 179 C++ tests and eight Python replay tests. ARM64 disassembly confirms that the further change removes integer division from the tested original/LEGACY inner loop. Its hardware result is recorded separately below; reducing an instruction count alone is not a measured CPU saving.

This follows the [initial CPU profile](cpu-profile-2026-11-13.md) and [SDK/driver architecture review](sdk-1x-driver-review-2026-11-13.md). The SDK, point schema and output cadence were kept fixed rather than introducing a new driver implementation.

## Implemented changes

**B — upstream destagger optimization and correctness fix.** Backported upstream [f4c83a6](https://github.com/ouster-lidar/ouster-ros/commit/f4c83a6115c006d872526a21b738f5e7ae606a03) together with [bbe9a89](https://github.com/ouster-lidar/ouster-ros/commit/bbe9a890012226f7d9354250bbe212852b2443f2). The converter calculates a normalized row shift once per row and uses subtraction plus conditional addition for each point, replacing the per-point modulo. The follow-up is necessary for negative sensor row shifts and also moves the destagger choice outside the point loop; metadata validation enforces the required shift range. Existing timestamp, raw-packet, mask and receive-safety changes remain in the fork.

**D — hoist the remaining row calculation.** On top of B, `point_cloud_compose.h` calculates `u * w` and, for organized output, `(u / rows_step) * w` before entering the column loop. The point loop adds the column to these row bases. This is a four-line addition/two-line removal in one file, with no executor, image, field, schema or SDK changes.

In the actual Release ARM64 component, B's organized original/LEGACY loop contains `sdiv` at `0x196428` within the loop at `0x196420`–`0x1964dc`. D's corresponding loop at `0x195620`–`0x1956d4` contains no integer division. Its row-shift and target-row divisions precede the loop at `0x1955dc` and `0x1955ec`. At the tested 2048×128 layout with no row reduction, the remaining target-row division falls from 262,144 to 128 executions per frame. That 2048-fold reduction applies to this operation, not to total CPU. The compiler already effectively hoisted the source-row multiplication; the output-row division is the substantive additional saving.

## Hardware comparison

The Orin at `10.160.1.21` received Linux ARM64 images built on the Apple Silicon Mac. The real OS-0-128 at `10.160.1.51` ran firmware 2.5.3, LEGACY packets, 2048×10, full 360° azimuth, internal-oscillator timestamps, `original` points, `organized: true`, `destagger: true`, and all `IMU|PCL|SCAN|IMG|RAW|TLM` processors. Zenoh SHM, the 256 MiB session pool, local raw-CDR point subscriber and health supervisor were unchanged. The isolated test used ROS domain 84 and namespace `/cpu_backport`.

CPU was sampled for approximately 60 seconds after a 15-second settling period, with a 90-second point observation window. **100% CPU means one occupied core**, not all 12 Orin cores. Service figures include the driver, supervisor and transient health processes; separate router and subscriber CPU is excluded. Each run also recorded `perf stat` counters, active sensor configuration, generated ROS parameters, image provenance, schema and host UDP counters.

| Run order | Service CPU | Driver CPU | Received clouds | Header gaps >150 ms |
| --- | ---: | ---: | ---: | ---: |
| A1, baseline | 61.99% | 50.10% | 10.006 Hz | 0 |
| B1, backport | 59.79% | 48.11% | 10.004 Hz | 0 |
| B2, backport | 60.05% | 48.24% | 10.006 Hz | 0 |
| A2, baseline | 62.78% | 50.09% | 10.004 Hz | 0 |

The B mean saves **1.92 percentage points of driver CPU (3.83%)** and **2.46 percentage points of service CPU (3.94%)**. Mean sampled driver instruction count fell 2.92%, and cycles fell 4.38%. Frequencies remained dynamically governed; these are short real-workload measurements, not a fixed-frequency microbenchmark or a statistical confidence interval. The baseline driver repeat differed by only 0.01 percentage points. Service CPU is noisier because the health process varies independently.

All four runs retained 128×2048 slots, a 48-byte point stride, 98,304-byte row stride, 12,582,912 data bytes and the same nine named fields, types and offsets. Every active sensor configuration matched the original apart from its operating state. Host UDP receive/error counters showed no new errors. Received timestamp cadence stayed near 10 Hz; observer arrival gaps reached approximately 0.37 seconds, so this is not a claim of bounded end-to-end latency. Live scenes differ between runs: deterministic fixtures establish field equivalence, while hardware checks establish the observed schema and delivery behavior.

The separate **B3–D1–D2–B4** sequence measured the additional row-loop change:

| Variant | Service CPU, mean | Driver CPU, mean | Cycles over 60 s, mean | Instructions over 60 s, mean |
| --- | ---: | ---: | ---: | ---: |
| B3/B4 reference | 59.361% | 48.071% | 21.813 billion | 33.771 billion |
| D1/D2 row loop | 59.393% | 48.045% | 21.620 billion | 33.588 billion |

Driver CPU differed by only −0.025 percentage points (−0.053% relative), while service CPU increased 0.031 points. This is **effectively unchanged total CPU**, within the observed run variation. Cycles decreased 0.89% and instructions 0.54%; those small counter differences support reduced work but do not establish a material service-CPU benefit. Both D trials retained the identical parameters, sensor settings, full cloud schema and approximately 10.00 Hz delivery, with no timestamp gaps over 150 ms or new UDP errors. The small change is retained for its simpler invariant indexing and verified generated code, without claiming an additional practical CPU reduction.

The final wrapper pins D in all three ROS/message-source locations. Its ancestry includes B and is retained on [the tested fork branch](https://github.com/christomaszewski/ouster-ros/tree/codex/backport-cloud-conversion-optimizations).

The separate **B4–C1–C2–B5** sequence used the unchanged B driver component in the stock EventsCBG container. B4 also closes the preceding loop comparison; these are matched pairs, not independent additional samples.

| Variant | Service CPU, mean | Driver/container CPU, mean | Cycles over 60 s, mean | Instructions over 60 s, mean |
| --- | ---: | ---: | ---: | ---: |
| B4/B5 standalone reference | 60.259% | 48.470% | 21.923 billion | 33.960 billion |
| C1/C2 EventsCBG component | 59.875% | 48.119% | 21.771 billion | 33.947 billion |

The mean driver/container difference is −0.351 percentage points (−0.72%), with instructions only 0.04% lower. The standalone references themselves moved from 48.168% to 48.772%, a larger change than the apparent executor benefit. These short dynamic-frequency trials therefore show **no convincing material CPU improvement**. C1/C2 delivered 10.006/10.005 Hz, with no header gaps over 150 ms and the identical schema, parameters and sensor settings. This configuration uses the executor's default worker count on the 12-core Orin, while the baseline spins a single executor thread; the native processing workers are unchanged. It is a configuration comparison, not a controlled test of queue implementation alone. The wrapper keeps the standalone executor. Functional checks are documented below.

## Build and regression provenance

| Variant | ROS source revision | Local image tag |
| --- | --- | --- |
| A | `40f57c34ef6a36d797d1cf6e3c0e7624313d435f` | `ouster_driver:cpu-profile-c36c750` |
| B | `b9b6f8f5fffaff3e64af42a99dda7d1bf0e57caf` | `ouster_driver:cpu-backport-20261113-b9b6f8f` |
| D, parent B | `82a46b870451d9dac2d9eb1db8a953a6bff1d3b9` | `ouster_driver:cpu-loop-20261113-82a46b8` |
| C, experimental launch only | same source and binaries as B | `ouster_driver:cpu-events-component-20261113-b9b6f8f` |

The wrapper started at fetched `origin/main` revision `c36c75088479b6dc0e0d492bae34c964fe336a04`. Every variant uses SDK `8aa86b94b1c75dfcb99159a4147b44d973e52323` and fleet base `localhost:5000/fleet-ros@sha256:bf5921ad3fb5fd74bff0dda1bc1666e7da4e292ee6948a4d11fcdd485d5a63d9`. Builds use native Linux ARM64 Docker on the Mac, ROS Lyrical, GCC 15.2.0 (`15.2.0-16ubuntu1`), CMake 4.2.3, Ninja and unchanged Release `-O3 -DNDEBUG` flags. Source archives are exported from the exact commits and supplied as named Docker contexts; both ROS/message pins match in each isolated wrapper context.

Image digests:

- A: `sha256:62c4f6969ded1b88e26c61c48ec494ad3e185741fb84507f9b4e1a6f5479c1f2`.
- B: `sha256:edadbe491719a295edfad1c0bbc6acfc92611fd3164b78adcb56f0721765d15c`.
- D: `sha256:6719bde37d7a052a7513cb67845187fa201ed05afa754ab93ee49ab7bf64edbc`.
- C: `sha256:25924c5395daf6f689f2773acd9a1b98f1ff26bdf6e5d7b74cb172c49a3afaa9`. A full 44,568-entry filesystem inventory against B found only `driver.launch.py` changed; image configuration also adds an experiment label. Source provenance and driver binaries match B exactly.

B's production changes compiled in 56.3 seconds. Its final test-only commit reused all 128 compilation results and completed the package build in 3.06 seconds. D performed an incremental production build in 32.6 seconds. These package-build timings exclude export, transfer and other stages. Both runtime candidates passed independent loading checks for all 14 shared libraries and pin consistency checks.

For **each of B and D**, the test run passed 179 C++ cases with zero failures, errors or disabled cases: receive bounds (9, ASan), legacy IMU bounds (6, ASan), main ROS suite (118), lidar/timestamp handling (8), raw QoS (9), raw publisher (7), raw sensor (2) and masks (20). Eight Python replay-launch tests also passed. Test enabling reused 75 production objects and left the production install and compiler configuration unchanged. The wrapper runner now explicitly executes `ouster_ros_test`, so its coverage is part of the normal upstream gate. The 25 relevant wrapper fetch/runtime-Dockerfile/build-reuse tests also passed, as did shell syntax, manifest consistency and diff checks.

The main suite includes 96 added equivalence cases against the previous modulo algorithm: widths 7, 1024 and 2048; height 128; organized and unorganized output; both destagger modes; row steps 1 through 128 in powers of two; and negative/boundary shifts. They compare point order, all nine named fields, NaNs, timestamp clamping/wrapping and the original 48-byte PointCloud2 field layout. Row steps above 16 exercise low-level boundaries, without expanding the public ROS parameter range. This comparison is scoped to LEGACY input and the `original` point type; it does not establish equivalence for every packet profile or point type, nor byte equality of unspecified padding. Fixtures supply synthetic XYZ; they do not independently validate Cartesian calibration, masks, dual returns, images or middleware delivery.

## Threads, copies and executor experiment

The current combined driver already separates sensor acquisition, scan processing and raw publication into native worker threads. UDP reads block in `select`, and workers wait on condition variables. Cloud/image work does not run chiefly in the ROS executor. Earlier thread measurements placed the main executor at about 0.025–0.049% of one core while the full driver consumed tens of percentage points. An executor replacement therefore has limited scope to improve the identified hot loops, and its default worker count can add overhead.

| Work | Current execution context |
| --- | --- |
| UDP reception and packet dispatch | Native sensor thread |
| Telemetry conversion/publication | Synchronous on the sensor receive path |
| Cloud, LaserScan and images | Processors run sequentially on the native scan-processing thread |
| Raw lidar and IMU publication | Two native queue workers |
| Lifecycle, services, parameters and timers | ROS executor callbacks |
| Wrapper health/temperature supervision | Separate Python process with its own ROS/network threads |

These are tasks within the combined `OusterDriver` lifecycle component, not independently scheduled ROS components. Putting this same component in a container retains the native workers. Splitting into the available sensor/cloud/image components would add raw-packet subscriptions and copies; cloud/image each perform their own scan assembly. It also requires explicit telemetry enablement, lifecycle coordination and frame/timestamp compatibility work: the split cloud path timestamps received packets at its subscription callback and copies packet payloads, and the image path has its own frame handling. It is not equivalent to changing the combined driver's executor.

The receive-to-scan-worker handoff uses owned scan-ring slots rather than copying an entire scan. Raw publication copies into an owned bounded queue, then swaps the worker buffer. Those ownership choices prevent asynchronous users from seeing overwritten data. There is no loaned point-message publication path: the cloud publisher calls `publish(*msg)`.

The point path still computes a full float XYZ array, assembles a PCL cloud, and copies that cloud into PCLPointCloud2 before moving its data into the ROS message. B and D reduce indexing work; they do not remove this whole-cloud staging copy. Zenoh SHM reduces transport overhead in the measured local-subscriber configuration, but does not establish end-to-end zero-copy publication or remove conversion, staging, serialization and Python health work. Constructing the final PointCloud2 buffer directly is a separate possible optimization requiring ownership, lifetime and schema tests. SDK 1.x alone does not demonstrate a fix for those costs; see the architecture review.

A standalone EventsCBGExecutor build, ROS revision `31b4ab462ad3a73f1a0eaeb58e2559cefe9a9041`, changed only the generated driver's executor selection. It started but aborted on SIGINT in all three sensor-free trials with `Node '/os_driver' needs to be associated with an executor`; the baseline exited cleanly in all three. Generated-main cleanup removes the node after the event executor's shutdown has already removed it. This standalone candidate was rejected and was not sent to the sensor. No core `rclcpp` change was made.

The bounded alternative uses the installed stock `component_container --executor-type events-cbg` and loads the unchanged B `ouster_ros::OusterDriver` component. Three direct local smoke trials passed five lifecycle GetState requests each; two stopped with the component loaded and one unloaded it before stopping, all with exit code zero. A separate launch overlay preserves the driver parameters, name and namespace and coordinates lifecycle configuration/activation. On the real sensor, both C trials delivered approximately 10.005 Hz with the unchanged full cloud schema and zero header gaps over 150 ms. Wrapper state agreed on active/NORMAL/RUNNING. A subsequent standby/reactivation check passed: the lifecycle drain finished in 0.98 s, the driver stopped cleanly in 2.28 s, the whole standby request took 9.15 s including sensor parking, and activation took 39.59 s including sensor startup. No component process survived standby; reactivation created a new one with the same name and parameters. No SIGTERM/SIGKILL escalation or executor exception occurred in this hardware shutdown.

An eight-second post-reactivation subscription check received clouds, LaserScan, all four baseline grayscale image topics, IMU, lidar/IMU raw packets and telemetry. The RGB topic was advertised but inactive in both baseline and C, consistent with this sensor/profile. The first baseline smoke assertion incorrectly required every advertised topic to publish; its saved counts established that exception, and the final comparison requires C to match the baseline's active topic set. This is a publication/lifecycle smoke, not a raw-packet loss or full reconnect-fault test. C remains an experiment and is not selected by the final wrapper pin.

 A noninteractive process-group SIGINT can be forwarded twice by launch; this produced container exit `-2` with launch exit zero in both EventsCBG and SingleThreaded stock-container controls, without the standalone abort. Single-signal and lifecycle shutdown need to remain distinct checks.

## Optional organized crop and further work

The earlier sensor azimuth experiment reduced incoming work but retained every organized cloud slot, including NaN XYZ outside the window, and the full 12 MiB payload. An explicit organized-column crop could retain rows while publishing only selected columns using the existing standard **`sensor_msgs/msg/PointCloud2`**; no different standard message is required. A 128×1024 crop with the same 48-byte points would occupy 6 MiB rather than 12 MiB and remain organized. The message requires `data.size == row_step * height`, so retaining the full dimensions with a shorter array is not valid. See the [standard message definition](https://github.com/ros2/common_interfaces/blob/rolling/sensor_msgs/msg/PointCloud2.msg).

The crop must select the same global columns for every row, account for destaggered edge holes and wrapping windows, and retain NaN padding where necessary. Per-row compaction would break the organized correspondence. Consumers needing sensor-column identity require crop-origin/full-width metadata or a column field; XYZ-only consumers may not. Calculations must still use the original scan width when mapping sensor columns. Publishing an opt-in cropped topic would keep existing consumers compatible. It would change `width`, `row_step`, data length and the mapping from output columns to sensor azimuth, so it must be an opt-in consumer contract with wraparound, destagger, timestamp and missing-column tests. It is not implemented here. The existing `organized: false` option omits invalid points but loses organized row/column indexing; it is a different output contract.

The next narrow loop candidate is the per-pixel destagger modulo in `ImageProcessor` (`image_processor.h`, approximately lines 189–218). First confirm that division survives in the actual ARM64 image, then apply the same per-row normalization principle with image-equivalence tests. Auto-exposure sample selection may also benefit from reusing scratch storage or selecting sample values directly rather than repeatedly resolving indices, but exposure state evolves across frames; any change needs a deterministic multi-frame equivalence test. Neither change is included in B or D. Larger opportunities remain configurable image work, telemetry publication cadence and direct PointCloud2 construction, each with explicit output/timing compatibility gates.

## Evidence and limits

Local evidence is retained under ignored `var/` directories in this checkout:

- `var/cpu-backport-2026-11-13/`: A–B–B–A runner, input configuration and per-run measurements; `results/comparison.json` contains all completed comparisons and common schema/settings assertions; `results/summary.json` retains the initial A/B means.
- `var/backport-build-2026-11-13/B/` and `D/`: build logs, exact source manifests, compiler identities, runtime validation, test XML and replay results. `D/assembly-summary.json` and adjacent disassembly captures support the division-frequency claim.
- `var/backport-build-2026-11-13/C-rejected-31b4ab4/` and `C-stock-component-launch/`: separate executor smoke and image evidence.

After all trials, the test service, subscriber and router containers were removed. The sensor was verified in STANDBY with its complete original active configuration restored exactly; only the preexisting registry container remained. Final state/configuration and container-list evidence is retained with the results. Images and profiling artifacts remain available for inspection.

These are short isolated tests with one OS0 sensor, one firmware and one live lidar mode. They do not cover a full vehicle workload, thermal steady state, multiple subscribers, simultaneous raw recording or all packet profiles. No allocation or end-to-end latency bound was established. The deterministic cloud fixtures provide broader layout coverage than the live run, within the original-point scope described above.
