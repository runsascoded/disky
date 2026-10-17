# Stacks and sources: compute 1:many with sources; buckets are sources

Status: **proposal**, 2026-10-16, written from gcs at Ryan's request. Not before Monday; built by a subagent or root in parallel, isolated from prod.

## 1. The idea

Two orthogonal axes, today conflated:

- **Source**: one scanned thing: a bucket (`gs://marin-us-east1`), a prefix, an HF namespace, a laptop root. It has its own scan history, its own listing, and its own index generations.
- **Stack**: the compute and serving that produces and reads sources:
  - the pipeline profile (`job/profiles/<deployment>.yml`) and its executor;
  - the serving deployment (Pages project, D1, R2).

The feature: **a stack is 1:many with sources, and a bucket is a source.** A deployment chooses its shape by configuration, not by code:

- **One stack, many sources** (gcs today): one profile lists six buckets. On 2026-10-16, listing the remote one (`marin-eu-west4`) from us-central1 cost +2.5–7.5 min, off the critical path, so co-locating it isn't worth a stack of its own. One stack serves all six.
- **One stack per source**: six profiles, one per bucket, each with its own pipeline, possibly in its own region and project. The FE↔BE contract looks the same as today. The BE↔data side fans out 6 ways, each part smaller and summing to today's work. That may be faster, since per-source work parallelizes in ways the union pass can't.

Either way, the site's multi-bucket view is the same thing as a multi-source view. It's the federation model we already have (§2), made first-class, rather than a special case of one scan whose top-level dirs happen to be buckets.

## 2. What exists ([federated-scans.md], [done/multi-store.md], [done/union-of-roots.md])

- **Union at ingest.** r2.rbw.sh lists three buckets into one `path-index` and one D1. gcs does the same with six buckets: one `dt-cloud path-index` pass over all listings. [federated-scans.md] calls this "the migration bridge, not the target model".
- **Union at read time, within one deployment.** A `STORES_JSON` `{"union": […]}` entry is a view over the deployment's own stores. They share one D1 (rows scoped per store) and one R2.
  - **At the root,** the backend fans out to each member at its own resolved scan for one cutoff, merges with `unionRoot` (nodes tagged `src`/`at`, a shared byte threshold), and returns a `union` vector.
  - **Below the root,** the request goes to the member owning the first segment, byte-identical to `store=<member>`.
  - Code: `site/functions/_lib/union.ts`, `stores.ts`; `site/src/federatedScans.ts`, `unionSources.ts`, `FederatedScanPicker.tsx`, `SourceMark.tsx`.
