# m3 on the scan pipeline (`scan.nf`, local executor), age pyramids, and age bars from them

Ryan (2026-10-18, via the root session): "yes m3 should do the new idiomatic local pipeline". Today m3's ingest is bash (`infra/aws/ingest.sh`): path-index, upload, the interval-store append, index-sync, static-names and the meta store's scan as phases. The goal is the shared `job/nf/scan.nf` DAG with `executor: {kind: local}` running inside the Fargate task the capture trigger starts, with scan profiles beside cw's / hf's (`job/profiles/m3.yml`, `m3-meta.yml`).

Prod stays on per-scan sorts (`PATH_STORE` unset) until the interval store's answers are complete, the age bars included (§2).

## 1. The pipeline move

What m3 needs from the generic layer, to check against `specs/demo-branch.md` part 2's local-executor gaps:
- **A capture as the listing.** m3's primary scan doesn't list a bucket: the laptop's `capture` already wrote listing shards to `r2://disk-tree/captures/<host>/<root>/<stamp>/`. The profile's source is that capture (a `list` step that adopts an existing listing), not a `bulk-list`. Open: whether `scan.nf` has such a source kind; if not, a `[cloud]` one.
- **A non-GCS data store.** `data: r2://disk-tree`; steps that glob `/gcs/<bucket>/…` (path-index's `-l`) need the R2 path. `$DT_DATA_MOUNT` has no R2 FUSE on Fargate.
- **A same-day re-run** (a hand re-ingest of a capture) under `-resume`.
- **Hosts → stores** (`dt-cloud hosts resolve`, `HOST_STORES`): the NAS's captures go to store `nas`, which skips the interval store.
- The head: `dt-cloud scan run -P <profile>` from the image's entrypoint; Java ≤ 25 and Nextflow's plugins in the image (`infra/aws/Dockerfile`).
- Steps: `index` (path-index `-g` + `age-pyramid -c`), `interval-store` (append; the base build stays a submitted job), `index-sync`, `static-names`, then the meta profile (`store: meta`, the bucket as its source, skips as hf-meta's).

Each gap closes as a `[cloud]` commit with tests (the root picks them); the profile and image changes are m3's; the switch is a `pulumi up` (Ryan's go).

### Gap map (2026-10-18)

The task's FUSE-less Fargate disk is the constraint: with `kind: local` every task runs on one machine, so `data:` is a local dir (`/work/data`, the 120 GiB ephemeral disk) and outputs are copied to R2.

`[cloud]`, in order, each tested in `cloud/tests/test_scan_*.py`:
1. `Ctx.nproc` honours the cgroup CPU quota (Fargate's `sched_getaffinity` reports the host's CPUs; `ingest.sh`'s `vcpus()` is the reference).
2. `index` in `path-index` mode on a local `data:` dir: no `/gcs/` staging, listings read in place, outputs copied (`copy_atomic`); today it refuses any non-`gs://` data (`scan_steps.py` `_index_check`).
3. `publish-r2` from a local dir or fsspec root (today GCS only, `publish.py`).
4. An adopted listing: `sources[].listing` (a URL template, e.g. `{env:DT_CAPTURE}`), whose `list` copies the shards + `_SUCCESS.json` into the listing dir instead of running `bulk-list`; REPROC still fetches it (it has no other copy on an ephemeral disk).
5. `options.index.annotate`: a command run between `path-index` and the publish, for `disk-tree capture-meta` (the snapshot's physical disk/VM bytes, which `index-sync` then reads).
6. The head's scan-run facts: kind, run id from `$AWS_BATCH_JOB_ID`, machine/spot/region, the capture and queue phases dated before the job.
7. Maybe `options.static-names.source` (an `add` over a local data dir sees only this scan).

m3: profiles `m3.yml` (store `laptop`), `m3-nas.yml`, `m3-meta.yml`; the image (Java 17–26 ARM64, `procps`, pinned Nextflow, `job/nf` + `job/profiles`, the venv on `PATH`) and the Pulumi sources; the R2 key/endpoint/bucket names in the job env; `ingest.sh` slimmed to host → profile → head(s) (the head records the run itself, so the `scan-run supervise` entrypoint goes); `scan-runs.json`'s phase names. `-resume` is lost between jobs (local work dir, no cloudcache): a resubmit reruns everything, minutes at m3's size.

The age-pyramid backfill doesn't need the DAG: `dt-cloud age-pyramid -c` over each held scan's `path` sort, then `index-sync` of the new variants, in one submitted job once the image can run it.

## 2. Age bars from the age pyramid

The children table's age bars (`ag`, from the per-scan `age_b0…6` columns) are missing from interval-store answers. The store must not carry them: the strata are relative to the scan day, so nearly every active dir's strata change every scan, the per-scan churn the store removes.

- **(a) Age pyramids for m3.** m3 has none (its index is the `path` + `bysize` sorts only; `/api/age-pyramid` answers `records: []`). Add `age-pyramid -c` to the `index` step (§1), and backfill the held scans (28) with a submitted job: `age-pyramid -c` over each scan's `path` sort, then `index-sync` of the new variants.
- **(b) The read** (`local`): under `intervals`, `/api/subtree` decorates the viewed node's children with `ag` from the pyramid's `1d` tier, all children in one ask (`Ask {depth: d+1, under: path}`; the root's children are one depth-1 rect). Exact by construction: `age_b<i>` buckets each file's created UTC day against the scan's day (`AGE_EDGES_DAYS` 1, 7, 30, 91, 365, 1095), and the `1d` bins are UTC days. A coarser tier can't serve: the `8d` bins straddle the 365- and 1095-day edges, and the tiers are sorted path-major, so reading recent days only doesn't save bytes. Guards: past the group guard, the view carries no `ag` at all (never part of it). A child under the pyramid's floor (`coarse_floor(fleet, 24)`: 32 KiB at m3's 560 GB) has no rows, so it gets no bar. Tests: on a fixture scan, the per-child strata equal the `age_b*` columns exactly, for every child above the floor.

## Status

- 2026-10-18: interval store built (base `2026-10-18T1014`, 28 scans, 15.58M versions, 842 MB), each capture appends; dev serves it to `?ps=iv`. Parity: series, diff, age-pyramid and the subtree tree are equal; only `ag` differs (§2). Meta store live via `ingest.sh` (bash, until §1).
- 2026-10-18 (later): the 7 `[cloud]` gaps on `cloud` (2d0521b6 … 0449cb74); the age-bar read on `local` (14fffd38). m3: profiles `job/profiles/m3.yml`, `m3-host.yml` (a host store's, `@STORE@` filled by the wrapper: `nas`, an enrolled Mac), `m3-meta.yml`; `infra/aws/scan.sh` (capture → host → profile → `scan head`, then the meta head; `DT_RUN_*`, `DT_PRE_PHASES`, the R2_* names from the job's AWS_* keys); `entry.sh` picks it under `INGEST_PIPELINE=nf` (else `ingest.sh` under `supervise`, until the first clean week); `age-backfill.sh` (`INGEST_PIPELINE=age-backfill`: `age-pyramid -c` + `index-sync -A` per scan without a `1d` tier). Smoke-tested on the laptop over a NAS capture (local executor, R2/D1 writes off): fetch, `path-index -g`, `capture-meta`, the 8 pyramid tiers, the snapshot. Next: `pulumi up` (the image), a submitted `INGEST_PIPELINE=nf` re-ingest, the backfill, then `INGEST_PIPELINE=nf` in the job env. Done (2026-10-18 evening): image built; two `INGEST_PIPELINE=nf` re-ingests of capture `2026-10-18T10-14-45Z` — the first listed nothing in D1 (the pointer flip refused a local `-d` without `-b`: `[cloud]` adf9a65d, on cloud as 5d152d5a) and missed the capture phase's end (`DISKY_R2_ENDPOINT_URL`); the second clean (laptop + meta runs, 8 pyramid tiers each in D1, trigger `manual`). Backfill: 25 scans built, 2 refused (2026-09-29/30: pre-cut-over `path` sorts without `kind`/`mtime`; they keep no bars). Age bars on `?ps=iv` vs per-scan, 15 views fresh: 1,652/1,657 equal; the 5 are dirs under the pyramid's 32 KiB floor (no bar). Needed `[local]` c06db24a (the oldest bucket = `b` less the newer: epoch-dated files have no pyramid row; the top node and `(other)`) and 7352971f (file children from their day `d`). Next: `INGEST_PIPELINE=nf` in the job env (pulumi up), prod `PATH_STORE = "intervals"`.
