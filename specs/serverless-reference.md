# Serverless reference: converge dt's demo with the mgu fleet + codify IaC

The public demo (r2.rbw.sh) is a serverless (SL) Cloudflare Pages + Pages
Functions deployment over an R2 bucket of reduced scans — the same family as the
`marin-gcs-usage` (**mgu**) deployments (`gcs`, `cw-s3`). This spec is about
making dt's demo a *complete, reproducible* member of that fleet — **not** by
implementing features from scratch, and **not** by moving to a serverful backend
(SL edge reads with pushdown are faster and scale to zero). The work is
**convergence**: cherry-pick where a peer leads, wire the shared core where dt
leads, and codify the infra.

## Fleet reality (researched 2026-09-10, supersedes the stale "marin JSON blob" note)

Earlier docs (incl. `public-diff-demo.md`'s fleet-finding) described the peer as
"marin" serving a "precomputed single JSON blob". **Both are out of date:**

- It's **mgu**, not "marin" (legacy name).
- mgu **migrated off** the `tree.json` precompute onto **on-the-fly hyparquet
  slicing** (`site/functions/_lib/index.ts`, `view.ts`) — the same family as
  dt's `ui/cfn/parquet.ts`, but a **generation ahead**:
  - **multi-tier coarse byte-floors** (fine tier + coarse tiers at E=16/20/24) —
    the planner reads the coarsest tier whose floor ≤ the query threshold, so
    fewer row groups are fetched;
  - **row-group footers stored in Cloudflare D1** (`index_row_groups`), so a cold
    isolate never parses the ~5 MB thrift footer — the biggest cold-start win;
  - a **blob-manifest fallback** (`<tier>.groups.json`) for GC'd D1 rows;
  - a **by-user sort variant** for the user "lens";
  - exact `(other)` residual by subtraction.
- Core widgets flow **bidirectionally** with dt: `dt-core-upstreaming.md` records
  that `packages/react` + core "evolve here [mgu] first (the forcing workload),
  then CP onto disk-tree". So dt is the canonical/published home (dist branches),
  mgu is the co-dev peer that upstreams. "ahead/behind" on widgets ≈ "not yet CP'd".
- The `factored/cw-s3-gcs-2026-09-09` branch is **in-flight convergence**: it
  re-decomposes the combined history to separate shared core from divergent
  gcs/cw-s3 work and bring `cw-s3` (which has *no* serverless API yet) onto
  `gcs`'s dynamic architecture.

## Per-concern sort (CP-in / wire / port / adopt)

| Concern | Fleet state | dt action |
| --- | --- | --- |
| **on-the-fly parquet read layer** | mgu **AHEAD** (D1-footer + multi-tier + by-user lens) | **CP mgu → dt** — the single highest-value pull. Bring the D1-stored-footer + coarse-tier planner into `ui/cfn/`. Cold-isolate perf + fewer range reads. |
| **diff table** | shared `@rdub/treemap` `DiffTable`; **dt FE already wires it** (`CompareView`), gcs FE doesn't | Nothing to build in dt. (gcs wiring is an mgu-side CP of dt's.) |
| **filter / search** | mgu: light `q=` name-filter folded into the tier view; dt: richer `/api/filter` + vocab sidecar (Flask) | **Decide**: adopt mgu's lightweight `q=`-on-the-read approach for SL, or port dt's `/api/filter`. mgu's is simpler and already SL; dt's re-aggregates. Likely adopt mgu's shape for the demo. |
| **histogram** | **dt-only** (mgu has none; uses `series.ts` size-over-time + atime axis) | dt's own SL port if wanted — but low priority (least-central; 128 MB envelope risk on whole-blob load). Not a fleet CP. |
| **preview / file-content** | **Neither** serves it from Functions; both delegate to `@rdub/file-tree`'s `/files` browser (`ft-dt-consolidation.md`) | **Adopt file-tree `/files`**, don't build a bespoke Function. Drops the "cross-account SigV4 preview" idea entirely. |
| **capabilities endpoint** | **dt-only** (`/api/capabilities` + `useCapabilities`); mgu has none | dt's is the better pattern — candidate to **upstream to mgu**, not something dt lacks. |
| **marks / sweep / ownership** | mgu-only (GCS cost-attribution domain) | No dt analog; out of scope for dt's demo. |
| **IaC (Pulumi)** | **Neither implemented**; mgu has a written plan (`cf-iac.md`: 2 stacks, `@pulumi/cloudflare` + `@pulumi/gcp`) | **Adapt mgu's `cf-iac.md`** to dt's simpler graph (below). |

