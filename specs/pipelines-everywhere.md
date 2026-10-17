# Every deployment's scan pipeline on one declared DAG

Status: §6.1 (the generic layer on `cloud`) built, 2026-10-16, plus r2's gaps (§3.1–3.4: a second store, per-call credentials, the path-index extras, a dry index-gc). On prod: cw (its 2026-10-17 00:00 cron on `dt-cloud scan head`: 86 min, \$0.73, 25 phases) and gcs (its 07:00 daily on `scan head` since 2026-10-17; the first prod run, 2026-10-17T0010, took 144 min vs 146–225 for the last five one-VM runs; `NF=0` keeps the one-VM fallback). Scratch runs passed for r2.oa.dev and hf; their cutovers are next. Phase 2 of [workflow-engine.md] (decided: Nextflow), which cw has proven on prod (§7 there). Ryan: "we need to prepare to move all deploys' pipelines to that or sthg similar. the ad hoc bash script 'WF mgmt' is not acceptable."

## 1. Where things stand

| deployment | runner today | lines | engine |
|---|---|---|---|
| cw-s3 | `dt-cloud scan head` + `job/profiles/cw.yml` (cron since 2026-10-17 00:00; was `job/nf/cw.nf` + `cw-head.sh` + `cw-step.sh`) | profile | Nextflow, google-batch |
| gcs | `dt-cloud scan head` + `job/profiles/gcs.yml` (07:00 cron since 2026-10-17; `job/run.sh` = the `NF=0` fallback) | profile | Nextflow, google-batch |
| r2.oa.dev | `job/r2-scan` | 710 | bash on one Batch VM |
| hf | `job/hf-run.sh` | 225 | bash on one Batch VM |
| m3 | `infra/aws/ingest.sh` (per laptop capture) | 209 | bash on AWS |
| r2.rbw.sh demo | `.github/workflows/daily-ingest.yml` | 186 | GHA steps |

They share one core sequence and differ only in sources and optional steps:

```
bucket-check, pre ─┐
list ×source ──────┴→ index ─┬→ static-names ─────────────────┐
                             ├→ publish-r2 → interval-store ──┼→ index-sync → post: over-time, index-gc, warm-cache, probe, digest
                             └→ index-rows ───────────────────┘
```

- gcs adds: access-ingest (a soft `pre` step), `lifecycle pull` and `rules` (non-fatal), healthcheck, a second digest transport (`digest -P discord`), and its index variant (`dt-cloud path-index` over every listing at once, with attribution and access inputs, on a local-SSD machine). SWEEP, ACCESS_ONLY, GATE and SWEEP_BENCHMARK_PLAN are other job kinds, not scan steps: they stay out of `scan.nf`.
- m3 runs per capture, not per schedule: `hosts resolve`, ingest of an uploaded capture, then the core from index on.
- hf lists with `bulk-list hf://`. r2.oa.dev and the demo list R2.

## 2. Shape (as built)

1. **Steps are `dt-cloud scan step <name> [arg]`, Python** (`cloud/src/dt_cloud/scan_steps.py`). One function per step, registered with `@step(name, core=, skippable=, stage=, soft=, arg=, options=, requires=)`; the module docstring is the how-to for adding one (copy a step). Each step:
   - reads the deployment's profile (§3: `$DT_SCAN_PROFILE_JSON`, which the head passes, or `$DT_SCAN_PROFILE` / `-p FILE` by hand) and the scan's identity, stamped once by the head and never re-derived in a task: `SNAP_ID` (also exported as `SCAN_ID`, which the scan-run wrapper reads), `GEN`, `DT_JOB_START`, `DT_RUN_ID` (the head's Batch job uid; a task whose own `BATCH_JOB_UID` differs records its own job and machine per phase, 25a483ef);
   - records its phases into `scan_runs` in-process (`JobRun.begin` / `end`, the same names as `cw-step.sh`: `bucket-check`, `list <b>`, `import <b>`, `layer-2`, `webdata`, `publish`, `index-write`, `static-names` (overlapped), …);
   - skips work its own outputs say is done (`ctx.done(path)`: the source's layer-2, the index's `path-index.parquet`, copied last), or calls a command that is idempotent itself (publish-r2's size + md5, the static-names / interval-store appends after the newest scan they hold, `index-sync -r`'s INSERT OR REPLACE);
   - checks its inputs (`ctx.need(path)`): a cached `done` marker can point at a lost output, so a missing input fails naming the producing step, and `scan head -F` reruns every task with Nextflow's cache off (each step then skips what's done). The cache only saves the VM boots.
   - Commands are the existing CLIs, run as subprocesses with argv logged (`+ …`) and secrets only in the env. The cw-specific bits stay on cw's branch and are named by its profile (`job/cw-webdata.py`, `job/digest.yml`, `job/static-names.json`).
   - `soft` steps never fail the run: an unexpected error is a `WARN` and exit 0, so the DAG's "soft dependency" (index waits for access-ingest but runs without its output) is a step property, not a Nextflow feature.
   - Tests and DAG checks: `DT_SCAN_STEP_FAKE=<file>` makes every step append `<name> <arg>` and return; `DT_SCAN_STEP_FAIL=<name>` fails that step.
