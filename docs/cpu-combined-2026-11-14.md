# Combined Ouster configuration comparison against the original driver

This study measures complete configurations against the original driver fetched at the beginning of the investigation, rather than adding percentages from separate experiments. It follows the [CPU/GPU study](cpu-next-2026-11-13.md); both earlier combined sequences remain excluded because camera workloads overlapped them.

## Compared configurations

| Configuration | ROS / SDK | Runtime image |
| --- | --- | --- |
| Original baseline | `40f57c34ef6a36d797d1cf6e3c0e7624313d435f` / `8aa86b94b1c75dfcb99159a4147b44d973e52323` | `sha256:62c4f6969ded1b88e26c61c48ec494ad3e185741fb84507f9b4e1a6f5479c1f2` |
| Optimized CPU defaults and CPU with 10 Hz telemetry | `f80d05c7a1255a365cfa2a36e218dd988a077b59` / `dce1ef0debdec33cb9e9611b08bc9ec60ee1f73e` | `sha256:96bde388958b4e2b752111af66651dfc1333061f633e67c0a22ec020f3f74e49` |
| Experimental CPU/GPU with 10 Hz telemetry and direct SHM serialization | `d739e7395f39dfde1fc013d46d201ada57d2e911` / `dce1ef0debdec33cb9e9611b08bc9ec60ee1f73e` | `sha256:38639ba36ee6661687b9c37ebe29775dd1a5a5de70aacc6f5fb4d315bac03fd2` |

All images were built on the Mac and transferred to the Orin during the preceding study. This comparison reuses those exact verified images; it does not rebuild unchanged source. The original wrapper revision is `c36c75088479b6dc0e0d492bae34c964fe336a04`. The experimental middleware library is SHA-256 `30f1ea71d8349d84177571dd221b417aad13b1457166d8b585409384ca0231f0`.

Every configuration uses Zenoh shared-memory transport. The experimental middleware change additionally serializes CPU-group messages directly into the transport allocation. It is not a TCP-versus-SHM comparison or end-to-end zero copy. Moving from the production CPU image to the experimental CPU/SHM image compares complete options, since the latter also includes the disabled GPU hook. CPU/SHM versus GPU/SHM uses the same image and changes only GPU activation.

## Measurement conditions

The real OS-0-128 at `10.160.1.51` is accessed through the AGX Orin at `10.160.1.21`. Sensor settings remain full 360°, LEGACY, 2048×10, internal-oscillator timestamps. All `IMU|PCL|SCAN|IMG|RAW|TLM` processors remain enabled. Clouds remain original 48-byte points, organized and destaggered, 128×2048, with 12,582,912 data bytes. The four full-width mono16 images are subscribed throughout each measured interval. This does not measure savings from unsubscribing images or reducing azimuth.

The subscriber, native telemetry counter, router, namespace, ROS domain, NVIDIA runtime/device exposure and 256 MiB SHM pool are identical. Original and optimized-default telemetry remain per-packet; the other options explicitly select a 10 Hz limit for unchanged status. Status/countdown changes still publish immediately. Candidate-only crop/telemetry parameters are omitted from the original driver, and common effective configuration is checked independently.

Run order is original → CPU defaults → CPU/10 Hz telemetry → CPU/10 Hz/direct SHM → GPU/10 Hz/direct SHM, then the reverse order. Each run samples CPU for approximately 60 seconds after 15 seconds of observer settling, with approximately 90 seconds of cloud/image observation. GPU cases first match eight complete live clouds against the same-scan CPU output; shadow work finishes before timing. **100% CPU means one occupied core.** Service CPU includes the driver and supervisor; observer and router CPU are measured separately.

MAXN and schedutil remain unchanged. The workload guard begins a continuous, all-types Docker event stream before each trial and records container/process CPU, frequencies and available thermal readings. It rejects external container activity and uses predefined host CPU exclusion rules during the actual CPU interval: a 25% sampled peak or 5% overlap-weighted average from external userspace processes. Those rules were calibrated with sensor-free probes before performance collection; ordinary infrastructure/kernel work is retained separately. They are practical exclusion rules, not proof that every transient host activity is absent. A rejected trial stops the remaining sequence.

Sensor alerts retain the corrected snapshot/cursor interpretation from the preceding study. Lidar packet timestamps are not used to locate alert events inside CPU windows. Output cadence, matching schemas and zero host UDP error counters do not prove complete sensor packet delivery.

## Results and recommendation

All ten trials pass the operational and isolation audit. The table uses the arithmetic mean of two runs per configuration. Relative reductions use the two original-baseline runs, not a sum of earlier stage improvements.

| Complete configuration | Driver CPU | Service CPU | Driver reduction vs original | Service reduction vs original |
| --- | ---: | ---: | ---: | ---: |
| Original baseline | 57.46% | 70.60% | — | — |
| Optimized CPU, per-packet telemetry | 51.70% | 64.80% | 10.02% | 8.22% |
| Optimized CPU, 10 Hz telemetry | **30.32%** | **32.27%** | **47.24%** | **54.30%** |
| CPU, 10 Hz telemetry, direct SHM serialization | 28.34% | 30.25% | 50.68% | 57.16% |
| GPU, 10 Hz telemetry, direct SHM serialization | **23.44%** | **25.37%** | **59.21%** | **64.07%** |

