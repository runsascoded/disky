# Public diff demo (r2.rbw.sh): periodic re-scans + a served compare view

Grow the public open demo (`disk-tree-demo`, r2.rbw.sh) toward the mgu/cw-s3
shape: accumulate dated scans of the public buckets so the **diff treemap +
diff table** (`@rdub/treemap`'s `DiffTreemap`/`DiffTable`, already used by
`ui/src/components/CompareView.tsx`) can be served at the edge.

## Terminology

r2.rbw.sh is **Cloudflare Pages + serverless Pages Functions** over an R2
bucket — the same serverless model as mgu's marin site and cw-s3, *not*
static-HTML-only. "Serving compare" means adding a Pages Function; there is no
inherent perf penalty. The two candidate mechanisms (below) are both serverless.

## Current state

- The demo was populated **once, by hand** (`specs/done/cfn-demo-and-flask-localhost-peer.md`):
  `disk-tree index r2://<bucket> --to r2://disk-tree-demo/scans/` for
  `nj-crashes` / `jc-taxes` / `ctbk` → **one scan per bucket**. Nothing
  re-scans them; `reduce.yml` is `workflow_dispatch`-only, `sync` writes only to
  the local/private store, and the sole scheduled job is the 6 h launchd scan of
  the private `/Users/ryan`.
- Compare is **not served statically**: `ui/functions/api/capabilities.ts` says
  `compare: false`; there is no `/api/compare` / `/api/diff/status` Function, so
  those routes 501. `CompareView` works fully against the Flask server only.
- The core `DiffTreemap` + `DiffTable` are complete (`packages/treemap/src/diff/`,
  shared via `@rdub/treemap`) and **wired** by `CompareView` (treemap + table).
  mgu's `gcs` FE renders only the treemap (no table wired) though the shared
  widget is available; `cw-s3`/`factored` FE do wire the table. So this is a
  wiring detail of one shared widget, flowing bidirectionally (see
  `serverless-reference.md`), not a capability dt owns alone.

## Phases

### Phase 1 — periodic re-scans ✅ (`.github/workflows/rescan-demo.yml`)

Daily cron + `workflow_dispatch`: for each demo bucket,
`disk-tree index -C -q -D r2://<b> --to r2://disk-tree-demo/scans/`. Each run
adds a `<uuid>.parquet` + `.scan.json` under `scans/`; the Functions already
list newest-per-path and expose `/api/scans/history`, so dated points just
accumulate. Same R2 auth as `reduce.yml`. `-D` because a stateless runner has no
prior scan locally (diff indexes are Phase 2). Cron time (`23 8 * * *`) is a
guess — align with nj-crashes' daily refresh.

**Blocker found on first dispatch (run 34400284520):** the scan of `r2://ctbk`
succeeded but writing the blob to `r2://disk-tree-demo/scans/` failed with
`PermissionError: Access Denied` (s3fs `put_object`). The GHA R2 token
(`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`) can **read** the source buckets but
lacks **write** on `disk-tree-demo`. Fix (account-side, user): grant that token
Object Read **& Write** on `disk-tree-demo` (or scope it to all four buckets),
then re-dispatch. Until then no second scan accumulates, so compare stays
live-blocked.

### Phase 2 — diff computation  ⟵ OPEN DECISION (storage policy)

A diff index `<a>-<b>.parquet` is the per-path outer join of two scans (old/new
size+count + status, sorted `(depth,path)`); its size ≈ **one scan**,
independent of N. The only question is *how many pairs we persist* — a policy
choice, not inherent to any mechanism. **Never pre-build all i×j pairs (O(N²),
unbounded).** Options, from least to most caching:

- **(B) Pure on-the-fly (default).** No persisted diff index. The compare
  Function reads the path-prefix *slice* of both scans from R2 and outer-joins
  them at request time (bounded per request — thousands of rows for a subtree,
  not the whole blob), edge-cached per `(scan1,scan2,path,budget)`. This is what
  marin's `site/functions/api/diff.ts` does. **O(1) diff storage**, bounded
  compute. The "compute diffs on the fly" reading.