**Net:** dt's demo isn't a feature subset waiting on fresh implementation. The
real moves are: **CP mgu's read layer in**, **adopt file-tree `/files` for
preview**, **decide filter shape** (probably mgu's), **wire IaC** (adapt
`cf-iac.md`), and **upstream dt's `capabilities` pattern** to mgu. histogram is
the only genuine dt-side build, and it's optional.

## The SL envelope (unchanged, still governs any dt-side port)

A Pages Function is a Worker isolate: ~128 MB memory, a CPU-time cap, no
subprocess (scanning stays batch), JS/WASM only (the read API is TS, not the
Python — bounded duplication; don't fight it with Pyodide), R2 **bindings are
same-account**, and a subrequest cap on outbound `fetch` (binding reads are
cheap; cross-account S3 fetches count). mgu's D1-footer design is partly a
*response* to this envelope (avoid the cold footer parse) — another reason to CP
it rather than reinvent.

## Credential topology (cross-account; per-bucket `profile:` — built)

The source buckets (`path`, `nj-crashes`/"crashes", `ctbk`) live in the **HCCS**
Cloudflare account; the demo bucket (`disk-tree-demo`) + Pages project are in the
**RAC/personal** account (pinned by `CLOUDFLARE_ACCOUNT_ID` in `.envrc`). So
publishing a scan is inherently cross-account, handled by the per-bucket
`profile:` in `buckets.yml` (`blobfs.bucket_profile`, threaded through every S3/R2
seam — see `public-diff-demo.md` Phase 4):

- **HCCS read** — `CF_HCCS_R2_ACCESS_KEY_ID` / `CF_HCCS_R2_SECRET_ACCESS_KEY`
  (Object **Read-only**, `CF_HCCS_R2_RO_TOKEN`) in `.envrc`. Reads the sources.
- **RAC write** — the RAC R2 key with Object **Read & Write** on `disk-tree-demo`
  (the `disk-tree-wrangler` token, once widened from "Admin Read only").
- `DISK_TREE_R2_ENDPOINT_URL` must be **unset** (it globally overrides per-bucket
  endpoints) — each bucket carries its own `endpoint_url` + `profile`.

`CF_HCCS_*` unblocks the *read* side. The *write* side still needs a RW key on
`disk-tree-demo`. Open: run the publish **locally** (laptop `.envrc` has both
creds) or via **GHA** (needs the HCCS RO pair added as repo secrets alongside the
RAC RW pair)?

## IaC / Pulumi (adapt `cf-iac.md`)

dt's graph is simpler than mgu's (no GCP Batch, no Zero-Trust-heavy sweep):
- **RAC account** CF provider: `disk-tree-demo` R2 bucket; the Pages project +
  `SCANS` binding + `[vars]` (`SCANS_PREFIX`, `PUBLIC_OPEN`); the `r2.rbw.sh`
  custom domain + DNS; the auth D1 (`disk-tree-auth`).
- **HCCS account** CF provider: the source R2 buckets (read-only from dt's POV;
  they belong to their own projects — dt may only *reference*, not own, them).
- GHA secrets/vars (rescan workflow) — Pulumi github-provider or manual; default
  **manual** initially.

## Phases

1. **CP mgu's read layer → dt** — port the D1-stored-footer + multi-tier coarse
   planner from `site/functions/_lib/{index,view}.ts` into `ui/cfn/`. Highest
   value; also the concrete first work item. (Needs the indexer to emit the tier
   set + populate D1 row-group stats — scope during the CP.)
2. **Preview via file-tree `/files`** — track under `ft-dt-consolidation.md`.
3. **filter (SL)** — decide mgu-`q=` vs dt-`/api/filter`; implement the chosen shape.
4. **IaC / Pulumi** — adapt `cf-iac.md`; independent, parallelizable.
5. **histogram (SL)** — optional, dt-only; validate vs 128 MB first.
6. **Upstream dt's `capabilities`** to mgu — mgu-side spec handoff.

## Open decisions

1. **filter shape** — adopt mgu's lightweight `q=`-on-the-read, or port dt's
   re-aggregating `/api/filter` + vocab? (Lean mgu's for the demo.)
2. **read-layer CP scope** — how much of the D1-footer machinery does dt need vs
   its current whole-footer-read? D1 adds a binding + an index-population step;
   worth it for cold-start, but scope it.
3. **publish locus** — local (`.envrc` both creds) vs GHA (add HCCS RO secrets).
4. **IaC ownership of source buckets** — does dt's Pulumi *reference* HCCS
   buckets it doesn't own, or leave them wholly to their projects? (Likely the
   latter; dt only owns `disk-tree-demo` + Pages.)
5. **histogram** — build the SL port at all, or leave Flask-only?