Individual driver readings, in first/second occurrence order, are 57.144/57.780 for the original, 51.863/51.542 for CPU defaults, 30.756/29.878 for CPU with 10 Hz telemetry, 28.406/28.273 for CPU/direct SHM, and 23.571/23.308 for GPU/direct SHM. Every candidate reading is below both original readings. The original baseline's driver spread is 0.64 percentage points, about 1.11% of its mean. These are two short measurements per option under adaptive clocks, not population confidence intervals or a fleet qualification.

**Recommended practical configuration: the optimized production CPU image with `telemetry_max_rate_hz: 10.0`.** It reduces measured service CPU by 54.30% while retaining full-width clouds, all four images, the existing processor mask and unchanged sensor settings. It uses the CPU production source pins and ordinary fleet middleware. Keep image demand gating enabled at its default; this comparison requested every image, so the result does not depend on skipping unused images.

```yaml
driver_params:
  telemetry_max_rate_hz: 10.0
```

This setting changes the telemetry recording contract: unchanged statuses are coalesced to approximately 10 Hz, while status/countdown changes publish immediately. Keep the default `0.0` if a consumer requires per-packet telemetry; the optimized CPU-default row then gives the measured result. The independent HTTP temperature topic is unchanged.

**Lowest measured service CPU: GPU plus direct SHM serialization and 10 Hz telemetry.** It saves a further 6.90 percentage points of one core in service CPU compared with the recommended CPU/10 Hz option. Both GPU runs pass eight exact same-scan CPU comparisons before timing and clean shutdown afterward. This remains an experimental deployment: the backend has narrow LEGACY/original/organized layout support, earlier standalone trials observed 134–145 ms idle-resume calls, and the middleware change is outside the production fleet pin. These CPU measurements do not establish deadline guarantees, energy savings or performance under a simultaneous camera/GPU workload. The CPU/direct-SHM option also remains experimental; its additional service saving over the production CPU/10 Hz option is 2.02 points of one core.

### Output and measurement limits

All ten runs observe clouds at approximately 10.004–10.007 Hz and all four mono16 images at approximately 10 Hz. Schemas are unchanged; no observed header timestamp gap exceeds 150 ms. Whole-observer callback arrival gaps reach approximately 366–406 ms across configurations. Arrival history was not retained with interval markers, so those maxima cannot be assigned to startup or the CPU window and do not support a bounded-delivery-latency claim.

Host UDP error counters do not increase during the measured intervals. Sensor alert snapshots include newly observed destination-unreachable and LEGACY-notice records, whose event timing cannot be located using lidar packet timestamps. This is not a packet-loss-free qualification.

Instrumentation adds material load outside service CPU. The guard consumes 9.30–10.09% of one core, measurement helpers 3.10–3.48%, and separately recorded Docker/containerd infrastructure 21.96–23.21%. The observer averages 29.05–29.58% for per-packet telemetry configurations and 9.45–9.95% for limited telemetry configurations; this workload-dependent subscriber cost can affect scheduling and adaptive clocks. Router CPU stays below 0.004%. The harness and subscriber graph are fixed across the matrix; no overhead is subtracted to estimate unobserved production CPU. Per-run all-core mean frequency is 0.761–0.797 GHz, and sampled CPU temperature is 43.81–45.72°C. Frequency is not a thread-weighted normalization.

Kernel-process CPU is also recorded separately: 0.65–0.84% of one core in CPU cases and 2.52–2.74% in GPU cases. Thus the GPU service-CPU reduction does not directly describe whole-host CPU savings. These sampled process classes are not a whole-machine CPU or energy accounting.

## Final state

The complete sensor configuration matches the saved original, including full azimuth and `STANDBY`. The study driver and router stopped with exit code 0, without OOM kills, and were removed. Only the preexisting registry remains. MAXN and schedutil were preserved. Cleanup and sensor-state records are retained with the raw study evidence.

## Evidence

New raw evidence and executable harness are retained under `var/combined-profile-2026-11-14/` on the Mac and `/home/uxv/ouster-combined-profile-20261114/` on the Orin. Historical studies are unchanged. Each run records exact image/source identity, generated parameters, sensor configuration, output schemas/rates, GPU lifecycle checks, CPU/perf counters, and independently bounded isolation evidence. Credentials are not stored in the report or harness.

- [Independent matrix audit](../var/combined-profile-2026-11-14/results/independent-full-matrix-audit.json): 1,551 control checks and 18,039 isolation subchecks pass; 258 input artifacts are hashed.
- [Secondary numerical review](../var/combined-profile-2026-11-14/results/secondary-numeric-review.json): 545 checks pass, including reconstruction from per-second CPU records and input-hash verification.
- [Frozen harness manifest](../var/combined-profile-2026-11-14/harness-manifest.json) and [detailed per-option summary](../var/combined-profile-2026-11-14/results/independent-full-matrix-summary.md).
- [Cleanup audit](../var/combined-profile-2026-11-14/results/independent-cleanup-audit.json): all 12 restoration and shutdown checks pass.

Raw `var/` artifacts are local study evidence and are not included in the Git checkout.
