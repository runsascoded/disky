# VM pilot: r2 + hf served and scanned from one small always-on VM

Status: draft (2026-11-26, the cw session, from Ryan's ask: "get this working for r2+hf, on one small/cheap VM, where we'd nonetheless build out all the mechanisms"). For the root session to own (`cloud`, r2, hf). Builds on [vm-mode.md] (`vmd`, packs, tenants, the cell rule, parity: built and measured), [serverless-tiers.md] (the Worker ↔ VM hand-off, §5) and [full-scan-serving-bench.md]. Nothing here is approved for infra yet: the VM, its tunnel, secrets and Pulumi are each Ryan's go.

## Why

Serving the full filter cube (start/end, dir, query, user, class, ledger status) exactly needs every version row in memory: precomputed indexes cover a slice (the static name index answers rare terms, refuses common ones and any query combined with a user or class scope) and are the costliest part of each scan. GCP compute, last 14 days scaled to a month (billing export, 2026-11-26): static-names (append, catalog, drill, anchors, folds) cw ~$700, gcs ~$400; scans cw ~$290, gcs ~$400; gcs's interval store ~$270; r2 ~$36, hf ~$13. `vmd` answers every axis in 0.1–2 s from a memory-mapped pack (gcs: 32.4 GB).

The pilot proves the shape on the two smallest deployments, where nothing has to move off GCP: one always-on VM that **serves** every view from `vmd` and **runs** the deployments' scans in its idle CPU, under a fixed memory budget for scan work, with no per-scan indexing beyond the interval store and the pack.

## The box

- One VM, `deployment=shared`, `component=vm-pilot`: an e2-highmem-2 (16 GB) or e2-standard-4 (16 GB, 4 vCPU), ~$65–100/month; a 50 GB pd-balanced disk for packs, the work dir and the job runner's state.
- Memory budget (systemd slices; cgroup v2):
  - `serve.slice`: `vmd` and its packs (r2 + hf, each < 1 GB), `MemoryMin` = packs + headroom, `CPUWeight=10000`.
  - `scan.slice`: every scan job, `MemoryMax` = the scan budget (e.g. 8 GB), `CPUWeight=100`, `IOWeight=100`. A request never waits on a scan for CPU; a scan that needs more memory spills to disk, it never grows the slice.
- Packs mlocked (or `MemoryMin`-protected) so scan page cache never evicts them.
- The slices are tracked unit files, [`deploy/vm-pilot/serve.slice`] and [`deploy/vm-pilot/scan.slice`] (copied to `/etc/systemd/system/`). Their values are placeholders until the box is sized:

  | slice | key | placeholder | sized as |
  |---|---|---|---|
  | `serve.slice` | `MemoryMin` | 3G | r2 + hf packs (each < 1 GB) + `vmd`'s headroom |
  | `serve.slice` | `CPUWeight` / `IOWeight` | 10000 / 1000 | fixed: serving always wins |
  | `scan.slice` | `MemoryMax` | 8G | the scan budget: box memory − `serve.slice`'s `MemoryMin` − the OS's |
  | `scan.slice` | `MemorySwapMax` | 0 | fixed: past the budget is an OOM kill in the slice, never a slow box |
  | `scan.slice` | `CPUWeight` / `IOWeight` | 100 / 100 | fixed |

## Mechanisms (each one cw and gcs need later)

1. **A job backend interface.** Today `batch.submit_job` is the one GCP seam for most jobs; the static-names runners, Nextflow's executor and the site's REST submits bypass it. Define `JobBackend` (`submit(spec) → id`, `status(id)`, `logs(id)`, `cancel(id)`) with three implementations: GCP Batch (today's), `local` (a process in `scan.slice`, state on disk, logs to Cloud Logging or the store), Kubernetes Job (later, cw's CoreWeave nodes). The scan profile names its backend; every step's job goes through it.
2. **Bounded scan steps.** Each step the pilot runs takes its memory from the slice budget: listing (already streaming), `import -e stream` / `path-index` (DuckDB `-M`), the interval-store append (`-M`, `-B` sub-ranges). Measure each at the budget on r2 and hf; fix any that doesn't fit by spilling, not by more memory.
3. **Incremental pack updates.** Today `vm-pack build` rewrites the whole pack, loading every version row (117 GB peak on gcs). Instead the interval-store append's output (the scan's opened versions and close records) is folded into the tenant's pack: a delta file `vmd` maps beside the base pack, compacted into a new base on the interval store's own compaction. Memory then scales with a scan's changes, not with history.
4. **Serving wiring, beside today's** ([serverless-tiers.md] §5, [vm-mode.md] §7): `vmAdapter.ts` called by `/api/subtree`, `/api/diff`, `/api/series`, search; the Worker reaches `vmd` through a Cloudflare Tunnel (HMAC per tenant, already built); `vmd`'s `/v1/status` copied to the store so the Worker knows the box's state. Both backends serve prod side by side (Ryan, 2026-11-27): each session is pinned to one, `idx` (today's indexes) or `vm` (`vmd`), and a URL parameter (`?be=vm` / `?be=idx`) overrides and re-pins it. The pin is a cookie the middleware reads; responses say which backend answered (`Server-Timing` and a small badge), and the pin passes through links the page builds. The default pin stays `idx` while VM mode bakes; an admin setting moves a share of new sessions to `vm`. When the box is down or loading, a `vm` session is answered by `idx` and says so (no silent partial answer).
5. **Retiring per-scan indexing is out of the pilot** (Ryan, 2026-11-27): prod keeps indexing and serving as today while VM mode bakes beside it. Turning off static-names and the served sorts for a tenant (the site's `FILTER_STATIC*` flags replaced by `vmd`) comes only after a long bake, as its own call; then the last built generation stays frozen, dated, as the `idx` fallback.
6. **Cost per step on /scans**, from the same budget: the box's cost is fixed, so a step's share is its CPU-seconds × the box's $/CPU-s; Batch steps keep their billed cost (see the cw session's per-step cost proposal).

