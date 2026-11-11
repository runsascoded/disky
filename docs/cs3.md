# Storage growth (CS3) on gcs

CS3 ("constant-size steady state") is the goal that what gcs stores grows with what changes in the scanned fleet, never with how many times it was scanned: a daily scan of an unchanged fleet should add ~nothing. Literally constant storage would further need a bounded workload and retention policy; the working target is **O(#changes) plus bounded working sets**.

This is gcs's snapshot: each prefix of the data bucket (`gs://oa-gcs-usage-dvx`) and the serving copy on R2 (`r2://oa-gcs-usage-index`), what it grows with, what bounds it, and what's open. Sizes stay behind the site's auth: `/meta` maps both buckets, with size over time and diffs. The cross-deployment picture (cw, hf, the r2 demo, each meta store) is root's `specs/deployment-cs3-harmonization.md`.

## Growth classes

- **C**: bounded (a lifecycle rule, or a fixed number of newest copies).
- **Δ**: proportional to changes in the fleet (versions of paths).
- **S**: proportional to scans (a per-scan residual): the thing CS3 is about removing.
- **T×A**: proportional to time × activity (how much the fleet is read, how often we sweep), not scans or changes.

## By prefix (2026-11-11)

| Prefix | Class | What bounds it | Status / open |
|---|---|---|---|
| `interval-store/` (GCS + R2): the map's path store | Δ | Each scan appends a run; runs merge binary-counter style; every 2^5 = 32 scans a compaction folds base + runs into a new generation (`<gen>.cNNN`), which `promote` makes current; the predecessor is kept a grace period for rollback, then deleted | First compaction promoted 2026-11-11 (`2026-10-09b.g3` → `.g3.c001`; it had been off, `"compact": "none"`, since 10-09). Schedule back on. Open: deleting a predecessor after its grace is a hand step (no generation-GC step in the job yet: `index-gc` is skipped); a compacted generation's R2 copy includes fold inputs (`pv`, `pvl`, `sv`, `svt`) the site never reads |
| `static-names/` (GCS + R2): the search index | Δ | Same run / compaction scheme | `.c002` current since 10-28; the original and `.c001` payloads retired 11-10 (forwarding records kept) |
| `listing/<scan>/` | C + S | `retire` (keep 2): the two newest scans keep their working set (listings, dir-cache, per-scan index); older scans shrink to ~one `attr.tsv` each | Open (S): retire the per-scan `attr.tsv` once nothing reads it (the interval store's `attribution` table versions the same data) |
| `access/raw/` | C | Bucket lifecycle: deleted at 7 days (GCS deletes asynchronously, so 7–8 days are present) | Done |
| `access/agg/` | T×A | Nothing yet: O(time × distinct (path, reader) pairs read per window); grows with how much the marin buckets are read | Open, low priority: fold windows older than N into a last-read summary, O(distinct paths ever read) + a fixed window. **Revisit 2027-01, or once it doubles from its 2026-11-11 size** (`/meta`), whichever is first |
| `sweep/runs/` | T×A | Nothing yet: each sweep (dry or real) keeps its manifests | Open: a retention rule for dry-run manifests; real runs' `deleted/` manifests are the audit trail |
| `nf/work/` | C | Bucket lifecycle: 14 days | Done |
| `tmp/` (data bucket) | ? | Nothing | Open: experiments belong in the scratch bucket's `tmp/` (3-day rule); move or expire what's here |
| `meta-listing/`, `meta-l2/` (the meta store's scans) | S (small) | — | Open: the meta store's own retire / interval history (see the harmonization spec) |
| `snapshots/`, `digest/`, `config/`, `probes/`, `attr/`, `marin-gcs-usage/` | small | — | — |
| Scratch bucket `oa-gcs-usage-scratch` | C | Lifecycle: 7 days, `tmp/` 3 | Done |

## Rules that keep it this way

- No full copy per scan: a new per-scan artifact needs a retirement path (`retire`) or a place in a versioned store before it ships.
- A generation is never overwritten: a compaction writes a new one, then `promote` moves `CURRENT.json`; the old one is deleted only after its grace and with the deletion listed (prefix, object count) beforehand.
- An R2 copy should hold only what the site reads (open for compacted generations, above), and nothing on R2 outlives its GCS source.