- **What already works over a union:**
  - treemap + table;
  - size-over-time (carried-forward steps, `missing` per member);
  - diff (changed / unchanged / unknown per member);
  - age pyramid;
  - `?f=` filter (each member's own static index; a refusal makes the answer `partial`);
  - the federated scan picker.
- **Refused or primary-only:**
  - owners and attribution (`/api/{owners,estate,assignments,claims,actions}`, `lens`, `o`, `by`, `cl` at the union root);
  - OG, `largest`, `prefixes`, `name-summary`;
  - the interval store, warm-cache and probe for non-primary stores.
- **Not built:** cross-deployment federation (a remote source read over another stack's API with a grant), producer-published source catalogs ([federated-source-catalog.md] is a contract only).

## 3. What "first-class" adds

1. **A source is the unit of scanning.** A profile's `sources[]` already fans `list` out per source. The rest of the pipeline gains a per-source mode, beside today's union mode:
   - `index` builds one store per source: its `path`/`bysize` sorts, snapshot JSONs and D1 footers under a per-source store key;
   - static-names, interval-store and publish-r2 run per source too.

   Per source, a bucket whose listing fails no longer aborts the scan: the others publish and the failed one is `missing` in the union (today's `missing` member semantics).
2. **A stack declares its sources' stores**, so the site serves `store=<source>` for each and a `{"union": [...], "main": true}` view over them. FE↔BE stays one origin, and the union fan-out is the BE's.
3. **Cross-stack sources** (the "one stack per source" shape across deployments): the union's member resolver gains a remote kind. It reads another stack's `/api/*` with a grant token, which is the [federated-scans.md] §cw+HF design. Same response shapes, so the union merge doesn't care where a member lives.
4. **Owners over a union:**
   - **gcs's blocker.** Attribution is fleet-wide today: one roster and one actions ledger, with owner rollups across all buckets.
   - **Per-source shape.** Each member's sorts carry its own `usr` columns. The union merges per-owner totals by summing members, which is exact because the sources are disjoint.
   - **The ledger stays one table per stack,** keyed by full path, so it already applies per member.
   - Until this lands, gcs can't move off union-at-ingest without losing `/users`, the owner lens and the owner chart.
5. **Search over a union:** `?f=` already merges per-member static indexes, so per-source static-names stores make gcs's search a union too. Cost: N static indexes to build (each smaller) and N to query per search.

## 4. Progressive loading (UI)

Per-source serving makes per-member loading states possible without layout jitter:

- **Lay out first.** Each member's root total (`meta.json` / the root row) is cheap and known before any subtree arrives, so the union treemap lays out its top-level cells first, at final sizes.
- **Fill in place.** Each member's cell shows its own spinner and fills when its subtree lands. Nothing resizes, because the top-level proportions were fixed by the totals.
- **Tables do the same:** per-member rows with known sizes, children streaming in.
- **Failures stay contained.** A slow or broken member is one cell's state (`missing` / `partial`, as today), not the page's.

This is the rule: isolate loading states, never move boxes once drawn. Per-source loading is fine. Loading everything and showing it at once is only better when the parts arrive within ~100 ms of each other.

## 5. Costs and risks

- **BE fan-out.** A fleet-root view becomes N reads (D1 footers and R2 ranges) instead of one, and cold latency is the slowest member's. Below the root, nothing changes, since one member answers.
  - **Measure before committing.** `dt-cloud probe -c` on a union of gcs's six buckets vs today's single store, the same page loads, cold.
- **Root-level work** that sums across members: owners, `largest`, `prefixes`, name-summary and the OG card each need a merge, or stay refused at the union root (today's choice).
- **N× fixed costs per scan:** N static-names runs, N interval-store appends and N D1 footer sets. Each is smaller, but the per-run overheads (Batch VM boots, manifests, D1 round trips) multiply. The D1 hot window (30 scans × N stores) needs re-sizing.
- **Migration.** gcs's history is one union store. The cut is a new store per bucket from a date on; the union view's series carries the old union store for scans before it, the "legacy union history" of [federated-scans.md] §30.

## 6. Plan

Built off-prod, autonomous, isolated:
- its own scratch data prefix and its own D1 (a preview database);
- the job SA's scratch-bucket rights only;
- no writes to the gcs data bucket, prod D1 or prod R2.

Each phase lands on `cloud` with tests, and deployments adopt it by profile.

0. **Measure.** Build per-bucket stores for one gcs scan from its existing listings (`dt-cloud path-index` per bucket, into scratch). Serve them from a dev stack with a `union` view over the six. Compare:
   - `probe -c` cold latencies against the prod single store;
   - index wall time, per-source in parallel vs one pass;
   - total bytes.
   This decides whether per-source is faster, as Ryan expects (§1), or slower (§5).
1. **Pipeline per-source mode:** `index`, publish-r2, static-names and interval-store run per source when the profile says `sources_as_stores: true` (name TBD), fanned out in `scan.nf` like `list`. A failed source's steps fail alone, and the union marks it `missing`.
2. **Site:**
   - a stack's sources become its stores;
   - the union view is `main`;
   - owners and `largest` merge over the union root (§3.4);
   - progressive cell fill (§4).
3. **Cross-stack members** (§3.3): a remote member kind in the union resolver.
4. **gcs adopts it** after 0–2 pass, with a dated cut (§5, migration).

## 7. Open

- Whether gcs should split at all: decided by §6.0's numbers, not by locality (listing locality is measured and small).
- The interval store: per source, or one over the union? It's the Map's path store, so it follows whatever the reader serves.
- The static name index: per source (N small) or one union index fed by per-source listings? Search's latency budget decides.
- Where a source's own data lives (its listings, index, `/meta`): a separate data bucket by default, or, as a per-source option, a reserved prefix in the source bucket itself (`meta: {bucket: <self>, prefix: .disky/}`, the prefix excluded from its own listing). From Ryan (2026-10-17): "why not just put "/meta" data in a specific ("hidden" / "reserved") key within each bkt (config'able by the source though, ofc)? that feels nicer than setting up a 2nd bkt, though i guess that should be allowed and easy too". Not the default, for four reasons:
  - Most deployments can read their sources but not write them: Marin's GCS buckets, CoreWeave, HF repos, the demo's HCCS buckets.
  - Owners' lifecycle rules and cleanup tools act on the reserved prefix too.
  - Serving reads indexes from R2 beside Cloudflare, and the source may be in another cloud.
  - A union of sources has no single bucket to live in.

  It fits buckets the deployment owns outright (m3's R2, a user's own bucket).
- Naming: "source" already means two things (a profile's bucket URI; a federation catalog entry). This spec makes them one: a pipeline source *is* a federation source once it's a store.

[federated-scans.md]: federated-scans.md
[federated-source-catalog.md]: federated-source-catalog.md
[done/multi-store.md]: done/multi-store.md
[done/union-of-roots.md]: done/union-of-roots.md
