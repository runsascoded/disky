# Stage canaries: a candidate image climbs a size ladder before it takes production work

## Why

4f154316 (anchors prior probes via pyarrow) passed CI and went straight into cw's catch-up. There it ran latency-bound over gcsfuse (0.5 of 16 cores) and ran out of DuckDB memory 56 min into a real run (10-22T1802).

CI's fixtures are kilobytes, so they prove the bytes, not the cost. A heavy stage's change needs a step-up of real inputs, each about 4× the last, compared against the image it replaces, before it reaches the live store.

## Scope

The heavy stages: static `anchors` (`static_anchors run`), `drill` (`static_drill build`), merges/compaction (`static_merge`), the interval-store fold, and `import`. A commit touching one names it in its message; the canary runs for those stages.

## The ladder

Real inputs already processed, re-run into scratch, never the live store:

1. CI (as today).
2. **Small:** a recent run of ~5–10M rows (cw: 10-22T1202, 6.6M).
3. **Medium:** ~30M (cw: 10-22T0602, 31.8M).
4. **Heaviest recent:** cw 10-22T0001, 125M. gcs's equivalents follow from its manifests.
5. Production.

Each step runs only once the one before it passes. cw's data is ~0.28× gcs's, so cw's top step is a cheap rung below gcs's.

**Inputs come from the newest manifest.** Re-running a run over the tiers its own manifest listed fails once a carry has folded those tiers and prune has deleted them. cw's 10-22 ladder was un-runnable by 10-24: `manifests/2026-10-22T1202.json`'s L3/L2/L1 runs were gone. So each rung is a level-0 run the newest manifest lists, re-run over the tiers that manifest lists before it (`-P` = the newest manifest). Those tiers hold the same scans, and they live as long as that manifest does. The rungs are picked by rows (~4× apart) when the ladder runs, so a carry refreshes the ladder instead of breaking it. The comparison is baseline vs candidate on that same input, both re-run, never against the published files, which were built over the unmerged tiers. When the newest manifest has no level-0 run of a rung's size (right after a carry), that rung uses the nearest it has. A ladder that needs a rung at all costs copies the tiers into scratch first. That's deferred until one does.

**Published bytes as the baseline.** When a rung's own manifest still lives, so its tiers are the ones its published files were built over, the published files are an a2a baseline (`prior` in `meta.json` aside). This is also the **bootstrap**: no image before 2a573d6b (cw) has `run -P -o`, so the first candidate with it can only be gated against published bytes, pinned to such a manifest. From then on, the baseline image re-runs too.

**A rung's input pruned mid-run** (a publish's prune deleting the folded runs its manifest listed) surfaces as gcsfuse `ESTALE` ("Stale file handle") or a missing tier, not as a clean refusal. cw's 10-23T0001 canary died this way at 37 GiB. The canary classifies those as "input pruned: take the next rung on the newest manifest", never as a candidate failure. A rung pinned to the newest manifest (its tiers not yet folded) avoids it.

## At each step

