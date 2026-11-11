# Portable executor acceptance — 2026-11-09

Phase 1 establishes that the existing reader can run outside Workers while preserving its answers. It does not demonstrate a faster production serving architecture. All inputs here are tracked, small synthetic fixtures; there is no billion-path fleet measurement or deployment switch.

On the existing `mgu` EC2 dev node, Node v24.22.1 and pinned Miniflare `5.20261006.2-beta` / workerd (compatibility date `2026-10-13`) passed **143 complete semantic response comparisons**: 53 base cases, nine indexed-only refusals and 81 owner/class/name-slice cases. The matrix includes cold, warm and concurrency-two passes, roots and drills, maps and diffs, multiple scans, a literal golden total including a zero-byte object, partial cuts, unsupported/unavailable results, cancellation, malformed budgets and deadline/read/byte/output limits. Only telemetry is excluded from equality. Each capsule gets its own process/isolate.

Median kernel time through serialization, milliseconds; each phase uses the same answered cases. Cold is the first suite pass, so later cases can reuse earlier reads; warm is the second pass. These tiny samples are correctness instrumentation, not capacity forecasts.

| Corpus | Cases per phase | Phase | Node | workerd |
| --- | ---: | --- | ---: | ---: |
| Base | 11 | cold | 4.98 | 6 |
| Base | 11 | warm | 0.89 | 1 |
| Base | 11 | concurrent (2) | 1.48 | 1 |
| Owner/class/name slices | 27 | cold | 16.66 | 27 |
| Owner/class/name slices | 27 | warm | 10.79 | 8 |
| Owner/class/name slices | 27 | concurrent (2) | 17.51 | 8 |

Separate sampling profiles over five repetitions of answered cases report the following exclusive stack-time estimates. Sampling is deliberately separate from the latency passes. The workerd clock is coarse at these durations; differences of a few milliseconds are not evidence of an architectural advantage.

| Corpus/runtime | Reader + decode, ms | Aggregation, ms | Other, ms | Idle, ms |
| --- | ---: | ---: | ---: | ---: |
| Base / Node | 23.45 | 11.88 | 142.67 | 0 |
| Base / workerd | 58.93 | 45.72 | 86.92 | 64.85 |
| Slices / Node | 776.36 | 1,742.32 | 694.31 | 0 |
| Slices / workerd | 1,144.39 | 1,825.14 | 647.16 | 175.72 |

The sampled categories are elapsed estimates from V8 stacks, not exact CPU attribution. Node's separately measured process CPU was 206 ms user + 9 ms system for base and 4,091 ms user + 194 ms system for slices. The Node process RSS snapshots were 243 MB and 634 MB, including the harness and Miniflare controller; workerd isolate used-heap snapshots were 6.5 MB and 53 MB. These snapshots are not peak working sets or comparable per-request memory limits.

The full site regression run passed **4,345 tests** (312 files; one skipped file/two skipped tests), and the subsequently added budget/file-admission tests passed **2/2**. The real Chromium session-log UI/client tests passed **2/2**. Logs and raw benchmark/profile artifacts are under the dev node clone's `tmp/`; `scripts/remote-test --executor` reproduces the parity/profile run from a frozen install. The standalone CLI, build and type checks are recorded with the implementation spec.

Next acceptance is Phase 2: a configured dev transport with short-lived capabilities, explicit routing and pinned real-data shadow comparisons. Data placement, startup/working-set costs and complete-body fleet latency must be measured there before selecting a hosted executor. Production still uses its existing Worker adapter.
