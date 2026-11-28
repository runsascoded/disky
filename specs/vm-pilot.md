# VM pilot: r2 + hf served and scanned from one small always-on VM

Status: draft (2026-11-26, the cw session, from Ryan's ask: "get this working for r2+hf, on one small/cheap VM, where we'd nonetheless build out all the mechanisms"). For the root session to own (`cloud`, r2, hf). Builds on [vm-mode.md] (`vmd`, packs, tenants, the cell rule, parity: built and measured), [serverless-tiers.md] (the Worker ↔ VM hand-off, §5) and [full-scan-serving-bench.md]. Ryan's go on the box's infra (2026-11-27, high priority): one e2-standard-4 + a 50 GB pd-balanced disk in `oa-internal-450019`, labelled `app=disky`, `deployment=shared`, `component=vm-pilot`; a Cloudflare Tunnel; per-tenant HMAC secrets for `vmd`; a service account for the box. Per-deployment isolation (Ryan, 2026-11-27): the box's account holds nothing of a tenant's but `iam.serviceAccountTokenCreator` on its job account; a job runs as its spec's account, as on Batch. VM mode bakes beside prod (each session pinned `idx` or `vm`). The IaC and the box's bootstrap are built, not applied: see "Runbook".

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

- **Translation** (`container_runs`): each task (`taskCount`, run in turn, not `parallelism` at once) × each runnable becomes `<engine> run --rm --name disky-<job>-<task>-<runnable> --cgroup-parent=<slice> --network disky-<job> -e K=V… -v HOST:CTR… [--entrypoint E] IMAGE COMMANDS…`, with `$BATCH_TASK_INDEX` / `$BATCH_TASK_COUNT` as Batch sets them. Env is the task's then the runnable's `environment.variables`, sorted; their `secretVariables`, the job's metadata server's address (`$GCE_METADATA_HOST`, `$GCE_METADATA_IP`, `$GCE_METADATA_ROOT`: below, "Identity"; a spec's own values for these are dropped) and, unless set, `$GOOGLE_CLOUD_PROJECT` (the account's project) join them, a secret or the address as `-e NAME` alone. Task volumes:
  - a new disk (`deviceName`: the local-SSD `stage`, a `newDisk`) becomes a host dir (`$DISKY_LOCAL_WORK/<job>/<device>`, default `<root>/<job>/disks/<device>`), created before the first run and removed after the last (as Batch's VM teardown removes the SSD), never while anything is mounted under it;
  - an existing disk (the device's `allocationPolicy.instances[].policy.disks[].existingDisk`) becomes `$DISKY_LOCAL_WORK/_disks/<disk name>` (default under `<root>`), created when missing and never removed: it outlives the job, as the disk outlives a Batch job's VM;
  - a gcsfuse volume (`gcs.remotePath` `bucket[/dir]`; its `mountOptions` as gcsfuse argv, `-implicit-dirs` given its second dash) is mounted on the host at `<root>/<job>/gcs/<i>` for the job's runs, by `gcsfuse --foreground` as the supervisor's child (so in its unit, in the slice: the FUSE daemon's memory is the scan budget's), as the job's account through the job's metadata server; unmounted after the last run (`umount`, then `umount -l`); a mount point that won't go is left, said, and never removed under.

  A container volume whose host path is under a task volume's `mountPath` (the innermost) is mounted from that dir. `maxRunDuration` is enforced per task (the container is stopped, `FAILED`).
- **Refused** (`Unsupported`, before anything is written): a spec without `allocationPolicy.serviceAccount.email`, NFS volumes, a container volume outside every task volume, `encryptedVariables` (and a `secretVariables` ref that isn't a `projects/…/secrets/…/versions/…` name), non-container runnables (script, barrier), `background` / `ignoreExitStatus` / `alwaysRun`, a container's registry credentials or raw `options`, more than one task group. A spec missing its cost labels is refused as on Batch (`cost_labels.check_spec`), since `deployment` is the tenant.
- **Identity** (2026-11-27; a metadata server per job since 2026-11-28): a job runs as its spec's service account, never as the host. The supervisor creates the job's own Docker network (`disky-<job>`) and serves a GCE metadata server on its gateway ([`cloud/src/dt_cloud/job_metadata.py`]: project id, the account's email, scopes, aliases, universe domain, `token`), answering only that network's subnet and only with `Metadata-Flavor: Google`. Every container of the job gets its address, so every Google client in it takes the GCE path to it: Python's google-auth (gcsfs, `google-cloud-storage`, `gcp.session`, and the `disk-tree` CLI, which never imports `dt_cloud`), Go's, Java's, C++'s. Its tokens are minted for the job's account (IAM Credentials `generateAccessToken`, under the host's credentials, which hold `iam.serviceAccountTokenCreator` on it; lifetime `$DISKY_LOCAL_TOKEN_LIFETIME_S` at submit, default 3600 s) and reminted once under 5 minutes are left, so a job runs as long as it needs. That replaced the one-shot `$GOOGLE_OAUTH_ACCESS_TOKEN` in each container (`dt_cloud.job_identity`, removed), which lasted an hour and reached only processes importing `dt_cloud`: a scan's `disk-tree bulk-list` writes its listing to `gs://` with no credentials at all. Each run's `secretVariables` are read with the current token (Secret Manager `:access`; payloads verbatim, as Batch injects them) into the engine's environment only (argv carries `-e NAME`), beside the token itself for the engine's registry helper (`docker-credential-disky`: the image is pulled as the job's account), never a container's: `job.json` holds the secrets' names, never a value or a token. Job containers can't reach the host's metadata server (`docker-no-metadata.conf`), and another job's containers, on networks of their own, get 403 from this one; host processes reach it (their connections to the gateway come from inside its subnet), as the box's own services. Tests: `cloud/tests/test_job_backend.py`, `test_job_metadata.py` (each answer; the refusals; a token reused until it nears expiry; google-auth in a fresh interpreter finding the server and refreshing through it).
- **Usage:** each container run's cgroup (from its first process's `/proc/<pid>/cgroup`: `scan.slice/docker-<id>.scope`) is sampled every 2 s by the supervisor ([`cloud/src/dt_cloud/job_usage.py`]): `memory.current`, `memory.stat`'s `anon` / `file`, `memory.peak` and the slice's `memory.current` into `usage.tsv`, and at the run's end its summary into `usage.jsonl`: wall time, exit, `memory.peak` (its last reading: the run's high-water mark to within an interval, since the cgroup goes with the container), the sampled maxima, `memory.events`' `oom_kill`, `cpu.stat`'s CPU seconds. `memory.peak` counts page cache, which a cgroup fills to its limit whenever it reads or writes more than that and reclaims before an OOM kill: `anon` is what a run can't give back.
- **Not applied:** `computeResource` and the rest of `allocationPolicy` (the slice is the budget; only its existing disks are read), `maxRetryCount` (no spot here), `logsPolicy`.
- **State** under `$DISKY_LOCAL_JOBS/<job_id>/`: `spec.json`, `job.json` (tenant, submit order, the translated runs, the disks' dirs, gcsfuse volumes, network), `status.json`, `supervisor.json` (`{pid, boot, at}`), `log` (every run's output), `exit`, `cancel` (a cancel's marker), `usage.tsv` / `usage.jsonl`, `gcs/<i>` and `gcsfuse-<i>.log`. Every read is from disk, so `status` / `logs` / `cancel` work from any process: `dt-cloud local-job status [JOB]` (with each run's usage), `local-job logs [-n N] JOB`, `local-job cancel JOB`.
- **Supervisor:** `submit` spawns `python -m dt_cloud.job_backend supervise ROOT ID`, by default as its own transient unit (`systemd-run --collect --unit=disky-job-<id> --slice=<slice> --property=KillMode=mixed --property=TimeoutStopSec=180`, so it outlives a submitter that is itself a systemd unit; `$DISKY_LOCAL_SPAWN=direct` for a detached process on a dev box). It waits its turn, sets the job up (dirs, network, metadata server, gcsfuse mounts), runs the job's containers in order, records the end and takes the setup down. Its TERM (`systemctl stop disky-job-<id>`: `KillMode=mixed` sends it to the supervisor alone) is a cancel: it stops the running container, which has 60 s to end, then tears down as after any end.
- **One job at a time per tenant** (the spec's `deployment` label): under a per-tenant `flock`, a job starts only when it is the tenant's oldest queued job and none of the tenant's is running; other tenants run beside it (all inside `scan.slice`'s budget).
- **Restarts:** a queued or running job whose supervisor is gone (its pid dead, a different boot id, or never started within `START_GRACE_S`) is `FAILED`, "supervisor lost", the next time anyone asks. A reboot doesn't resume jobs; the caller resubmits (a new id).
- **Cancel:** a queued job is `CANCELLED` at once; a running one's current container is stopped (`<engine> stop -t 60`) and its supervisor records `CANCELLED` and starts nothing more.

### Selection

`job_backend.resolve(region, project)`: `$DISKY_JOB_BACKEND` = `batch` (default) or `local`. Local reads `$DISKY_LOCAL_JOBS` (required), `$DISKY_LOCAL_WORK`, `$DISKY_LOCAL_ENGINE` (`docker` | `podman`, default `docker`), `$DISKY_LOCAL_SLICE` (default `scan.slice`), `$DISKY_LOCAL_SPAWN` (`systemd-run` | `direct`). A deployment sets them in its job env; the pilot box's would be `DISKY_JOB_BACKEND=local`.

### Callers

Through `JobBackend`:
- `append_runner.BatchRunner`, i.e. every `Runner.run_job`: the interval store's `run` (`interval_append`), the static-names runner (`static_runner`) and its merge helpers (`static_merge`), with the `StillRunning` / `wait` polling and transient-error retries unchanged. Their specs mount gcsfuse buckets, which `local` now mounts (above); none has run locally yet.
- `dt-cloud scan box submit` ("A scan on the box", below): a deployment's whole scan head as one local job.
- `vm_pack.submit` and `access_jobs.submit` (submit only, not waited on). `vm-pack`'s spec runs locally as is unless it names `secrets` (a non-GCS store's credentials).

Still on raw Batch (known gaps):
- `scan_tasks` (`scan-run task`): it clones its worker's spec from its own controller's Batch job (`$BATCH_JOB_UID`) and checks an existing worker against Batch's stored job; it only exists inside a Batch controller.
- Nextflow's `google-batch` executor (`scan_head`), the Fray backend (`scan_backend_fray`). A scan on the box doesn't use either: its head runs Nextflow's local executor inside a local job of its own.
- `batch_events.submit` (a compaction submitted from Batch's own state-change events).
- The shell's `dt-cloud batch submit`, `job submit-listing` (the listing fan-out, `wait_jobs`), `job submit-reviewed`; `job status|logs|watch|metrics` and `batch-cancel` (`cancel_labelled`) read Batch only.
- The site's REST submits (`_lib/sweepDispatch.ts`, `_lib/cwBatch.ts`) and `BatchCron` bodies (Cloud Scheduler posts them).

### For review

- **Secrets.** Resolved (2026-11-27): through the job's own impersonated account ("Identity" above), not the host's.
- **gcsfuse volumes.** Resolved (2026-11-28): the box mounts gcsfuse itself (3.13.1, pinned in `bootstrap.sh`), per job, as the job's account ("Translation" above): specs stay unchanged, and the FUSE daemon's memory is in `scan.slice`.
- **Logs.** A local job's output is its `log` file (`dt-cloud local-job logs`), not Cloud Logging: `dt-cloud job logs` (Batch only) doesn't see it, and nothing ships it off the box. Options: the store (copy `log` beside the scan's records at the end), or Docker's `gcplogs` driver (the spec's "logs to Cloud Logging"), which ties the box to GCP.
- **Identity.** Resolved (2026-11-27): impersonation of the spec's `serviceAccount`; since 2026-11-28 through the job's own metadata server, which remints its tokens ("Identity" above), so hf's hours-long listing needs no org-policy change.
- **Restart semantics.** A lost job fails rather than resumes; the scan steps already resume from their outputs on a resubmit.

## A scan on the box (Order step 1)

Built 2026-11-28 (`[cloud]`), not yet run: [`cloud/src/dt_cloud/scan_box.py`] (`dt-cloud scan box submit|report`), on the local backend's gcsfuse volumes, existing disks, per-job metadata server and usage sampling ("`Local`" above); `bootstrap.sh` installs gcsfuse.

### The gap, and the way through it

A deployment's scan is a Nextflow head whose tasks go to Batch through nf-google, past `JobBackend`. Two ways onto the box:
- **(a) the head on the box, under Nextflow's local executor: chosen.** The head is one local job (the deployment's pinned image, `dt-cloud scan head` over its own profile rewritten for the box), and every task is a process of the local executor inside that container. The whole scan is then in `scan.slice`, every process authenticates through the job's metadata server, and nothing gets a Docker socket: Nextflow's Docker integration run from a container would need the host's socket, root on the box and, through its metadata server, every tenant's account; run on the host instead, it would need a JVM and Nextflow there and a per-task token mechanism of its own. Tasks run one at a time (`queue_size: 1`), so the head container's cgroup samples while a task runs are that task's (plus the head's constant share), beside the trace's own per-task peak RSS.
- **(b) the head on Batch, its tasks on the box: worse.** nf-google posts each task as a Batch job itself; routing them to the box needs an executor of our own, and the head stays a billed Batch VM watching the box.

### What `submit` does

`dt-cloud scan box submit -a ACCOUNT -i IMAGE PROFILE` (PROFILE: the deployment's profile's path in the image):
1. Pulls IMAGE as ACCOUNT (a minted token for the registry helper) and reads PROFILE from it (`docker run --network none --entrypoint cat`): the profile the deployment's scheduler runs, byte for byte.
2. Rewrites it for the box (`box_profile`): the local executor; `work` = `/mnt/disks/nf` (an existing disk, below); `queue_size` 1 (`-q`); machines reduced to their `time` (the local executor refuses a task asking for more memory than the host has: r2's `static` asks 30 GB); sources' machines dropped, `procs` / `threads` at most the box's CPUs; every `options.<step>.mem` and `$DUCKDB_MEM` at most the DuckDB cap, every `threads` and `$DUCKDB_THREADS` at most the CPUs; `$DT_MEM` the steps' share of the budget (what `{mem}` sizes from); `$DT_DATA_MOUNT=/mnt/disks`; `$GCP_PROJECT` the executor's project (the steps' `secrets exec`); `$DT_LIVENESS_PID` empty (each step logs its own liveness line, as a Batch task does). `-x ENV` drops a secret binding the account can't read; `-S STEP` leaves a step out. Refused: a data store that isn't `gs://`, and a step left on that submits Batch jobs (`options.static-names.profile` / `compact_cmd`, `options.interval-store.profile`, `options.vm-pack.batch`, `access-jobs`): the box's job accounts have no Batch.
3. The budget: `-B` (default `scan.slice`'s `memory.max`, 8 GiB) less 1.5 GiB for the head (Nextflow's JVM at `-Xmx768m`, the head's Python, the bucket's gcsfuse) is `$DT_MEM` (6.5 GiB); 60 % of that is the DuckDB cap (3 GB; `-m` overrides).
4. Writes the box profile and `box.json` (the budgets) to the head's disk (`/var/lib/disky/work/_disks/<tenant>-nf/profiles/<job>.{yml,box.json}`) and submits the head (`head_spec`) as local job `<tenant>-box-<YYYYMMDD-HHMMSS>`: entrypoint `dt-cloud scan head /mnt/disks/nf/profiles/<job>.yml`; the data bucket as a gcsfuse volume at `/mnt/disks/<bucket>` (nf-google's mount point and options); the existing disk `<tenant>-nf` at `/mnt/disks/nf` (Nextflow's launch and work dir: its `-resume` cache and the run's reports stay on the box); the head's own secrets (the D1 token, the Slack bot) when its profile records the run; `$DT_RUN_ID` = the job id, `$DT_TRIGGER=manual`, `$DT_TRIGGER_BY` (`-b`), `$DT_RUN_MACHINE` (`-l`, default `vm-pilot`: not a priced type, the box's cost is fixed), `$DT_RUN_REGION` (`-r`); 6 h limit. `-s SCAN -g GEN` resumes a box scan (its Nextflow session, from the disk's cache). `-n` prints the box profile, the spec and the DAG, and writes nothing.

Kept from Batch: the run record and every step's phases on /scans, the alerts and the run thread (`nominal`), every data-store output at its key (through the gcsfuse mount: `listing/<scan>/`, the index, the snapshots, `access/`), the interval store's and the static name index's one-machine appends (each appends every scan after its generation's newest: scan-id order), the R2 publish, `index-sync`, the digest, `vm-pack`. Not kept: Nextflow's reports go to the box's disk (`runs/<scan>/` there), not `<work>/runs/<scan>/` on GCS (the scan-runs profile's `nf-dag` output isn't measured); the run has no Batch jobs, so `finish` prices nothing; the base config's 30 s poll leaves up to 30 s between tasks.

What r2's VM account can't do (its grants in r2's `infra/gcp/__main__.py`): read `oa-r2-cf-pages-token`, the digest's Pages token (so `-x CF_PAGES_TOKEN`, else the digest's `secrets exec` fails; without it the plot's `wrangler pages deploy` runs on the D1 token and likely fails, and the digest step with it: a soft step, which the next Batch scan converges, unless r2 grants that secret), and anything of Batch (`gcp-cost`'s compute half, `run-cost`: `-S` both, or they fail softly).

### Measuring a run

`dt-cloud scan box report JOB`: per traced task, its wall time and peak RSS (Nextflow's trace) and the maxima of the head container's cgroup samples while it ran (`anon`, `current`, the slice's `current`); then the container run's `memory.peak`, max `anon`, OOM kills and CPU seconds. Written beside the run's trace (`runs/<scan>/usage.{json,tsv}` on the head's disk). `dt-cloud local-job status JOB` prints the run's summary.

Expected, from r2's Batch traces (2026-11-28T0716, T0838; Batch's own machines):

| task | wall | peak RSS | at the box's budget (8 GiB slice, DuckDB ≤ 3 GB) |
|---|---|---|---|
| `list_source (r2://marin-na)` | 7m17s–7m24s | 3.1 GB | fits (4 procs × 4 threads on 4 vCPUs) |
| the other three sources | 14–24 s | 0.35–1.4 GB | fits |
| `pre` (`access-r2`, DuckDB 2 GB) | 1m30s | 2.6 GB | fits |
| `index` (path-index, DuckDB 12 GB) | 51–54 s | 3.6 GB | fits at 3 GB, slower |
| `index_rows`, `publish_r2` | 13–18 s | ≤ 0.46 GB | fits |
| `interval_store` (DuckDB 12 GB) | 2m05s–2m10s | 2.8 GB (PSS 2.6 GiB, DuckDB 1.3 GiB) | fits |
| `static_names` (DuckDB 12 GB) | 9m–14m28s | 6.3–12.6 GB | **at risk**: the append's catalog refold reached 12.1 GiB RSS; at 3 GB DuckDB must spill, and any part not bounded by `-M` is an OOM kill in the slice. It also mirrors the 14.1 GiB generation to `/tmp` (the container's layer on the data disk) |
| `post` (digest, warm-up, probe, retire, age store, `vm-pack`, …) | 13m24s–13m57s | 2.5–2.7 GB | fits (22–25 % CPU: mostly waiting) |

One at a time that's ~45–75 min, plus the first image pull. Every task shares the slice with the head's ~1–1.5 GiB and gcsfuse; `memory.peak` will read near 8 GiB for any task moving more bytes than that through the page cache (the listings, the 14 GiB mirror), so `anon` and the trace's RSS are the numbers to read.

### Running it

On the box (root; `/etc/disky/job.env`), with the deployment's scheduled scans and canaries held for the run:

```bash
sudo bash -c 'set -a; . /etc/disky/job.env; set +a
  dt-cloud scan box submit -a oa-r2-usage-vm@oa-internal-450019.iam.gserviceaccount.com \
    -i <r2 scan image, by digest> -b <who> -r us-west1 -x CF_PAGES_TOKEN -S gcp-cost -S run-cost \
    /app/job/profiles/r2.yml'
sudo bash -c 'set -a; . /etc/disky/job.env; set +a; dt-cloud local-job status <job>; dt-cloud local-job logs -n 60 <job>'
sudo bash -c 'set -a; . /etc/disky/job.env; set +a; dt-cloud scan box report <job>'   # after it ends
```

Abort: `dt-cloud local-job cancel <job>` (or `systemctl stop disky-job-<job>`): the head gets its TERM, has 60 s to finish its run record, and the job's mount, network and metadata server go with it.

## Runbook (the box's infra)

Built 2026-11-27 (`[cloud]`), not applied. Everything is shared infra owned by the root session.

### Where it lives

| what | where |
|---|---|
| The box as a component: account + grants, its own VPC (no ingress but IAP SSH), Cloud NAT, data disk, instance | [`infra/gcp/vm_box.py`] (`VmBox`) |
| The `shared` GCP stack: project `disky-shared-gcp`, stack `shared`, config `vmPilot` | [`infra/gcp/shared/`] |
| The `shared` Cloudflare stack: project `disky-shared-cf`, stack `shared`, the tunnel, its route, `vmd-pilot.oa.dev` | [`infra/cf/shared/`] |
| The startup script, the units, `with-secrets`, `pack-sync`, `vmd-status`, the Ops Agent config | [`deploy/vm-pilot/`] |
| The pack sync's pull | `dt-cloud vm-pack pull` ([`cloud/src/dt_cloud/vm_pack.py`]) |
| Tests (Pulumi mocks, from the committed `Pulumi.shared.yaml`s; in CI's infra job) | `infra/tests/test_vm_pilot_stack.py` |

Why there: a `shared` stack per provider, each its own Pulumi project in its own directory. `infra/gcp/` and `infra/cf/` on a deployment branch hold that deployment's `Pulumi.yaml` and `__main__.py`; a shared stack there would collide with each of them on the next merge of `cloud`. State is in `gs://oa-pulumi` beside the deployments' stacks (keyed by project/stack, so no collision), with the same KMS secrets provider (`ops/pulumi`). `infra/cf/r2-access/` is the precedent for a project of its own under a provider directory.

### What it makes

- **GCP** (23 resources; applied 2026-11-28 04:31Z): the account `disky-vm-pilot@oa-internal-450019.iam.gserviceaccount.com` with `logging.logWriter` and `monitoring.metricWriter` (the Ops Agent's host metrics; without it the agent's self-metrics fail every few seconds), `secretAccessor` on its own three containers (below), and `iam.serviceAccountTokenCreator` on exactly each tenant's narrow VM account, owned by its own deployment's stack (Ryan, 2026-11-27: never the full job account): `oa-r2-usage-vm@oa-internal-450019.iam.gserviceaccount.com` (a scan's runtime secrets, no Pages edit or search token) and `hf-usage-vm@oa-internal-450019.iam.gserviceaccount.com` (a read-only R2 key for the pack pull only): nothing else. Everything the box does for a tenant (its jobs, its images, its pack sync's store credentials) it does as that tenant's job account, which already holds its deployment's secrets, data bucket and images; no grant is needed for the box as itself (the pack sync's R2 reads use the tenant's R2 keys, read as its job account). The VPC `disky-vm-pilot` (subnet 10.64.0.0/24 in us-west1, Private Google Access) with one firewall rule, IAP's 35.235.240.0/20 to TCP 22, so the default network's `0.0.0.0/0` rules never apply; a Cloud NAT for egress (no external address). The instance `disky-vm-pilot` (e2-standard-4, us-west1-b, Debian 12, 20 GB pd-balanced boot disk, shielded, OS Login) and its 50 GB pd-balanced data disk `disky-vm-pilot-data`. Labels: `app=disky`, `deployment=shared` (the provider's defaults) and `component=vm-pilot` on the instance, both disks and the secrets; networks, firewall rules, routers, NAT and IAM members take no labels in GCP.
- **Cloudflare** (3 resources): the remotely managed tunnel `disky-vm-pilot`; its config, one route `vmd-pilot.oa.dev` → `http://localhost:7340` and a 404 for anything else; a proxied CNAME `vmd-pilot.oa.dev` → `<tunnel id>.cfargotunnel.com`. No page links to the hostname; `vmd` answers `/healthz` unsigned and every `/v1/*` only under a tenant's HMAC.
- **Cost:** ~$98/month for the e2-standard-4 (on demand, us-west1), ~$7 for 70 GB of pd-balanced, ~$1 for the NAT plus $0.045/GB through it (packs, the Hub, R2; Google APIs bypass it). The tunnel is free.

### The box (`bootstrap.sh`, at every boot)

From the instance's non-secret metadata (`disky-repo`, `disky-ref`, `disky-tenants`, `disky-registries`, `disky-tunnel-secret`, the slices' budgets): formats and mounts the data disk at `/var/lib/disky`; installs Docker, git, jq, the Ops Agent (the journal to Cloud Logging, no metrics), cloudflared 2026.11.0, uv 0.13.8 and gcsfuse 3.13.1 (with `fuse`; each checksummed); checks out `disky-ref` from `disky-repo`; installs the slices plus a `budget.conf` drop-in each (`serveMemoryMin`, `scanMemoryMax`); moves Docker's data root to the data disk and configures the Artifact Registry hosts; builds `vmd` + `fsb` once per `bench/full-scan` tree in `job/Dockerfile`'s Rust image, in `scan.slice`; syncs `dt-cloud` into a venv per checkout (`uv sync --frozen --no-dev --no-editable --package dt-cloud`); writes `/etc/disky/job.env` (`DISKY_JOB_BACKEND=local`, `DISKY_LOCAL_JOBS`, …, also in login shells); then starts:

- `vmd.service` (`serve.slice`, a dynamic user, loopback only) under `with-secrets -f /etc/disky/vmd.secrets` (`VMD_KEY_OA_R2`, `VMD_KEY_HF` from their secrets into its environment), serving `/etc/disky/vmd.json`.
- `disky-tunnel.service` (`serve.slice`, a dynamic user): `cloudflared tunnel run` with `TUNNEL_TOKEN` from `vm-pilot-tunnel-token`, the same way.
- `disky-pack-sync.timer` (every 10 minutes, `scan.slice`): `dt-cloud vm-pack pull` per tenant with its store's credentials, then `vmd.json` rewritten to list the tenants that have a pack (`vmd` re-reads it; a tenant without one isn't listed, so its status is `404 unknown-tenant`).

Secrets reach a process only through `with-secrets`: read from Secret Manager into that process's environment, never a file, argv, metadata or output, as the box's account for its own (`vmd`'s keys, the tunnel token) and as the tenant's job account (`-i`, impersonated) for a tenant's (the pack sync's store keys). `/etc/disky/*.secrets` and `tenants.json` hold names only. Docker: the tenants' registry hosts go through `docker-credential-disky` (the job's minted token, from the environment), and a drop-in rejects job containers' traffic to the metadata server.

### Apply

0. Land the commits on `cloud` and push `o/cloud`: the box checks out its code from `https://github.com/Open-Athena/disky.git`.
1. The GCP stack (as Ryan's gcloud account; read-only first):

   ```bash
   cd infra/gcp/shared
   pulumi stack init shared --secrets-provider gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi
   pulumi config set --path vmPilot.ref <landed cloud sha>   # commit the two changed lines of Pulumi.shared.yaml
   pulumi preview -s shared    # 22 creates, nothing else
   pulumi up -s shared
   ```

   The instance boots at once; `vmd` and the tunnel retry every 5 s until their secrets have values (step 2), then start on their own.
2. The `vmd` keys (one per tenant; the same value later becomes that site's `VM_KEY` Pages secret, when the serving half lands):

   ```bash
   openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add vm-pilot-vmd-key-oa-r2 --project oa-internal-450019 --data-file=-
   openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add vm-pilot-vmd-key-hf --project oa-internal-450019 --data-file=-
   ```
3. The Cloudflare stack, as an IaC token on OA's account with **Account › Cloudflare Tunnel: Edit** and **Zone › DNS: Edit** on `oa.dev` (in `$CF_IAC_TOKEN`; `$CLOUDFLARE_ACCOUNT_ID` and `$CF_ZONE_ID` set, as in hf's worktree), then the tunnel's token into Secret Manager without printing it:

   ```bash
   export CF_PULUMI_DIR=shared
   infra/cf/run stack init shared --secrets-provider gcpkms://projects/oa-internal-450019/locations/global/keyRings/ops/cryptoKeys/pulumi
   infra/cf/run preview -s shared   # 3 creates
   infra/cf/run up -s shared
   infra/cf/run stack output -s shared tunnel_token --show-secrets \
     | gcloud secrets versions add vm-pilot-tunnel-token --project oa-internal-450019 --data-file=-
   ```

To change the checkout or the budgets later: `pulumi config set --path vmPilot.ref <sha>` (or `scanMemoryMax` / `serveMemoryMin`), `pulumi up` (metadata changes in place), then `sudo google_metadata_script_runner startup` on the box (or reboot).

### Check

```bash
gcloud compute ssh disky-vm-pilot --project oa-internal-450019 --zone us-west1-b --tunnel-through-iap
sudo journalctl -b -u google-startup-scripts | grep disky-bootstrap   # ends "done: <sha>; vmd active, tunnel active"
systemctl show -p MemoryMin serve.slice; systemctl show -p MemoryMax scan.slice   # the budgets
systemctl is-active vmd disky-tunnel disky-pack-sync.timer docker
gcsfuse --version                                                  # 3.13.1: the local backend's gcsfuse volumes
systemd-cgls -u serve.slice                                        # vmd + cloudflared, nothing else
curl -s localhost:7340/healthz                                     # "ok"
sudo /usr/local/lib/disky/with-secrets VMD_KEY=vm-pilot-vmd-key-oa-r2 -- /usr/local/lib/disky/vmd-status oa-r2
sudo journalctl -u disky-pack-sync -n 20                           # per tenant: pulled | current | none
```

From anywhere, through the tunnel: `curl -s https://vmd-pilot.oa.dev/healthz` (`ok`), and with the key from Secret Manager, `DISKY_SECRETS_PROJECT=oa-internal-450019 deploy/vm-pilot/with-secrets VMD_KEY=vm-pilot-vmd-key-oa-r2 -- deploy/vm-pilot/vmd-status oa-r2 https://vmd-pilot.oa.dev` (the request and the reply's signature both checked). The tunnel's connectors show as healthy on the Zero Trust dashboard (Networks › Tunnels › `disky-vm-pilot`). Logs: Cloud Logging, `resource.labels.instance_id` of `disky-vm-pilot`.

`vmd-status` answers `200` for a tenant only once it has a pack: none is published for r2 or hf yet. Either their scans turn on the `vm-pack` step (`FSB=build` in the image, `options.vm-pack`, specs/vm-mode.md §6; the deployment's call), or a first pack is built on the box, which has `fsb`, reading the tenant's store credentials as its job account (it writes `vm-pack/` into the tenant's index bucket):

```bash
sudo systemd-run --wait --pipe --slice=scan.slice /usr/local/lib/disky/with-secrets \
  -i oa-r2-usage-vm@oa-internal-450019.iam.gserviceaccount.com \
  AWS_ACCESS_KEY_ID=oa-r2-index-access-key-id AWS_SECRET_ACCESS_KEY=oa-r2-index-secret-access-key -- \
  env DISKY_R2_ENDPOINT_URL=<r2's endpoint> AWS_DEFAULT_REGION=auto \
  dt-cloud vm-pack build -o r2://oa-r2-usage-index -w /var/lib/disky/work/vm-pack
```

The box's checkout is `cloud`'s, so `vm-pack build` names the store's class columns from `cloud`'s `interval_store.CLASS_COLS` (`c2`..`c4`: r2's too). hf's store names its own: build hf's first pack with `-C 5=private_bytes,6=git_bytes` (specs/vm-mode.md §6.1; without it the pack is refused, `no column for class 2`).

### Teardown

1. GCP first (it stops the tunnel's connector): the account and the three secret containers are `protect`ed (the components' convention), so `pulumi state unprotect --all -s shared`, then `pulumi destroy -s shared` in `infra/gcp/shared`. The tenants' job accounts are untouched (only this stack's token-creator members on them go).
2. Cloudflare: `CF_PULUMI_DIR=shared infra/cf/run destroy -s shared`. The provider warns that a tunnel's config "cannot be destroyed"; deleting the tunnel removes it.
3. Any site's `VM_KEY` Pages secret, if set by then.

### Open choices

- **Identity: decided** (Ryan, 2026-11-27: per-deployment isolation). The box impersonates each tenant's job account; it holds no tenant's secrets, buckets or images itself. Still open: long steps against the token's hour (above, "For review").
- **The tunnel's front door.** HMAC alone authorizes (as specs/vm-mode.md §4.3 says); an Access application with a service token on `vmd-pilot.oa.dev` would also stop unauthenticated traffic at Cloudflare's edge, at the cost of two more site secrets. Later, if `vmd`'s exposure (its HTTP parser, `/healthz`) worries anyone.
- **Egress.** Cloud NAT (no external address at all) over an external IP with no ingress rules ($3.65/month, no NAT): equivalent exposure, NAT chosen so the box has no public address.
- **Region.** us-west1 (r2's Batch region and data bucket) over us-central1 (hf's): r2 is the first tenant scanned end to end; hf's data bucket is read cross-region (cents a month).
- **Tenant names.** `oa-r2` (r2.oa.dev's `deployment` label, so the local backend's per-tenant queue and `vmd`'s tenant agree) and `hf`. The "r2 demo" (r2.rbw.sh) is a different deployment, on another Cloudflare account, and not a tenant.

## Scheduling

- Scans as today (the r2 demo's daily ingest; hf's cron), submitted to the `local` backend. A scan arriving while one runs queues (one at a time per tenant; the box's CPU is shared).
- Inverted cost model (Ryan's): the box's price is the compute budget; scans run as often as its idle CPU allows. The pilot measures that rate for r2 + hf.

## Order

1. `JobBackend` + `local`, the slices (code: built, see "JobBackend (step 1)"), one r2 scan end to end on the box (`vmd` not yet serving): its wall time and peak memory per step at the budget. The scan's code is built ("A scan on the box"); the run is pending, by the root session with r2's crons and canaries held.
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
[`infra/gcp/vm_box.py`]: ../infra/gcp/vm_box.py
[`infra/gcp/shared/`]: ../infra/gcp/shared
[`infra/cf/shared/`]: ../infra/cf/shared
[`deploy/vm-pilot/`]: ../deploy/vm-pilot
[`cloud/src/dt_cloud/vm_pack.py`]: ../cloud/src/dt_cloud/vm_pack.py
[`cloud/src/dt_cloud/job_metadata.py`]: ../cloud/src/dt_cloud/job_metadata.py
[`cloud/src/dt_cloud/job_usage.py`]: ../cloud/src/dt_cloud/job_usage.py
[`cloud/src/dt_cloud/scan_box.py`]: ../cloud/src/dt_cloud/scan_box.py