2. **One pipeline: `job/nf/scan.nf`** (+ `job/nf/base.config`). The DAG above; optional steps gated by `params.steps`, fan-out over `params.sources`, deployment-only steps through `params.pre` / `params.post` (a registered step's `stage`). Every task runs `dt-cloud secrets exec [-p <project>] -s ENV=SECRET … -- dt-cloud scan step <name> [arg]`. Channels carry only `done` markers; a gathered input is a sorted list of strings, so a task's cache key doesn't depend on the order its upstreams finished in (it did, and a resume reran `index`).
   - **Folding** (`fold`, default both): a task boots in ~1.5–2 min and the local executor refuses a `gs://` work dir, so tiny steps can't run in the head; bucket-check runs in the first source's list task, index-sync in the post task. `fold: []` gives each its own task.
   - **index-rows / index-sync**: `index-sync -r` (row upload) overlaps static-names and interval-store; only the pointer flip (`-p`, which checks the rows) waits for both. For every deployment.
   - **REPROC** (`scan head -R`): no list tasks, the run starts at `index` from the archived listings / layer-2s; `REPROC=1` (unrecorded, no static-names / interval-store / warm-cache / probe). NOP_IF_PUBLISHED is just `-resume` plus the steps' skip-if-done.
3. **One head: `dt-cloud scan head PROFILE`** (`scan_head.py`), replacing `cw-head.sh`:
   - stamps `SNAP_ID`, `GEN` and `DT_JOB_START` (each kept when set: a resume);
   - `scan-run start -B -g GEN` (in-process, when the profile's env names `SCAN_RUNS_PROFILE`);
   - session = uuid5(URL, `disky/<deployment>/scan/<scan>`): cw's formula, so a scan started under `cw-head.sh` resumes under the generic head; `disky/<deployment>/<store>/scan/<scan>` for a profile whose `store` isn't the primary (§3.1); writes that one `.nextflow/history` line;
   - writes `params.json` and `executor.config` from the profile, runs `nextflow run scan.nf -c base.config -c executor.config -params-file params.json -resume <session> -with-trace trace.tsv` with `NXF_CLOUDCACHE_PATH=<work>/cache` (cloud executors), `start_new_session=True`;
   - on TERM / INT: `os.killpg(TERM)` (the JVM is the launcher script's child), never fails on Nextflow's own exit, sleeps `NF_SWEEP_DELAY` (30 s: a submit in flight lands after Nextflow exits), cancels whatever carries `nf-session=<session>` (`batch.cancel_labelled`), finishes the record 143 / 130;
   - `scan-run finish -x rc [-e "nextflow exit N: <first FAILED task> (exit M) failed (session …)"] -l nf-session=<session>`; on success prints `SCAN-JOB-DONE <scan> -> <data>/snapshots/<subdir>/<scan>`;
   - `-n` writes the launch files and prints the command; `-F` cache off; `-R` REPROC; `-w` launch dir (local: the profile's `work`, which holds the resume cache; cloud: `$TMPDIR/nf-<scan>`); `-N` / `$DT_NF_DIR` where `scan.nf` lives (default `job/nf`).
4. **Config is data.** `base.config` holds the generic process settings: `containerOptions` (`--entrypoint=/usr/bin/env`; the literal `--entrypoint=""` fails; on google-batch also `-v /mnt/disks/<work bucket>:/mnt/disks/<work bucket>:rw`, without which step writes went to the container's filesystem and were silently lost), `resourceLabels` (profile `labels` + `$DISKY_LABELS` + `component: scan` + `nf-session`), `errorStrategy 'finish'`, `maxRetries 0`, `cache = params.cache`, `queueSize`. The executor block is generated by the head (`scan_head.executor_config`): work dir, plugins pinned (`nf-google@1.29.1`, `nf-cloudcache@0.6.2`), `google { project, location, batch.serviceAccountEmail, batch.bootDiskSize, batch.spot }` (or `aws`), image, and one `withLabel` per machine (a label with fallbacks picks its machine by submit attempt, §7). The `list` label is left out of it: a config selector would override the per-source machine, so `scan.nf` sets each list task's `machineType` / `disk` from its source, falling back to `machines.list`.

## 3. The deployment profile (as built: `scan_profile.py`)

One file per deployment, `job/profiles/<deployment>.yml`, on that deployment's branch. Generic code reads only its keys and has no deployment defaults. Validated on load: unknown keys refused (also under `options.<step>`, against the step's declared keys and types), every missing key named by its path, every problem reported at once. A cw-shaped example (placeholder names) is the tests' fixture: `cloud/tests/fixtures/scan_profiles/cw-s3.yml`.

```yaml
deployment: cw-s3                   # required: session namespace, alert titles
store: primary                      # the site store fed (`STORES_JSON` key; default primary); §3.1
executor: {kind: google-batch, project: …, region: us-central1, service_account: …, image: …, boot_disk: 50}  # | awsbatch {queue, region, image, cli_path} | local
work: gs://<data bucket>/nf         # required: `<work>/work` + the resume cache `<work>/cache`; a local dir for `local`
data: gs://<data bucket>            # required: the store steps hand off through; google-batch: the work dir's bucket (the one Batch mounts)
layout:
  layer2: cw-l2/{scan}/             # LAYER2_PREFIX: `<bucket>.parquet` per source + `index/<gen>/`
  snapshots: cw                     # SNAPSHOTS_SUBDIR (`snapshots/cw/<scan>/`); "" for `snapshots/`
  listing: listing/{scan}/          # where `list` writes listings when `index` imports them (required unless `list.import`)
sources:                            # `list`'s fan-out; the scheme picks the kind (gs | gcs | s3 | r2 | hf)
  - {uri: s3://<bucket>, machine: n2-standard-8, procs: 8, threads: 8, endpoint: https://…, args: [-L, …],
     disk: {request: 1500, type: local-ssd},   # or `disk: 100` (GB, the pd-balanced boot disk resized)
     secrets: {AWS_ACCESS_KEY_ID: <secret>}}   # this source's list call only, over `secrets` + `options.list.secrets` (§3.2)
machines: {small: e2-standard-2, medium: e2-standard-4, big: {type: n2-standard-8, disk: 200}, list: {type: e2-standard-4, disk: 100}, post: e2-highmem-4}
# a label may fall back when its task stays queued (google-batch; §7): big: {type: n2-highmem-32, …, fallback: [n2d-highmem-32], queue_timeout: 10}
steps: [access-ingest, …]           # deployment-only steps (registered with core=False)
skip: [probe, …]                    # core steps left out (bucket-check, static-names, publish-r2, interval-store, over-time, index-gc, warm-cache, probe, digest)
fold: [bucket-check, index-sync]    # the default
secrets: {AWS_ACCESS_KEY_ID: <secret>, …}   # fetched in-task (`secrets exec`, as the task's service account), whitespace-stripped;
                                             # `options.<step>.secrets` (any step) overlays it for that step's call (§3.2)
env: {SCAN_RUNS_PROFILE: /app/job/scan-runs.json, D1_DB_ID: …, SITE_URL: …, R2_BUCKET: …}
pass: [DT_RECORD, DT_ALERTS]        # the head's env vars passed through when set
labels: {deployment: cw-s3}
queue_size: 20
options:
  list: {import: true, bulk_args: [-a], threads: 8, import_args: [-e, stream, -j, "{nproc}", -m, -p, storage_class_id]}
  index: {webdata: [python, /app/job/cw-webdata.py, "{web}", "{sources}", -l, …, -a, "{date}"], lifecycle: true, mem: 16GB}
  static-names: {profile: /app/job/static-names.json}
  interval-store: {timeout: 90m, mem: 12GB}       # store: default $R2_ARCHIVE; -L derived from layout.layer2
  over-time: {mem: 16GB}
  digest: {template: cw, config: /app/job/digest.yml}
  # under `index.mode: path-index` also `age_pyramids: true`, `verify: true` (§3.3); `index-sync: {variants: [path, …]}`;
  # `index-gc: {dry: true}` (§3.4)
```

It replaces each script's top-of-file constants, `NF_SECRETS` / `NF_PASS`, the cron's env, `STATIC_NAMES=1` (on by default, `skip` to drop), `INTERVAL_STORE_APPEND` (`skip`), and `LISTING_PROCS` / `LISTING_WORKERS` / `IMPORT_JOBS` / `DUCKDB_MEM` (per-source `procs` / `threads`, `{nproc}`, step options).

- **Disks.** `disk: N` only resizes the pd-balanced boot disk. `{request: 375·k, type: local-ssd}` gives k NVMe local SSDs in RAID0, ext4, mounted at `/tmp` inside the container, the task's cwd on it too. **Probe passed** (gcs, 2026-10-16: Nextflow 26.10.2 + nf-google 1.29.1, n2-highmem-32, `request: 1500.GB` → 4×375G, 1.5T, 1.6 GB/s direct writes). The request is validated as a multiple of 375; the counts a machine allows (n2-highmem-32: 0/4/8/16/24) are GCE's to refuse at submit. Steps stage under `${TMPDIR:-/tmp}` (`TMPDIR` is unset in tasks), so a local SSD is used without config.
- **Per-source region** is reserved and refused unless it equals the executor's: nf-google 1.29.1 has no per-task location (`google.location` and `google.batch.allowedLocations` are global config, the job's region is the client's). Decided (2026-10-16): every source lists from the executor's region. gcs measured `marin-eu-west4` (164M objects) from us-central1 at 1681 s, against 1205–1536 s from europe-west4: 2.5–7.5 min more, off the critical path (marin-us-central2 lists for ~34 min). That isn't worth a second executor, so `region` stays reserved.

What r2.oa.dev's switch needed beyond cw's shape (built 2026-10-16; tests `test_scan_{profile,head,steps,nf}.py`, `test_index_verify.py`, the fixture `fixtures/scan_profiles/meta-store.yml` + its params golden):

### 3.1 A second store: `store`

r2, cw and hf also index their own derived storage (the data and serving buckets) as the site store `meta`: its own layer-2 prefix (`meta-l2/{scan}/`), `snapshots/meta/`, D1 rows under store `meta`, its own static-names generation. That is a second profile (`job/profiles/<deployment>-meta.yml`) through the same `scan.nf`, with `store: meta`; its layout, sources, static-names `gen` and skips are its own keys already. What `store` changes:
- `store` is a `STORES_JSON` key (the site's `stores.ts`: `?store=<key>`; default `primary`, the CLIs' `-s` default).
- Steps that file rows per store pass `-s <store>`, and pass nothing for the primary, so the primary's argv is unchanged: both `index-sync` halves (index-rows and index-sync), index-sync's per-scan `index-gc`, the `index-gc` step, and `over-time-groups`. `publish-r2`, `warm-cache`, `probe` and `digest` already follow the layout (`LAYER2_PREFIX`, `SNAPSHOTS_SUBDIR`, the snapshots root).
- The scan-run record: the head's `scan-run start -K <store>`, which `finish` keeps through the state file. Each task's phases write the same row (an upsert that sets `store`), so `make_ctx` gives a task's `JobRun` the profile's store over its scan-runs profile's default.
- Session: `disky/<deployment>/<store>/scan/<scan>` when not the primary. The meta scan of the same id is another run, and the primary's formula is unchanged, so existing sessions still resume.
- Refused for a non-primary store (`requires`, `primary_only`), so its profile skips them:
  - `interval-store`: the site serves a secondary store per scan (`PATH_STORE=intervals` applies only with no `store=`), and `interval-store/CURRENT.json` is the store root's, i.e. the primary's when both share the R2 bucket. r2-scan never appended meta to one.
  - `warm-cache`, `probe`: their requests carry no `store=`, so they'd warm and probe the primary's views. Teaching them `store=` is open, and cw-meta-run.sh notes the same gap.

### 3.2 Per-call credentials: `sources[].secrets`, `options.<step>.secrets`

A step's call gets the profile's `secrets` overlaid by `options.<step>.secrets`. A list call also gets its source's `secrets` over both. The most specific binding of an ENV wins (`ScanProfile.secrets_for`). r2 lists with a read-only source key and publishes with the serving key, under the same names (`AWS_ACCESS_KEY_ID`), so neither can be profile-wide. Every call in a task is already its own `dt-cloud secrets exec`, so a folded step (bucket-check in a list task, index-sync in post) gets its own bindings, not its neighbour's. The head resolves the lists: `params.step_secrets[<step>]` holds the whole overlaid list for each step that has its own, and `params.sources[i].secrets` holds it for a source that has its own. `scan.nf`'s `secretsFor` picks the source's, then the step's, then `params.secrets`. Every bound name is whitespace-stripped in a step's env, not only the profile-wide ones.

### 3.3 Index extras (path-index mode)

What r2-scan builds after `path-index`, sorted by what [lean-scans.md] keeps:
- **Age pyramids: kept** (lean-scans §1.3: they stay per scan until an interval age index replaces them). `options.index.age_pyramids: true` runs `dt-cloud age-pyramid -c [-m mem] -t nproc -o <dir> <dir>/path-index.parquet`: a new CLI from r2-scan's `build_age` + `verify_age` (`index_verify.py`). It writes every `AGE_PYRAMID_BINS` tier from the `path` sort and checks each against its dated files. It is a flag, not a list of variants, because the ladder must match the site's (`agePyramid.ts`), and a subset would serve gaps. `index-sync` already syncs every variant present, so the tiers publish with no `-v`. `options.index-sync.variants` (`-v` each, both halves) is there for a deployment that wants r2's explicit list. Layer2 mode refuses the key, since `index-write` writes the tiers itself.
- **`verification.json`: kept** (the index checked against its listings, which lean-scans doesn't replace; its `interval-store verify` marker checks the store against the per-scan sort, a different check). `options.index.verify: true` runs `dt-cloud index-verify` (r2-scan's `verify_scan`, generic): each source bucket's and the total bytes / objects against the `path` sort's depth-1 rows and `meta.json`, the `age.json` histogram against the dated listing, the age pyramids and the `bysize` views where those files are written. A mismatch fails `index`, so nothing is published. The report goes beside the tiers (`<layer2>/index/<gen>/verification.json`), then to R2 with them. r2's `date_basis` field is left out (it was r2's own annotation). Under lean-scans the `bysize` check goes when that sort does (it is skipped when the file is absent).
- **`.groups` sidecars: not ported.** `path-index -P` already writes the sorts' `.groups.json` + `.groups.parquet` (`write_sorts`, `groups=True`). The age pyramids' sidecars have no reader: `index-sync` reads footers from the parquet, and `index-gc -r` retires only the sorts. lean-scans makes `.groups.*` transient anyway.
- **Archive to `gs://…/scans/<scan>/<gen>/`: not ported.** On the DAG the data store already is that archive, by layout: listings at `layout.listing`, tiers at `<layer2>/index/<gen>/`, snapshots at `snapshots/<subdir>/<scan>/`, each written once and skip-if-done. lean-scans drops r2's GCS duplicate of every per-scan tree. r2's `scan-runs.json` outputs (`gs://…/scans/{scan}/{gen}/{listing,index,snap}/`) move to the layout's keys.

`options.index.mem` is now also allowed under path-index: the age pyramids' and the check's DuckDB limit.

### 3.4 `options.index-gc.dry`

`dry: true` adds `-n` to the `index-gc` step's `-F` pass, which lists what would go and deletes nothing. r2's R2 has no soft delete, so the first deletions wait for a reviewed list. The per-scan row sweep in `index-sync` (`index-gc <scan>`) still runs, as in r2-scan. The default is unchanged.

### 3.5 What r2 / cw / hf set

- **r2.oa.dev** (`r2.yml` + `r2-meta.yml`):
  - both: `index: {mode: path-index, args: [-U], mem: 12GB, age_pyramids: true, verify: true}`, `index-gc: {dry: true}` until a reviewed list, then off. Optional: `index-sync: {variants: [path, bysize, age-pyramid-…]}`, r2's explicit list.
  - primary: `options.list.secrets` = the read-only source key as `AWS_*`; `secrets` = `R2_*` (serving), `CLOUDFLARE_API_TOKEN`, `SITE_TOKEN`, `SLACK_BOT_TOKEN`; `options.digest.secrets: {CF_PAGES_TOKEN: …}` (only digest needs it).
  - meta: `store: meta`, `layout: {layer2: meta-l2/{scan}/, snapshots: meta, listing: <its own>/{scan}/}`, sources `gcs://<data>` + `r2://<serving>` with `secrets` = the serving key as `AWS_*`, `static-names: {gen: <meta gen>}`, `skip: [bucket-check, interval-store, over-time, warm-cache, probe, digest]`.
  - Its `scan-runs.json` outputs move from `scans/{scan}/{gen}/…` to the layout's keys (§3.3).
- **cw-s3** `cw-s3-meta.yml`: `store: meta`, `layout.layer2: meta-l2/{scan}/`, `snapshots: meta`, sources `gcs://<data>` + `r2://<index bucket>` with that source's R2 key, `index.webdata` = cw-webdata.py with `-l "our storage"`, its static-names `gen`, the same skips as r2's meta. Its primary needs nothing new.
- **hf**: a meta profile the same way if it keeps its meta store. Its primary needs nothing new (`CF_PAGES_TOKEN` can move to `options.digest.secrets`).

## 4. Executors

- **google-batch**: gcs, cw, hf, r2.oa.dev. Proven on cw.
- **awsbatch**: m3, if its ingest stays on AWS. Generated (`process.queue`, `aws.region`, `aws.batch.cliPath`, `containerOptions` the entrypoint only) but unproven: the first probe is a 3-step pipeline, as cw did. The alternative is m3 on google-batch reading its capture uploads from R2/S3.
- **local**: the r2.rbw.sh demo in a GHA runner and the tests. Proven by the tests (`test_scan_nf.py`: a fixture scan through `scan.nf` with every step faked, DAG order, a failure in the post task, `-resume` rerunning only that task, REPROC with `fold: []`). The local executor needs a local work dir; whether `NXF_CLOUDCACHE_PATH` on R2 through its S3 API works with nf-cloudcache 0.6.2 (for the demo's resume across GHA runners) is unprobed.

**Pins and images.** The tasks run the head's own image when the submitter sets `$DT_SCAN_IMAGE` (by digest; it beats `executor.image`, which, baked into the image, can only name a tag), and the run records it (`start -i`): a rebuild mid-run can't split a run across images (from r2, 2026-10-17). Nextflow needs Java ≤ 25 (`NXF_JAVA_HOME`; Java 27 is refused: "Java 17 or later (up to 26)"). Plugins pinned in the generated config. The scan image needs `procps` (Nextflow uses `ps`), `NXF_HOME=/opt/nextflow` with the plugins baked in, the CLIs (`dt-cloud`, `disk-tree`) on `PATH`, and the deployment's profile + step files under `/app/job/`.

## 5. What moves out of the steps

`append_runner`'s stage machinery is a hand-built DAG inside one step: Batch submit/poll, skip-if-exists, the lease, `concurrently`. It drives static-names' stages and the interval store's ranges and merges. Once the core is on `scan.nf`, those stages become processes in the DAG with their own machine sizes, and `append_runner` keeps only the store logic (manifests, carries, prune). This step comes later and isn't needed for the migration.

## 6. Order

1. **Generic layer on `cloud`** — built (2026-10-16): `scan_steps.py` (cw's steps), `scan_profile.py`, `scan_head.py`, `dt-cloud scan {step,head}`, `job/nf/scan.nf`, `job/nf/base.config`; tests `cloud/tests/test_scan_{profile,head,steps,nf}.py`.
2. **cw on the generic layer.** It stays on its own `cw.nf` until then. The switch:
   - `job/profiles/cw-s3.yml` on cw's branch (the fixture's shape with cw's real names: buckets, `oa-cw-s3-usage-data`, project, SA, image, the eight `cw-s3-*` secrets, its env from `cw-env.sh`: `D1_DB_ID`, `D1_DB_NAME`, `INDEX_VARIANTS=path`, `SITE_URL`, `WARM_PATHS`, `SWEEP_S3_ENDPOINT` / the per-source `endpoint`, `R2_BUCKET`, `SLACK_CHANNEL`, `SLACK_ALERT_CHANNEL`, `CLOUDFLARE_ACCOUNT_ID`, `AWS_DEFAULT_REGION`, `AWS_EC2_METADATA_DISABLED`, `DT_S3_ADDRESSING_STYLE`);
   - the image: `procps`, Java ≤ 25, `NXF_HOME=/opt/nextflow` with both plugins, `job/profiles/`; the cron submits `dt-cloud scan head /app/job/profiles/cw-s3.yml` (with `DT_RUN_ID` = the head's `BATCH_JOB_UID`);
   - `job/scan-runs.json`: the scan job matcher's `commands` regex matches `dt-cloud scan head`, its log matcher `^SCAN-JOB-DONE (\S+)` (was `CW-SCAN-JOB-DONE`), `run_id_env: [DT_RUN_ID, BATCH_JOB_UID]` (already), prices for every machine the tasks use (already);
   - a scratch run first (the data bucket = the scratch bucket, small buckets, serving steps without credentials), then the first prod scans after a go. Output bytes must not change: the list / import / index-write / interval-store commands are cw's, given the profile's options. What does change: the unscanned-bucket alert's wording, the run's DONE line, `index-sync` in two halves (same end state), and `GCS_USAGE_TOKEN` → `SITE_TOKEN` carry is gone (the scheduler body passes `SITE_TOKEN`).
3. **gcs, in parallel with hf**, right after cw: Ryan wants gcs off `run.sh` as soon as possible. gcs's session writes its steps against this interface (access-ingest as a soft `pre` step, `lifecycle pull`, `rules`, healthcheck, the discord digest as `post` steps, its index as `options.index.mode: path-index` with `attribution` / `access`, the local-SSD index machine, per-source lister `args` for the chunk weights), and it still gets a scratch run first. hf is the closest to cw; the hf session does it with Ryan's go.
4. **r2.oa.dev.**
5. **The r2.rbw.sh demo** (local executor in GHA).
6. **m3**: per-capture, and its executor decided by the §4 probe.

Each deployment's old script is deleted when its first week on the DAG is clean. Every move carries the baseline in [CLAUDE.md "Deployments: parity by default"] (scan-run records, alerts, warm-cache, probe, digest).

### Run reports (DAG view)

`dt-cloud scan head` runs Nextflow with `-with-dag dag.mmd -with-timeline timeline.html -with-report report.html` (beside `-with-trace trace.tsv`). It uploads whichever exist to `<work>/runs/<scan>/` before `scan-run finish`, even when the run failed or was stopped. An upload error is logged and never fails the run. A deployment records the DAG as a scan-run output by adding `{key: nf-dag, uri: <work>/runs/{scan}/dag.mmd}` to its `scan-runs.json`. The site's /scans view draws it, colouring each node by its phase status (cw builds that part on `cloud`).

## 7. Open

- **Capacity for big tasks: fallback machine types (built, 2026-10-16; unproven on Batch).** gcs's first scratch run on google-batch (2026-10-16T2030) produced the same outputs as the one-VM scan, and its index on local SSD took 42.5 min against 48. But its n2-highmem-32 with 4 local SSDs waited ~30 min for a VM: `CODE_GCE_ZONE_RESOURCE_POOL_EXHAUSTED` ×7, then Batch requeued it. The one-VM job acquires its machine once, at the start. The DAG acquires the big one mid-run, so a stockout lands on the critical path. Batch takes one instance policy per job, and a stuck job stays QUEUED / SCHEDULED rather than failing, so Nextflow on its own never retries it.
  - **Profile**: a machine label may declare `fallback: [<type>, …]` with `queue_timeout: <minutes>`. Both are needed together (a fallback with no timeout is never reached, and resubmitting the same type waits in the same pool). They are google-batch only, never on `list` (each source names its own machine), and each type may appear once. Example: `big: {type: n2-highmem-32, disk: {request: 1500, type: local-ssd}, fallback: [n2d-highmem-32], queue_timeout: 10}`.
  - **Mechanism: Nextflow's own `maxSubmitAwait`, not a watcher in the head.** The head generates the label's `withLabel` block (`scan_head.fallback_block`). It sets `machineType = { types[task.submitAttempt - 1] }` and, on every attempt but the last, `maxSubmitAwait = '<queue_timeout>m'`. It also sets `maxRetries = <n fallbacks>` and an errorStrategy that returns `retry` only when `task.previousException.cause` is a `ProcessSubmitTimeoutException`, and `finish` otherwise, as for every other label. Read in the nf-google 1.29.1 / Nextflow 26.10.2 source (tag `v26.10.2`):
    - `GoogleBatchTaskHandler.checkIfRunning` moves a task to RUNNING only on Batch `RUNNING` / `SUCCEEDED` / `FAILED` / `DELETION_IN_PROGRESS`. A QUEUED / SCHEDULED job's task therefore stays SUBMITTED.
    - `TaskPollingMonitor.checkTaskStatus` then calls `TaskHandler.isSubmitTimeout()`, which is true once the task has been SUBMITTED longer than `maxSubmitAwait` since submit. Nextflow then kills the task (`killTask` → `deleteJob`: Nextflow deletes the Batch job itself) and sets `task.error = ProcessSubmitTimeoutException`. `TaskProcessor.resumeOrDie` counts that failure as a submit retry, not a task failure, and evaluates the errorStrategy with `task.previousException` set. On `retry` it resubmits with `submitAttempt + 1` (`attempt` stays 1, `maxRetries` bounds the submit retries).
    - The last type has no `maxSubmitAwait`, so when every fallback is exhausted the task stays queued. The retry onto the last type says so in its log line.
  - **Why not the head deleting jobs from outside.** When nf-google sees a job that was deleted externally, the outcome depends on timing. If Nextflow polls while the job is in `DELETION_IN_PROGRESS`, the task completes with exit `Integer.MAX_VALUE` ("terminated for an unknown reason"), a `ProcessException` that the errorStrategy could retry. If the job is already gone, `describeJob` returns NotFound after the client's retries (`BatchClient.apply`, 5 attempts). `handleException` → `resumeOrDie` then gets a non-`ProcessException`, never consults the errorStrategy, and returns a `TaskFault` that aborts the session. A queued job with no VM may be deleted faster than Nextflow's 30-s poll, so a watcher in the head could kill the whole run. `maxSubmitAwait` avoids that race, needs no thread or marker, and Nextflow does its own deletion after it has finalised the task.
  - **Visible**: the errorStrategy logs (Nextflow's console, i.e. the head job's log) `scan head: <process> queued <n> min on <type>; retrying on <next>[ (the last: no queue timeout)]`. Each task exports `NF_MACHINE` (its attempt's type) beside `NF_PROCESS` / `NF_TAG`. The step's scan-run phases already record each task's own Batch job and machine. In the trace a queue timeout is a FAILED row with no exit, followed by its retry's row of the same name. `first_failed` skips such rows, so the run's error names the real failure. (The trace's `error_action` reads `-` for them, not RETRY, so it can't be used to tell them apart.)
  - **Tests**: `test_scan_profile.py` (the keys, every refusal), `test_scan_head.py` (golden `withLabel` block, `first_failed` with retried rows), and `test_scan_fallback.py` end to end. That test runs the generated google-batch config under a fake Slurm (`fixtures/fake_slurm/`; Nextflow's grid handler, like nf-google's, keeps a task SUBMITTED while its queue says pending, whereas the local executor starts everything at once). `index` and `interval_store` are stuck on the primary and the first fallback, cancelled twice, and run on the last fallback. A genuine `interval-store` failure there is not retried and stops the run. The test swaps the timeout to 3 s.
  - **Probe (gcs, 22:13 UTC)**: n2-highmem-32 took 5m16s from submit to RUNNING and n2d-highmem-32 took 3m47s, with no stockout either time. A healthy acquisition with 4 local SSDs therefore takes ~4–5 min, and `queue_timeout` needs margin above that (10 is ~2×). n2d-highmem-32 is a plausible first fallback. Before it could be primary, it needs its own path-index timing: it is AMD, and the index is DuckDB-heavy.
  - **Unverified**: none of this has run on real Batch. Still to check: that nf-google's deletion of a SCHEDULED job releases its quota promptly; that the fallback's VM gets the same 4×375G local-SSD RAID (n2d-highmem-32 allows the same counts: 0/4/8/16/24); and the cost labels on the cancelled jobs (`finish -l` prices them by label, and a never-RUNNING job has no VM time). Remaining alternative, not built: `google.batch.allowedLocations` with a second region, at the cost of egress on the index's reads.
- **Engine-neutral DAG store** (asked by Ryan): `scan_run_dags` holds `{engine, dag}` with a nodes/edges JSON reduced by the writer; the raw Mermaid stays in the GCS archive. cw is doing it.

- m3: awsbatch, or google-batch over its uploads (§4).
- `warm-cache` / `probe` for a non-primary store (§3.1): `store=` on their requests, then drop the refusal.
- `options.index.mode: path-index` (gcs's index over every listing at once) is declared and refused as not ported; its `attribution` / `access` keys are accepted only with it.
- `NXF_CLOUDCACHE_PATH` on R2 (the demo's resume across runners), unprobed with nf-cloudcache 0.6.2.
- Static-names / interval-store stages as DAG processes (§5): when, and whether `append_runner`'s lease survives.
- Whether `scan.nf` should be generated from a Python DAG definition (steps, needs, executor and outputs as data, as workflow-engine §5 Phase 0 proposed) rather than hand-written Groovy. As built: hand-written `scan.nf` (~155 lines), the registry's `stage` places deployment-only steps, and the profile carries the variation. Note Nextflow 26's strict parser reserves `run` as a function name.

[workflow-engine.md]: workflow-engine.md
[lean-scans.md]: lean-scans.md
[CLAUDE.md "Deployments: parity by default"]: ../CLAUDE.md