## JobBackend (step 1)

Built: mechanism 1 and the slices (code only; no VM, Pulumi, tunnel or secrets). [`cloud/src/dt_cloud/job_backend.py`], tests in `cloud/tests/test_job_backend.py`.

### The interface

A spec stays a Batch job's JSON (what every submitter builds today), so each backend takes the same dict:
- `submit(spec, job_id, *, buckets=(), cross_region=None, exists_ok=False, retry_for=0) -> id`: `exists_ok` makes a resubmit of an existing id count as submitted (Batch's 409); without it, an existing id is an error on both backends. `buckets` / `cross_region` / `retry_for` are `batch.submit_job`'s; `Local` ignores them.
- `status(id) -> JobStatus(state, detail)`: `state` in Batch's vocabulary (`QUEUED`, `SCHEDULED`, `RUNNING`, `SUCCEEDED`, `FAILED`, + `CANCELLED`); `detail` a backend's note (Batch: task counts, which `BatchRunner` logged before; local: an exit code, or why a job was lost).
- `logs(id, limit=40) -> list[str]`: the last `limit` output lines, oldest first.
- `cancel(id)`.
- `what`: how messages name a job (`Batch job` keeps every existing message byte-identical; `local job`).

### `GcpBatch` (today's)

`batch.submit_job` (still the one place a Batch job is created: cost labels, region check, retries), `gcp.batch_job`, Cloud Logging's task logs (`gcp.log_entries` over `task_log_filter`), and a cancel by delete: `batch.cancel_job`, split out of `batch.cancel_labelled`, which now calls it. No behaviour change for any caller, with one alignment: `BatchRunner` now creates its job in `Profile.project` (it already polled there; that defaults to the credentials' project, as the submit did).

### `Local` (this host)

