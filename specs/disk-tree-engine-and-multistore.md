# marin-gcs-usage as a thin domain wrapper over disk-tree (+ multi-store R2/S3)

Companion to `~/c/disk-tree/specs/gcs-backend-and-snapshot-diff.md` (the disk-tree "DT" engine + widgets + canonical-format spec). This one owns the marin side: what moves *out* to DT, what stays *private* here as domain/biz logic, and how marin adds R2 + S3 alongside GCS. Read the DT spec first — it defines the canonical format and the engine primitives this plan consumes.

## Target end-state

marin-gcs-usage becomes a **thin domain wrapper**: it does not own listing, aggregation, scan storage, diffing, or the treemap/time-series widgets — those are DT primitives. It owns only what's genuinely marin-specific:

- **Attribution overlays** — `identities.yaml`, deepest-prefix-wins owner join, and the per-node team/user/shared rollups (`tm`/`us`/`sh`) that today live baked into `tree.json`.
- **Cost** — storage-class pricing over DT's generic class-bytes (`cb`).
- **W&B mining** — entity/run → owner enrichment.
- **The site** — `gcs.oa.dev` (CF Pages + CF Access), composing DT's `@disk-tree/react` widgets + marin's own chrome.
- **Batch specs / scheduler** — the daily GCP Batch job (`job/`, `batch.py`), now invoking DT's listing + aggregation instead of marin's private `bucket_list.py`.

## What moves out to DT vs. stays here

| Capability | Today (marin, private) | Target |
| --- | --- | --- |
| Sharded listing (prefix bin-pack, range-shard, reservoir quantiles) | `src/gcs_usage/bucket_list.py` | → DT `backends/{gcs,r2,s3}` + sharded-bulk primitive |
| Listing → per-path aggregation | DuckDB group-by (highmem Batch node) | → DT out-of-core DuckDB aggregation (canonical layer-2 parquet) |
| Snapshot diff | `cli.py compare` (terminal table) | → DT `dt diff` CLI + treemap Δ-view; keep marin's as a zero-dep quick-check |
| Treemap / time-series viz | marin's DIY treemap in the site | → **upstream** the treemap to `@disk-tree/react`; import it back |
| Multi-source listing merge (SII/S3-Inventory shim) | `listing.py` | → DT import backend |
| Attribution overlays (`tm`/`us`/`sh`), pricing, identities | `src/gcs_usage/*`, `identities.yaml` | **stays private here** |

## The format move (the part that must not be done wrong)

marin's Batch job emits **two** artifacts, and only one is a scan (confirmed 2026-08-03):

- Raw per-object listing parquet, retained at `gs://oa-gcs-usage-dvx/listing/<date>/` (`bucket,name,size,timeCreated,storageClass`, ~588M rows) — **this is DT layer-1.**
- `snapshots/<date>/{tree,meta,age}.json` — `tree.json` is a **derived, overlay-enriched presentation slice** (DT layer-3 *with marin biz logic mixed in*: `tm`/`us`/`sh`/`cb` per node), not a scan.

**Integration rule: feed layer-1 (the listing parquet) into DT's aggregation to produce DT-canonical layer-2 per-path parquet. Do NOT shim `tree.json` into DT as an opaque canonical blob** — it's the wrong layer and it carries domain logic. Overlays get re-applied by marin on top of DT's generic layer-2 output when producing site data. Because layer-2 is chosen as canonical up front, marin's near-term integration is already on the long-term path — no throwaway.

## Multi-store: R2 + S3 daily scans (near-free once DT backends land)

marin also uses R2 and S3 (less than GCS). Once DT has the generic sharded-bulk lister + backend interface, adding them is config, not code:

- **R2 ≈ S3 API** — one DT `r2` backend (S3-compatible endpoint + creds) covers it; `s3` backend covers S3.
- marin's Batch job generalizes from "list the 6 GCS buckets" to "list each configured store (gcs/r2/s3)" → same DT aggregation → same canonical scans → same attribution overlays → same site, with a store dimension.
- This is the concrete payoff of the engine-in-DT investment landing on marin first: daily cross-cloud footprint in one view.

## Site: compose DT widgets + domain chrome; deep-link

- The site keeps its domain chrome (team/user/age color modes, attribution transparency section, CF Access identity chip) and imports DT's treemap + time-series + diff-coloring widgets from `@disk-tree/react` instead of maintaining its own.
- For heavy drill-down/diff, deep-link into a DT instance loaded with marin's scans (DT "Work item E": `/scan/<id>?path=` and a `/compare?scan1=&scan2=&path=` form). Auth stays marin's problem (the DT instance sits behind the same CF Access, or marin embeds the widgets directly and keeps data server-side).

## Sequencing (near-term value, no rework)

1. **DT lands out-of-core aggregation + an import backend** (DT items A/B) → marin points its Batch job's post-listing step at DT aggregation, producing canonical layer-2 scans from the listing parquet it already retains. Marin's existing overlays re-apply on layer-2. *At this point DT's existing `/api/compare` + `/api/scans/history` + treemap already work over real marin data.*
2. **DT lands `dt diff`/`series` + treemap Δ-view + series chart + de-Plotly widgets** (DT items C/D) → marin's site swaps its DIY treemap for the upstreamed widget and gains diff/time-series views.
3. **marin adds R2/S3** once the `r2`/`s3` sharded listers are in DT.
4. **marin retires** its private `bucket_list.py` / aggregation / compare once the DT versions are proven, keeping only the domain layer. `storage-cost-attribution.md` phase 4 ("disk-tree glue") is where this import shim was always headed.

## Dependencies on the DT spec

Blocked on DT items, in order: **B** (out-of-core aggregation — the unblocker), **A** (import backend, then gcs/r2/s3 sharded listers), **C** (diff/series/treemap deltas), **D** (widget package + treemap upstreaming). Item **E** (deep links) is a convenience for the site. marin can begin the "feed layer-1 → DT aggregation" wiring as soon as B is available behind even a rough CLI.