The baseline image (the deployment's current tag) and the candidate run on the same input, side by side. Each records:
- wall seconds;
- CPU seconds (the process tree: `heartbeat.tree_cpu_seconds`);
- peak RSS (tree);
- work-disk and DuckDB temp peaks;
- the output's per-file md5.

The step passes when:
- **outputs:** byte-identical (every one of these stages is deterministic). A change that alters bytes on purpose (a format or bound change) names the expected changes (files or kinds), and the gate checks that the rest are byte-identical;
- **cost:** the candidate's wall and peak RSS are within a margin of the baseline (default +25%; a run expected to change cost names its expectation instead);
- **health:** no OOM.

Rules for running the steps:
- **Race, don't wait.** The candidate is killed, failing the step, as soon as its elapsed time passes the baseline's wall × (1 + margin) or its RSS passes the margin. The jobs' `[alive]` lines give both every minute. A doomed run isn't finished.
- **Machines:** canaries run on STANDARD machines, not spot. A preemption would otherwise read as a failure. If one happens anyway, the step reruns; it doesn't fail.
- **Cached baselines:** a baseline result is keyed by (stage, input, baseline digest) and reused until the production tag moves, so each candidate step runs one job, not two. The deployments' known timings seed that cache.

A disagreement between fleets (faster on cw, slower on gcs) means stop and discuss. Tuning constants (batch rows/keys, threads, chunk sizes) derive from the machine and the data's measured size, not per fleet. A fleet difference is a signal, not a reason to fork the algorithm.

## Mechanics

Per stage, a read-only re-run of one past input:
- **Drill:** `static_drill build -n -o <local>` already builds locally and uploads nothing.
- **Compaction:** `static_merge compact-probe` (local fold, per-file md5) and `measure-probe` (the measurement alone, `-H/-s/-S`).
- **Anchors (first change):** `static_anchors run -d <id>` reads its prior tiers from the newest manifest and writes into the bucket. It needs:
  - **`-P <manifest>`:** pins the manifest that published the run, so its priors are that manifest's runs before it. Re-running 10-22T1202 today would otherwise see the later carries' merged runs.
  - **`-o <local>`:** builds locally and uploads nothing.
- **Interval-store fold, import:** a scratch `-o` / `--to`, as their probes do.

On top, `dt-cloud stage-canary -s <stage> -d <input> -i <candidate image> [-b <baseline image>] [-u <report URL>]`:
- submits both re-runs as Batch jobs on the deployment's machine for that stage, each image by digest;
- waits on both;
- writes the compare report: the metrics above, the md5 diff, and pass/fail per the gates.

The gate is on the tag move itself: `dt-cloud image promote -r <repo> -t <tag> -d <digest> --ladder <stage…>` runs the ladder for the stages the change names, waits until the deployment's scans are idle (`/api/scan-runs`), then moves the tag. Each step's report is linked from the promotion's log line. This replaces cw's untracked `tmp/promote-when-idle.sh`; gcs moves its tag the same way.

## cw (the first deployment on it)

- **Images:** `us-central1-docker.pkg.dev/oa-internal-450019/cw-s3-usage/scan`.
  - `:cw` is prod. `:cw-next` is the candidate, built from a committed SHA in a clean worktree (`TAG=cw-next bash job/build.sh`).
  - Promotion is a tag move, `gcloud artifacts docker tags add $REPO@$DIGEST $REPO:cw`.
  - The canary takes `-i <repo>@sha256:<cw-next>`; `-b` defaults to `:cw`'s current digest. Both are resolved once and pinned by digest, since `:cw` moves mid-catch-up.
- **Jobs:** built by the runner's own `job()` spec builder with the profile's machine for the stage (`job/static-names.json`: n2-highmem-16, 750 GB local SSD, spot, us-east1, SA `cw-s3-job`, the data and scratch gcsfuse mounts), not a hand-written spec.
- **Outputs:** `gs://oa-cw-s3-usage-scratch/static-names/canary/<stage>/<id>/<digest-12>/`.
- **Ladder:** gen `2026-10-15cw`, level-0 runs, each pinned to `manifests/<id>.json`:

  | step | run | rows | known |
  |---|---|---|---|
  | small | `2026-10-22T1202` | 6,640,177 | 955 s (0c6b33db), prows 582 s |
  | medium | `2026-10-22T0602` | 31,832,873 | 2,110 s (7e144e10) |
  | heavy | `2026-10-22T0001` | 125,276,218 | 14,724 s on cde919c8: the regression, not a baseline. The first canary sets one. |
  | memory | `2026-10-21T1801` | 79.9M | 4,410 s, peak RSS 91.6 GiB (905ef3e3). The memory stress case. |

- **Metrics:** the jobs already log them: `ANCHOR_RESOURCE` (cpu_s, elapsed_s, rss_peak, cgroup_peak), the `[alive]` lines' `/stage` used, and `ANCHOR_QUERY` wall per table. cw's `tmp/anchors-slow.sh` / `anchors-probes.sh` parsers move into the compare report.
- **Output digest:** the md5s of the re-run's `anchors/**`.

## Steps

1. `static_anchors run -P/-o` (pinned manifest, local output), with a test that a pinned re-run of a past run reproduces its published `anchors/` bytes.
2. A per-stage "re-run spec" (`stage → argv` for a given input), shared by the canary and the probes.
3. `dt-cloud stage-canary`: submit (STANDARD), race the baseline (cached), compare, report.
4. `dt-cloud image promote … --ladder`: the gated tag move (cw first, implemented by the cw session; then gcs).

## Open

- **gcs's ladder inputs:** run ids at ~4× steps.
- **Margin per stage:** the default +25%, or tighter where runs are steady.
- **Reports:** where they live (scratch, `canaries/<stage>/<candidate>/`), and how long they're kept.