- **Translation** (`container_runs`): each task (`taskCount`, run in turn, not `parallelism` at once) × each runnable becomes `<engine> run --rm --name disky-<job>-<task>-<runnable> --cgroup-parent=<slice> -e K=V… -v HOST:CTR… [--entrypoint E] IMAGE COMMANDS…`, with `$BATCH_TASK_INDEX` / `$BATCH_TASK_COUNT` as Batch sets them. Env is the task's then the runnable's `environment.variables`, sorted. A disk task volume (`deviceName`: the local-SSD `stage`, a new disk) becomes a host dir (`$DISKY_LOCAL_WORK/<job>/<device>`, default `<root>/<job>/disks/<device>`), created before the first run and removed after the last (as Batch's VM teardown removes the SSD); a container volume whose host path is under a task volume's `mountPath` is mounted from that dir. `maxRunDuration` is enforced per task (the container is stopped, `FAILED`).
- **Refused** (`Unsupported`, before anything is written): gcsfuse and NFS volumes, a container volume outside every task volume, `secretVariables` / `encryptedVariables`, non-container runnables (script, barrier), `background` / `ignoreExitStatus` / `alwaysRun`, a container's registry credentials or raw `options`, more than one task group. A spec missing its cost labels is refused as on Batch (`cost_labels.check_spec`), since `deployment` is the tenant.
- **Not applied:** `computeResource` and `allocationPolicy` (the slice is the budget), `maxRetryCount` (no spot here), the spec's service account (a job runs with the host's credentials: on GCE, the VM's), `logsPolicy`.
- **State** under `$DISKY_LOCAL_JOBS/<job_id>/`: `spec.json`, `job.json` (tenant, submit order, the translated runs), `status.json`, `supervisor.json` (`{pid, boot, at}`), `log` (every run's output), `exit`, `cancel` (a cancel's marker). Every read is from disk, so `status` / `logs` / `cancel` work from any process.
- **Supervisor:** `submit` spawns `python -m dt_cloud.job_backend supervise ROOT ID`, by default as its own transient unit (`systemd-run --collect --unit=disky-job-<id> --slice=<slice>`, so it outlives a submitter that is itself a systemd unit; `$DISKY_LOCAL_SPAWN=direct` for a detached process on a dev box). It waits its turn, runs the job's containers in order, and records the end.
- **One job at a time per tenant** (the spec's `deployment` label): under a per-tenant `flock`, a job starts only when it is the tenant's oldest queued job and none of the tenant's is running; other tenants run beside it (all inside `scan.slice`'s budget).
- **Restarts:** a queued or running job whose supervisor is gone (its pid dead, a different boot id, or never started within `START_GRACE_S`) is `FAILED`, "supervisor lost", the next time anyone asks. A reboot doesn't resume jobs; the caller resubmits (a new id).
- **Cancel:** a queued job is `CANCELLED` at once; a running one's current container is stopped (`<engine> stop`) and its supervisor records `CANCELLED` and starts nothing more.

### Selection

`job_backend.resolve(region, project)`: `$DISKY_JOB_BACKEND` = `batch` (default) or `local`. Local reads `$DISKY_LOCAL_JOBS` (required), `$DISKY_LOCAL_WORK`, `$DISKY_LOCAL_ENGINE` (`docker` | `podman`, default `docker`), `$DISKY_LOCAL_SLICE` (default `scan.slice`), `$DISKY_LOCAL_SPAWN` (`systemd-run` | `direct`). A deployment sets them in its job env; the pilot box's would be `DISKY_JOB_BACKEND=local`.

### Callers

Through `JobBackend`:
- `append_runner.BatchRunner`, i.e. every `Runner.run_job`: the interval store's `run` (`interval_append`), the static-names runner (`static_runner`) and its merge helpers (`static_merge`), with the `StillRunning` / `wait` polling and transient-error retries unchanged. Its specs mount gcsfuse buckets and R2 secrets today, so under `local` they are refused (by design, until the two gaps below close).
- `vm_pack.submit` and `access_jobs.submit` (submit only, not waited on). `vm-pack`'s spec runs locally as is unless it names `secrets` (a non-GCS store's credentials).

