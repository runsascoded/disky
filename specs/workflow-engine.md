# Scan pipelines as declared DAGs: steps, executors, orchestrator

Status: **decided: Nextflow** (2026-10-16), written from cw-s3. It covers every deployment's scan pipeline (gcs, cw-s3, m3, the r2 demo, hf). Ryan: Nextflow only; "no server" is the killer app, and nobody writes Batch bindings for fray or Dagster. A GCP Batch executor for DVX is specced separately, in that repo (`specs/gcp-batch-executor.md`). Phase 1 is in progress on cw ([§7](#7-phase-1-as-built-cw)).

## 1. Problem

Every deployment's pipeline is a bash script that runs its steps in a fixed order. Each script is one long job:
- gcs: `job/run.sh`, 635 lines
- cw: `job/cw-run.sh`, 365 lines
- m3: `infra/aws/ingest.sh`, 184 lines
- the r2 demo: `.github/workflows/daily-ingest.yml`

The real dependencies between steps are only in comments and the line order. `scan-run supervise` records phases, but it doesn't schedule anything. That causes four problems:

- **No resume.** When step 12 of 15 fails, the next run starts over at step 1, or someone resubmits a one-shot (`cw-publish-submit.sh`, `cw-interval-submit.sh`, `REPROC=1`) by hand.
- **No parallelism the line order doesn't allow.** Steps that don't depend on each other still wait in line (§2).
- **Fan-out is hand-built.** Steps that need more than the scan VM (static-names, interval-store ranges and merges, gcs's listing fan-out, sweeps) submit their own Batch jobs and poll them. They do this through `append_runner` (1,311 lines): stage lists, skip-if-output-exists, a lease, `BatchRunner` submit/poll and `Runner.concurrently`. That is a small workflow engine, but only those two stores use it, and it's invisible to the scan's own record.
- **Waiting is hidden inside steps.** `static-wait` is a phase that only waits on a background subshell. `/scans` can't show what it waits on, or why.

GCP Batch has no DAGs: a job is one task group, plus array tasks. Glue is AWS's ETL service and isn't relevant here.

## 2. Today's cw scan, measured (2026-10-16T0601)

Each row's offset is seconds from job start, taken from the scan's `PHASE` lines. The run took 7,124 s (1 h 59 m) on one n2-standard-8.

| step | start → end (s) | duration | needs |
|---|---|---|---|
| bucket-check | 3 → 5 | 2 s | — |
| list + import marin-us-east-02a | 7 → 2902 | 48 m | — |
| list + import the other 4 buckets | 2903 → 3264 | 6 m | — (run after 02a only because the loop is serial) |
| webdata, publish, layer-2 | 3265 → 3327 | 1 m | every import |
| index-write | 3329 → 3638 | 5 m | every import |
| static-names (Batch stages; VM idle) | 3643 → 5907 | 38 m | index-write |
| publish-r2 | 5909 → 5966 | 1 m | index-write (**not** static-names) |
| interval-store add | 5970 → 6727 | 13 m | publish-r2 (reads the `path` sort from R2) |
| index-sync (lists the scan) | 6728 → 6807 | 1 m | static-names, interval-store, publish-r2 |
| over-time, index-gc, warm-cache, probe, digest | 6808 → 7124 | 5 m | index-sync |

The DAG implied by the "needs" column overlaps publish-r2 and interval-store (14 m) with static-names (38 m). The scan VM is idle for those 38 minutes anyway, since static-names' stages run as separate Batch jobs. So the same work lists the scan **≈14 min sooner** at no extra cost. Listing the four small buckets alongside 02a would save about 6 more minutes, if CAIOS and the VM's procs allow it.

## 3. Requirements

1. **Steps are idempotent CLI commands with declared outputs** (`dt-cloud …`, `disk-tree …`). A step whose outputs exist is skipped, and a re-run produces the same bytes (most steps already behave this way; `append_runner`'s stages do). All logic lives in the steps. The orchestrator only orders, places and retries them, which is what keeps it swappable.
2. **Declared dependencies**: one DAG definition per deployment, readable as data, with fan-out over runtime values (the buckets, the pending scans of a catch-up `-c`).
3. **Resume**: re-running a failed scan restarts at the first missing output, not at the listing.
4. **Pluggable executors**, chosen per step and per deployment, with no deployment tied to one engine:
   - inline on the head VM (most steps);
   - GCP Batch (heavy stages: static drill/anchors, interval ranges, sweeps);
   - AWS Batch or the existing ECS path (m3);
   - a GHA runner (the r2 demo);
   - local (tests, laptop dry runs).
5. **No always-on server to run**, if avoidable: today a scan costs one Batch VM for ~2 h. A webserver, daemon and metadata DB per deployment adds cost and ops for 4 runs a day.
6. **Observability through what exists**: each step's begin, end and failure go to `scan_runs` (/scans), and alerts go through `disk-tree notify`. An engine's own UI is a bonus, not a substitute.
7. **Cost attribution**: every Batch job the DAG submits carries `cost_labels`, as `append_runner.job_spec` does today.
8. **Secrets**: injected per step from Secret Manager, as now; the redaction in `scan-run` keeps working.

## 4. Options

### A. Marin's executor: `StepSpec` + `StepRunner` over `fray`

This is the "Marin-native" path (Marin `main` a7eeacc8, 2026-10-15). Marin dropped Ray (#5138).

- **`StepSpec`** (`lib/marin/src/marin/execution/step_spec.py`): `name`, `deps`, `hash_attrs` → `hash_id` → output path, plus `fn(output_path)` and optional `resources: ResourceConfig`. This matches requirement 1 almost exactly.
- **`StepRunner.run`** launches each step as soon as its deps finish, up to `max_concurrent` (default 8). It skips a step whose output has `STATUS_SUCCESS`. A status file with an atomic LOCK, lease and heartbeat keeps two runners off one step. That gives resume (requirement 3) for free.
- **`fray`** (`lib/fray`, package `marin-fray`, deps: cloudpickle, humanfriendly, marin-rigging) has a `Client` protocol (`submit(JobRequest) → JobHandle`: `wait`, `status`, `logs`, `terminate`). It ships two backends: `LocalClient` and Iris. Iris is Marin's scheduler over CoreWeave k8s (cw-us-east-02a, cw-us-east-08a, cw-rno2a).

Blockers:
- `StepRunner` is in the **`marin`** package, whose dependencies include jax, transformers, levanter[serve], datasets, wandb and openai. It also imports `iris` and `marin.training`. We can't depend on it for a storage scanner. That leaves two routes:
  - Marin extracts `marin.execution` into a light package, which needs their buy-in;
  - we vendor or reimplement its ~550 lines against the same `StepSpec` shape.
- No fray backend for GCP Batch or AWS Batch exists. We'd write one; `append_runner.BatchRunner` is most of it already.
- Running cw's steps *on Iris* (next to the data, on CoreWeave) would put our jobs on Marin's capacity, which needs Marin's agreement. That's a separate question from adopting the API.

Fit: the best semantic match and ecosystem alignment. But until Marin splits the package, most of the code is still ours, with a borrowed API.

### B. Nextflow

- Steps are `process`es running shell commands in a container: our CLIs as they are.
- Executors are set per process and include `google-batch`, `awsbatch`, `k8s`, `local` and others. That covers requirement 4 off the shelf.
- `-resume` replays from a content-hashed work dir, which can live on GCS or S3.
- Channels give fan-out over runtime values.
- No server: the driver is one JVM process, which can be the scan VM's own job.

Costs:
- A Groovy DSL in a Python/bash shop.
- Resume is keyed on Nextflow's work-dir cache, not our output markers, so a step's real outputs (R2, D1) still need their own skip-if-done. Ours already have that.
- The JVM and Nextflow have to go into the `:cw` image, or into a head-job image.

Fit: the most proven off-the-shelf match for "DAG of containerized commands on Batch, resumable, serverless."

### C. Dagster

- Assets with partitions, where scan ids are a time partition: the cleanest model of "each scan's outputs", plus the best UI and lineage.
- It needs its daemon, webserver and metadata DB running (or Dagster+, which is paid) for schedules and the UI. That conflicts with requirement 5.
- There's no first-party GCP Batch executor. Steps would go out through `dagster-pipes` and a launcher we write.

Fit: a good model, but too heavy to operate at this scale.

### D. Others

- **Prefect**: a Python flow/task API with work pools. It needs a Prefect server or Prefect Cloud, and there's no Batch worker as far as I know (to verify). It has the same ops cost as C.
- **Airflow / Cloud Composer**: an always-on scheduler; overkill.
- **Cloud Workflows**: GCP-only YAML, so it can't drive m3 (AWS) or the r2 demo (GHA).
- **DVX** (`~/c/dvx`): a local-subprocess, level-barrier DAG over `.dvc` files with local outputs. It has no remote executors and no fan-out over runtime values. It's a reference design for the provenance records, not the engine.

## 5. Proposal

**Phase 0 (engine-agnostic, now): declare cw's DAG, and take the overlap.**
- Write the DAG table of §2 as data in `job/cw-dag.yml`: steps, needs, executor, outputs. Every later phase reads this.
- In `cw-run.sh`, run publish-r2 → interval-store in the background alongside static-names, then wait for both before `index-sync`. That lists each scan ~14 min sooner, and the VM is idle in that window anyway.
- This needs an image rebuild. The order rules of [search-box-no-refusals.md §3.6] are unchanged: a scan is listed only once it's searchable, on R2 and in the interval store.

**Phase 1: prototype B and A on cw, side by side, on scratch outputs.** Run one real scan's DAG under each, writing to a scratch prefix and a non-prod D1. Compare:
- the wall time to "listed";
- resume after an injected failure in interval-store;
- the Batch cost;
- the lines of glue we own;
- what /scans shows;
- how a catch-up (`-c` over N pending scans) is expressed.

For A, that means `StepSpec`-shaped steps, a fray `Client` for GCP Batch built from `BatchRunner`, and the ~550-line runner vendored. In parallel, we'd ask Marin whether they'd extract `marin.execution` into a light package.

**Phase 2: one engine, every deployment.** gcs, m3 and the r2 demo move once cw's DAG has run a week of 6-hour scans. `append_runner`'s stage machinery then becomes steps in the DAG, and only its store logic stays: manifests, carries, prune.

**My lean: B (Nextflow)**, unless Marin agrees to extract their executor. B is the only option that meets requirements 3–5 without us writing the executor or running a server. A's advantage is ecosystem alignment, and it only becomes real once `StepRunner` is importable without `marin`'s dependency tree.

## 6. Decision (2026-10-16)

Nextflow for every deployment; option A and the Marin questions are dropped. Phase 0's overlap shipped with Phase 1's step split (§7), not separately.

The questions as they were put:

1. Phase 0 (DAG table + the publish-r2 / interval-store overlap in `cw-run.sh`, then an image rebuild): go?
2. Phase 1: prototype both A and B, or only B?
3. Should someone ask Marin about extracting `marin.execution`, and separately about running cw's steps on Iris? Who asks?


## 7. Phase 1 as built (cw)

**What the probes settled** (Nextflow 26.10.2, nf-google 1.29.1, a 3-step pipeline on Batch in the scratch bucket):
- **No `local` steps with a `gs://` work dir.** The `local` executor refuses it ("requires the use of POSIX compatible file system") unless Fusion is used, and Fusion needs Seqera's Wave. So every step is its own Batch task, and the head runs only the driver. That also ends today's 38 idle minutes of an n2-standard-8 waiting on static-names: each step gets its own machine size.
- **Each task mounts the work dir's bucket at `/mnt/disks/<bucket>`, read-write, with `implicit-dirs`.** With the work dir in the data bucket, the steps' `$GCS/$DATA/…` paths work unchanged.
- **The scan image's ENTRYPOINT is the one-VM driver.** A task's `/bin/bash … .command.run` would be appended to it, so tasks set `containerOptions = '--entrypoint=/usr/bin/env'`.
- **Secrets:** nf-google has no Secret Manager support, and Nextflow's own `secret` store writes values into `.command.run` in the work bucket. So each step fetches its own (`dt-cloud secrets exec -s ENV=SECRET -- …`, as the VM's service account). Already-set variables win, so the one-VM job's `secretVariables` still apply.
- **Resume with no local state.** Nextflow's cache can live in GCS (`NXF_CLOUDCACHE_PATH`), but Nextflow finds a session only through its local `.nextflow/history`. So the head derives the session id from the scan id (`uuid5`), writes that one history line, and runs `-resume <id>`. Verified: a run failing at its last step, re-run from a fresh directory, ran only that step (`completed=1 cached=2`). A fully finished session re-ran in 13 s with every step cached.
- **Overhead:** each task's VM start plus image pull is about 1–2 min.

**Layout:**
- `job/cw-step.sh STEP` holds the step bodies. `job/cw-env.sh` is the env every step sources.
- There are two drivers:
  - `job/cw-run.sh`: one VM, today's cron. Its order is unchanged, except static-names now runs alongside publish-r2 → interval-store.
  - `job/nf/cw.nf` + `nextflow.config` + `cw-head.sh`.
- The DAG:
  - bucket-check, and scan-bucket ×N run in parallel; then index;
  - static-names ∥ (publish-r2 → interval-store);
  - index-sync;
  - post: over-time, index-gc, warm-cache, probe, digest.
- Steps hand off through the data bucket. Nextflow channels carry only `done` markers.
- Each task records into the head's run: `DT_RUN_ID`, which `scan-runs.json` `run_id_env` reads before the task's own `BATCH_JOB_UID`.

**Next:**
1. A scratch run of the whole DAG: the data bucket set to the scratch bucket, the four small buckets, and serving steps without credentials (they skip). Check the timing, a resume after an injected failure, and the head on Batch.
2. Then the cutover: the cron submits the head job instead of the one-VM job, and the first scans run under it in prod, after a go.
3. Then gcs, m3 and the r2 demo.

[search-box-no-refusals.md §3.6]: search-box-no-refusals.md