- **(A-consecutive) Cache consecutive pairs only.** Persist just scan_i↔scan_{i+1}
  — exactly what `disk-tree index` already builds ("diff index against the
  path's *previous* scan"): **one** new index per re-scan → **O(N)** total, each
  O(scan), GC the oldest (keep last K). Makes the default "latest vs previous"
  view an instant slice (`ui/cfn/parquet.ts` pushdown, like `scan.ts`).
  Non-adjacent pairs still fall through to (B). Publish step needed: teach
  `index --to` to also upload the diff index it builds, or a `diff-index --to
  <url>` (mirrors `snapshots --diffs`, which writes the *file-tree* layout, not
  this scans-manifest tier).

**Recommendation: start with (B)**, add the (A-consecutive) O(N) cache only if
the default diff feels slow. Storage stays O(1)→O(N), never O(N²); per-request
compute is bounded either way. Phase 3's Function implements (B) first; the
cache is a later, transparent fast-path (Function checks for a persisted index,
else recomputes).

### Phase 3 — serverless compare Function (the convergent optimum) ✅ built

Landed: `ui/functions/api/compare.ts` + the pure `ui/cfn/diff.ts` (join + status +
byte-floor + depth-≤2 frontier) over the shared `ui/cfn/scanRead.ts` (extracted
from `scan.ts`, chunk-following reused). `compare: true`; the response omits
`index`, so `CompareView` treats it as final (no 3 s poll). `cfn/tests/compare.test.ts`
covers flat + recursive + added-subtree drill over a two-scan fixture. **Live-
blocked** on Phase 1 producing a *second* scan of a bucket (a diff needs two)
and a nav entry (Phase 5) to reach `/compare/*`.

Design below.


**Fleet finding (why not depth-by-depth streaming; refreshed 2026-09-10):**
neither deployment streams the diff to the client. disk-tree `/api/compare` is a
best-first |Δ| heap walk (`diff.py:251 recursive_diff`, `budget` expansions) +
persisted-index 3 s poll-and-refetch; **mgu** `site/functions/api/diff.ts` reads
*both* scans' index tiers at one shared byte-floor and returns table-ready rows
as an edge-cached JSON *response* (not a precomputed blob — mgu's read layer is
on-the-fly hyparquet slicing, and in fact a generation ahead of dt's: D1-stored
row-group footers, multi-tier coarse floors; see `serverless-reference.md`).
Depth-ordered iterative deepening exists only in `/api/filter/stream` (single
scan, SSE) and build-side in `iter_diff_depths` (→ parquet). Streaming
depth-by-depth over the wire **fights edge-caching** (a `ReadableStream` is
awkward to `cache.put`) and the treemap only needs ~2 visible levels to
first-paint — so the optimum is a *synthesis*, not streaming:

- **Shared byte-floor bound** (from mgu): `threshold = max(rootA,rootB) ·
  minArea/(w·h)` — viewport-relative and deterministic, so the edge-cache key is
  exact and `min_frac` (undrawable cells: bytes *and* |Δ| both under floor)
  becomes the primary prune, never walked.
- **Depth-≤2 slice + lazy drill** (from `scan.ts`): return the viewed uri's diff
  to depth 2 in one cacheable request; `DiffTreemap.fetchSubtree` re-requests
  rooted at a drilled node (each independently cacheable). First paint in one
  round-trip, deep work deferred *and* cached.
- **Best-first |Δ| frontier ordering** + a `top` cut (both deployments already
  do this at emit) so `truncated` is meaningful.

`ui/functions/api/compare.ts`: read the path-prefix slice of *both* scans at the
viewed uri (via `ui/cfn/parquet.ts` pushdown, exactly as `scan.ts`), outer-join
per path → status (added/removed/changed/touched/unchanged) + Δsize/Δcount, drop
undrawable rows, order + cut. Flip `compare: true`; adapt `CompareView` so a
static (no-persisted-index) response is final — skip the 3 s index poll. Surface
a compare entry point in the demo nav.

**Fleet alignment (bidirectional, not dt-canonical):** convergence pulls the best
of each — dt's `CompareView` diff-table wiring + best-first frontier ordering,
mgu's superior read layer (D1-footer + coarse tiers). This isn't "the fleet
converges on dt"; core evolves in mgu first and CPs to dt (`dt-core-upstreaming.md`),
and dt's biggest pull is mgu's read layer, not the reverse. The full sort is in
`serverless-reference.md`.

### Phase 4 — union `path` / `hbt` (cross-account)

`path`/`hbt` are separate public-data projects. They'll land in the **HCCS
Cloudflare account** (a *different* account from `disk-tree-demo`, which lives in
the personal/RAC account), so publishing a scan of them into `disk-tree-demo` is
inherently **cross-account**: the source read uses the HCCS key + HCCS endpoint,
the blob write uses the RAC key + RAC endpoint, in one `index --to` process.

**Per-bucket credentials (built).** A single `index --to` process now selects
its key per bucket via a `profile:` in `buckets.yml` (`blobfs.bucket_profile`,
threaded through the s3fs blob IO, the `aws`-CLI lister, and the `boto3` bulk
lister). So the topology is:

```yaml
buckets:
  - uri: r2://ctbk           # + nj-crashes, jc-taxes, path, hbt (HCCS acct)
    endpoint_url: https://<hccs-acct>.r2.cloudflarestorage.com
    profile: hccs            # Object Read-only key on the source buckets
  - uri: r2://disk-tree-demo # personal/RAC acct
    endpoint_url: https://<rac-acct>.r2.cloudflarestorage.com
    profile: rac             # Object Read & Write key on the demo bucket
```

The runner materializes `~/.aws/credentials` with `[hccs]` and `[rac]` from
separate secrets, and **must not** set `DISK_TREE_R2_ENDPOINT_URL` (it globally
overrides the per-bucket endpoints). Least-privilege falls out naturally: RO on
the sources, RW only on `disk-tree-demo`.

**Note:** the *demo's serving path* (Pages Functions over R2) is unaffected —
it reads only scan blobs from the `disk-tree-demo` binding, never the source
objects. Drill-to-file shows a file's recorded stats, not its bytes; reading the
actual source object would be a new capability + a Function, and a cross-account
R2 *binding* isn't possible (bindings are same-account), so it'd fetch via the
bucket's public/custom-domain URL — a deliberate future feature, not part of
this pipeline.

Credentials (as of 2026-09-10): the HCCS **Object Read-only** R2 pair is in
`.envrc` — `CF_HCCS_R2_ACCESS_KEY_ID` / `CF_HCCS_R2_SECRET_ACCESS_KEY`
(`CF_HCCS_R2_RO_TOKEN`) — so the source *read* side is ready (bind the `hccs`
profile to it). The `disk-tree-demo` *write* side still needs a RAC key with
Object R&W (widen `disk-tree-wrangler` from "Admin Read only"). Open: run the
publish locally (`.envrc` carries both) or in GHA (add the HCCS RO pair as repo
secrets beside the RAC RW pair).

### Phase 5 — demo UX ✅ (compare action on the Scans table)

A discoverable compare/diff entry point. Landed as a per-row **⇄ compare
action** on the Scans landing table (`ui/src/components/ScanList.tsx`,
`compareColumn`), gated on `caps?.compare` — so the demo (compare on) shows it
and the Flask default keeps it, while a deployment without compare hides it.
Each links to `/compare/<path>`, where `CompareView` auto-selects the latest two
scans of that path (and self-handles the "only one scan yet" case). Compare was
already reachable from *within* a scan (`ScanDetails` ⇄ button + header
Compare); this adds it to the landing page so a diff is one click from the root.
The marking / ownership / sweep machinery of mgu/cw-s3 is checkpoint-domain-
specific and out of scope; disk-tree's demo angle is treemap + diff + age-lens +
filter over public data.

## Decisions

1. **Phase 2 storage policy** — start **(B) pure on-the-fly** (O(1) storage,
   bounded compute, edge-cached); add the **(A-consecutive) O(N) cache** later
   only if the default diff feels slow. Never O(N²). *(Resolved: on-the-fly
   default per user; the persisted set, if any, is O(N) consecutive.)*
2. **Phase 4 source** — `path`/`hbt` land in the **HCCS** CF account (not RAC),
   so publishing into `disk-tree-demo` is cross-account. *(Resolved: per-bucket
   `profile:` credentials in `buckets.yml` — built; source reads with the HCCS
   RO key, demo writes with the RAC RW key, in one `index --to` run.)*
3. **Phase 1 cadence** — daily; time non-critical (nj-crashes' GHA drifts hours),
   so no tight alignment needed.
