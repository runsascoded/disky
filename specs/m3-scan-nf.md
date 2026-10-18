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

## 2. Age bars from the age pyramid

The children table's age bars (`ag`, from the per-scan `age_b0…6` columns) are missing from interval-store answers. The store must not carry them: the strata are relative to the scan day, so nearly every active dir's strata change every scan, the per-scan churn the store removes.

- **(a) Age pyramids for m3.** m3 has none (its index is the `path` + `bysize` sorts only; `/api/age-pyramid` answers `records: []`). Add `age-pyramid -c` to the `index` step (§1), and backfill the held scans (28) with a submitted job: `age-pyramid -c` over each scan's `path` sort, then `index-sync` of the new variants.
- **(b) The read** (`local`): under `intervals`, `/api/subtree` decorates the viewed node's children with `ag` from the pyramid's `1d` tier, all children in one ask (`Ask {depth: d+1, under: path}`; the root's children are one depth-1 rect). Exact by construction: `age_b<i>` buckets each file's created UTC day against the scan's day (`AGE_EDGES_DAYS` 1, 7, 30, 91, 365, 1095), and the `1d` bins are UTC days. A coarser tier can't serve: the `8d` bins straddle the 365- and 1095-day edges, and the tiers are sorted path-major, so reading recent days only doesn't save bytes. Guards: past the group guard, the view carries no `ag` at all (never part of it). A child under the pyramid's floor (`coarse_floor(fleet, 24)`: 32 KiB at m3's 560 GB) has no rows, so it gets no bar. Tests: on a fixture scan, the per-child strata equal the `age_b*` columns exactly, for every child above the floor.

## Status

- 2026-10-18: interval store built (base `2026-10-18T1014`, 28 scans, 15.58M versions, 842 MB), each capture appends; dev serves it to `?ps=iv`. Parity: series, diff, age-pyramid and the subtree tree are equal; only `ag` differs (§2). Meta store live via `ingest.sh` (bash, until §1).