Still on raw Batch (known gaps):
- `scan_tasks` (`scan-run task`): it clones its worker's spec from its own controller's Batch job (`$BATCH_JOB_UID`) and checks an existing worker against Batch's stored job; it only exists inside a Batch controller.
- Nextflow's `google-batch` executor (`scan_head`), the Fray backend (`scan_backend_fray`).
- `batch_events.submit` (a compaction submitted from Batch's own state-change events).
- The shell's `dt-cloud batch submit`, `job submit-listing` (the listing fan-out, `wait_jobs`), `job submit-reviewed`; `job status|logs|watch|metrics` and `batch-cancel` (`cancel_labelled`) read Batch only.
- The site's REST submits (`_lib/sweepDispatch.ts`, `_lib/cwBatch.ts`) and `BatchCron` bodies (Cloud Scheduler posts them).

### For review

- **Secrets.** `Local` refuses `secretVariables`. A clean seam exists: the supervisor could resolve each ref with `secrets.access` under the host's credentials and pass it as `-e NAME` (no value: the engine copies it from the supervisor's own environment), so a value never reaches argv, `job.json` or disk. That needs the box's service account to hold `secretAccessor` on those secrets, an infra decision, so it isn't built.
- **gcsfuse volumes.** Refused. The pilot's jobs that mount buckets (the interval-store and static-names stages) either read through the API instead, or the box mounts gcsfuse itself (`gcsfuse` on the host, a host-path volume); the second keeps specs unchanged but puts a FUSE daemon in `scan.slice`.
- **Logs.** A local job's output is its `log` file, not Cloud Logging, so `dt-cloud job logs` (Batch only) doesn't see it and nothing ships it off the box. Options: the store (copy `log` beside the scan's records at the end), or Docker's `gcplogs` driver (the spec's "logs to Cloud Logging"), which ties the box to GCP.
- **Identity.** A local job runs with the host's credentials, not the spec's `serviceAccount`; the box's account needs the union of the pilot's jobs' grants.
- **Restart semantics.** A lost job fails rather than resumes; the scan steps already resume from their outputs on a resubmit.

## Scheduling

- Scans as today (the r2 demo's daily ingest; hf's cron), submitted to the `local` backend. A scan arriving while one runs queues (one at a time per tenant; the box's CPU is shared).
- Inverted cost model (Ryan's): the box's price is the compute budget; scans run as often as its idle CPU allows. The pilot measures that rate for r2 + hf.

## Order

1. `JobBackend` + `local`, the slices (code: built, see "JobBackend (step 1)"), one r2 scan end to end on the box (`vmd` not yet serving): its wall time and peak memory per step at the budget.
2. `vmd` on the box with r2 + hf packs (full rewrite per scan, as today); the Worker wiring on r2's dev stack; parity on live requests.
3. Incremental pack updates.
4. r2 prod serves from the box for sessions pinned `vm` (`?be=vm`), beside `idx`; then hf. Bake; compare answers and latency per request kind on live traffic. Retiring indexing is a later call, not this pilot's.
5. Write up what cw (CoreWeave CPU nodes, Kubernetes backend) and gcs (memory: 32 GB pack) need beyond this.

## Open questions

- Does r2's public map tolerate the box's downtime (a VM restart, a pack swap)? The fallback is today's Worker path while it exists; after static-names is retired for r2, search has no fallback.
- hf's listing is rate-limited by the Hub API (429s at 8 in flight), not CPU: it fits the box's idle cycles, but a scan can take hours.
- Where the box runs: GCP (next to r2's and hf's Batch today) or CoreWeave (cw's free CPU nodes, next to the cw tenant later). GCP for the pilot; the backend interface keeps the choice open.
- Beyond the pilot, a shared box's size. cw's pack, measured 2026-11-26 (`cw-vm-pack-measure-1126-1618`, `vm-pack/2026-10-14cw.c003h.c003/2026-11-26T1203.fsb`): 13.74 GB for 348 scans, 391.6M rows (343.5M file versions, 48.1M dirs), 100.4M names, 16.8 GB resident (42.9 B/row); built in 6 m 50 s at a 41.2 GiB RSS peak on an n2-highmem-16. That's ~3× vm-mode.md's 11-14 estimate (`gov_reports`). With gcs's 32.4 GB (11-14, larger now), cw + gcs + the small tenants come to ~50 GB of packs: comfortable on an n2-highmem-16 (128 GB), tight on an 8.

[vm-mode.md]: vm-mode.md
[serverless-tiers.md]: serverless-tiers.md
[full-scan-serving-bench.md]: full-scan-serving-bench.md
[`deploy/vm-pilot/serve.slice`]: ../deploy/vm-pilot/serve.slice
[`deploy/vm-pilot/scan.slice`]: ../deploy/vm-pilot/scan.slice
[`cloud/src/dt_cloud/job_backend.py`]: ../cloud/src/dt_cloud/job_backend.py
