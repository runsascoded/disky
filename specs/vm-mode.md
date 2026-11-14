# VM mode, phase 1: an exact in-memory query server beside the Worker

Status: spec, 2026-11-13; phase 1 built on `w/vm-mode` (§ 11 records what was built and measured). Ryan's go: "#28: yes go" (2026-11-13), including a standing VM (one shared n2-highmem-16, ~$230–770/month, 24/7 for a few months, then re-evaluated). Builds on [full-scan-serving-bench.md] (the engine and its measurements) and [serverless-tiers.md] §5 (the hand-off contract), §6 (phasing) and §7 (Ryan's decisions), which bind this work. Nothing here changes a deployment: no live site wiring, no serving VM, no D1, secret or Pulumi apply.

## 0. Summary

- One server binary, `vmd`, holds each deployment's **slim pack** (the bench's slim index as a file, [full-scan-serving-bench.md] § Slim index) memory-mapped, and answers the site's views exactly: the plain map, a diff, owner / class / staged scopes, and search, under the request's scan pins. gcs's pack is 30.3 GB; the root's plain view is 0.11 s on 16 vCPU and a root search ~1 s.
- **Tenants** are deployments. One process serves several, each with its own key, pack and pins; a tenant is added, replaced (a new pack after a scan) or dropped without a restart (§ 5).
- **The cell rule** is the one in [serverless-tiers.md] §7 item 1: a node is drawn when its bytes are at least `min_share` of the view's total, `min_share = 2^-share_log2` from the viewport, rounded down to a power of 2. The request carries it; the Worker and `vmd` apply the same integer test (§ 2).
- **Shards and merge are separate layers** (§ 3). A pack splits into N path-range shards, each answering on its own with partial rollups; the merge sums them by path and applies the global cell cut. One `vmd` runs every shard in-process; a fan-out over serverless functions (Cloud Run beside GCS, Lambda beside S3) would run one shard per function behind a merging caller. N = 1 and N > 1 give identical answers (tested).
- **The pack writer** is a scan step (`vm-pack`) after the interval store's append: it mirrors the current generation's newest manifest, writes the slim pack, and publishes it next to the store under `vm-pack/` with a pointer (§ 6).
- **Parity:** the DuckDB oracle (`bench/full-scan/oracle/oracle.py`, no shared code) checks `vmd`'s answers on the repo's fixtures and on gcs; a shadow check compares them with the live Worker's answers on sampled real requests (§ 8).

## 1. Scope

In phase 1 (this branch):
1. `vmd`: the HTTP server, multi-tenant, signed requests and responses, shards + merge, the cell rule, the answer shape (§ 2–5).
2. The pack writer: `fsb pack-slim` (store → slim pack without the full pack), `dt-cloud vm-pack build` (mirror, write, publish), and the `vm-pack` scan step (§ 6).
3. The Worker adapter, `site/functions/_lib/vmAdapter.ts`: request from a parsed view, response to `/api/subtree`'s and `/api/diff`'s bodies; pure and unit-tested, not called by any route (§ 7).
4. Parity: the oracle on the cell rule; fixtures through `vmd` at N = 1 and N = 4; gcs against the oracle and against the live Worker (§ 8).
5. Measurements on the bench VM: per-query latency at 16 threads with two deployments' packs loaded together, RSS, cold start (§ 11).

Out of scope (each its own go):
- **Wiring the live site** to `vmd`: the lane decision ([serverless-tiers.md] §3), the route changes, the 202 state, caching VM answers.
- **The serving VM's infrastructure**: the instance, its disk, the Tunnel, the waker, the status object's writer service, Pulumi, secrets.
- **The Worker-scan lanes** ([serverless-tiers.md] §6 phase 3): not built; the long-term goal is an engine fast and cheap enough that lanes aren't needed.
- Incremental pack appends (each scan rewrites the pack), regex (not in the language), the hex-run rule (cw; § 4.4), `mtime` / `last_read` / per-node owner breakdowns / provenance in the answer (§ 7.2).

## 2. The query and the cell rule

### 2.1 Request semantics

A view is the bench's query ([full-scan-serving-bench.md] "Query"; `src/query.rs`'s doc comment is normative), with the cell rule replacing `min_ppm`:
- **Scans:** `scan` (a view), or `from` and `to` (a diff). Scan ids, never timestamps; the pack's scans are its generation's.
- **Folder** `path` (`""` = the deployment's root).
- **Search** `q`: the Worker's parsed `QueryAst` (`site/functions/_lib/queryAst.ts`), never the raw text (§ 4.4).
- **Owners** `{"in": [...]}` / `{"not_in": [...]}` (`""` = no owner); **classes** `[1..4]`; **staged** prefixes (the overlay's regions inside D, resolved by the Worker at the overlay head, [serverless-tiers.md] §5.1).
- **Cell rule** `cell`: `share_log2`, `atten_log2`, `depth` (below).

### 2.2 The cell rule

For a folder at depth `dp`, a node at depth `d` (`r = d − dp ≥ 1`) with bytes `x` (in a diff, `max(x_from, x_to)`), in a view whose total is `T` (in a diff, `max(T_from, T_to)`), is **drawn** when it is nonempty, `r ≤ depth`, and

```
x · 2^share_log2  ≥  T · 2^(atten_log2 · (r − 1))
```

in exact integer arithmetic (u128). Every drawn node's parent is drawn (bytes only shrink down a path and the right side only grows), so the drawn set is a tree.
- `share_log2 = ⌈log2(viewport area / cell area)⌉`: `min_share` is cell area / viewport area rounded down to a power of 2 ([serverless-tiers.md] §7 item 1). A 390×844 phone at 8 px² cells: 15; a 2560×1440 screen: 19.
- `atten_log2`: today's Worker halves the share per level below the first (`ATTEN_DEFAULT = 2`, i.e. `atten_log2 = 1`). Ryan's decision names the share only; `vmd` takes `atten_log2` (default 0) so either policy is one integer, and the Worker's policy version pins which (§ 10, open question 1).
- `depth`: the window, at most 64 levels below the folder. Default: `1 + ⌊share_log2 / atten_log2⌋` when `atten_log2 > 0` (no deeper node can pass), else 8.
- **`(other)`**: under every drawn node above the window's deepest level (and the folder), one `(other)` child holds the exact rest (its bytes and objects less its drawn children's, per side) whenever it is nonzero, with `folded` = its nonempty children not drawn. Signed per side: directory totals don't enter, so on object sums a rest is never negative.
- Legacy forms, for the bench and the oracle only: `min_ppm` (`x · 10^6 ≥ min_ppm · T`) and `floor_bytes` (`x ≥ floor_bytes · 2^(atten_log2 · (r − 1))`, an absolute threshold, used to compare with today's Worker, whose threshold is `T · minArea / (w · h)`, § 8.3).

## 3. Shards and merge

Two layers with a serialized boundary between them, so the same code runs them in one process or across machines.

### 3.1 A shard

A **shard** is a contiguous range of a pack's object rows `[lo, hi)` (rows are in depth-first path order, so a range is a path range). It answers a query by the slim pass over the folder's rows inside its range, and returns a **partial answer** (`ShardAnswer`, `src/shard.rs`):
- `total`: its bytes and objects per side;
- `nodes`: every window node it saw, each `{path, depth, kind, matched, a, b, kids, partial}`, sorted by path (segment order). `partial` marks a node whose rows cross the shard's edge; `kids` counts its nonempty children wholly inside the shard;
- `root_kids`: the folder's nonempty children wholly inside the shard.

What a shard may leave out depends on its `floor`:
- `local` (one round): a node wholly inside the shard is dropped when it fails the cell rule against the shard's **own** total. The global total is at least the shard's, so this keeps a superset of what the merge draws. Partial nodes are always kept.
- `{"bytes_total": T}` (the second round of two): the global total, known from a first round of totals-only requests; the shard drops what fails the rule against it, sending the fewest nodes.
- `none`: every node (tests).

A shard is either **logical** (a row range of a mapped pack; `vmd` splits one pack into its configured shard count) or **physical** (`fsb shard PACK -n N -o DIR`: N self-contained slim packs, each a row range with the directories its rows sit under, its own sub-dictionary, and the edge it was cut at: which directories and which object path continue past either end). A physical shard pack answers on its own, with the same partial answers as the logical one (tested).

### 3.2 The merge

`merge(answers, cell) → Tree` (`src/merge.rs`):
1. A k-way merge of the shards' node lists by path (each sorted), summing each path's sides and `kids`.
2. Each node that was partial in some shard and is nonempty after the sum counts once toward its parent's `kids` (or `root_kids`).
3. The global total is the sum of the shards' totals; the cell rule (§ 2.2) runs against it, top down (a node is drawn only under a drawn parent).
4. The tree: drawn nodes as children, sorted by bytes then name; each drawn node above the window's deepest level gets its `(other)`.

The merge is the only place the cut is final, which is why it is two-phase across machines: the cut needs the global total. In one process the shards' passes run on one thread pool and share a running total (only ever a lower bound on the final one), so in-process "phase 1" costs nothing extra.

### 3.3 A function fan-out (not built)

The boundary is what a fan-out over big serverless functions would use ([serverless-tiers.md] §7 item 5):
- Each function holds one physical shard pack (`fsb shard`; gcs's 30.3 GB at N = 8 is ~3.8 GB per shard, plus its sub-dictionary), mapped from local disk or read into memory at cold start, and runs `vmd --role shard`, which answers only `POST /v1/shard`.
- A merging caller (a `vmd --role merge`, or the Worker itself for small N) sends the request to every shard with `floor: local` (one round, more nodes back) or first `phase: totals` then `floor: {bytes_total}` (two rounds, fewer nodes), then runs `merge`.
- Cost and latency of that deployment (cold starts per shard, per-request price, the merge's bytes) are measured when it's proposed; nothing here assumes it.

## 4. The server's API

### 4.1 Endpoints

| Method, path | Who | What |
|---|---|---|
| `POST /v1/view` | the Worker | one view or diff, answered whole (shards merged) |
| `POST /v1/shard` | a merging caller | one shard's partial answer (`--role shard`, or any `vmd` for its in-process shards) |
| `GET /v1/status` | the Worker, the waker, ops | per tenant: `state`, `gen`, `manifest`, `scans`, `loaded_at`, `pack_bytes`, `shards` |
| `GET /healthz` | the load balancer | `ok` (unsigned, no tenant data) |

Every `/v1/*` call is signed (§ 4.3) for one tenant (`x-disky-tenant`); `status` lists only that tenant.

### 4.2 `POST /v1/view`

Request body (JSON):

```json
{
  "pins": {"gen": "2026-10-09b.g3.c001", "manifest": "2026-11-13T1734.m001.json"},
  "scan": "2026-11-13T1734",
  "path": "marin-us-central2",
  "q": {"alts": [[{"kind": "sub", "text": "ckpt"}]], "neg": []},
  "owners": {"in": ["alice"]},
  "classes": [2],
  "staged": ["marin-us-central2/old"],
  "cell": {"share_log2": 15, "atten_log2": 1, "depth": 8},
  "deadline_ms": 5000
}
```

`from` + `to` instead of `scan` for a diff. Every field but `pins`, the scan(s) and `cell` is optional.

Response `200`:

```json
{
  "v": 1, "tenant": "gcs",
  "pins": {"gen": "…", "manifest": "…"},
  "scan": "2026-11-13T1734",
  "total": {"bytes": 0, "objects": 0},
  "tree": {"name": "marin-us-central2", "kind": "dir", "bytes": 0, "objects": 0,
           "children": [{"name": "checkpoints", "kind": "dir", "bytes": 0, "objects": 0, "matched": true, "children": []},
                        {"name": "(other)", "kind": "dir", "bytes": 0, "objects": 0, "folded": 12}]},
  "nodes": 2,
  "timing": {"compile_ms": 0.1, "pass_ms": 107.0, "merge_ms": 0.4, "total_ms": 108.0, "rows": 0, "shards": 1}
}
```

A diff's nodes and total carry `bytes_to` / `objects_to` beside `bytes` / `objects` (the `from` side). `tree.name` is the folder's last segment, `""` at the root (the Worker substitutes its `ROOT_LABEL`).

Errors (JSON `{"error": code, "message": …}`):
- `400 bad-request`: malformed, an unknown scan, a class outside 1–4, more than 16 search atoms.
- `401 bad-signature` / `401 stale`: § 4.3.
- `404 unknown-tenant`.
- `409 pins`: the tenant's loaded pins differ from the request's; the body carries the loaded ones (`have`), so the Worker can answer 202 and wait for the reload ([serverless-tiers.md] §5.2).
- `422 unsupported`: a request outside phase 1's language (a regex or glob matcher, a hex-run deployment's search; § 4.4).
- `503 loading` (with `retry-after`): the tenant is mapping a new pack.
- `504 deadline`: the pass overran `deadline_ms`; nothing partial is returned ([serverless-tiers.md] §3.4).

### 4.3 Authentication

Both directions are HMAC-SHA256 under a **per-tenant** key (`VMD_KEY_<TENANT>` in `vmd`'s environment, the same value as the Worker's `VM_KEY` secret on that deployment), so one deployment's Worker can't read another's pack:
- Request: `x-disky-tenant`, `x-disky-ts` (Unix seconds), `x-disky-sig` = hex `HMAC(key, "v1\n{method}\n{path}\n{tenant}\n{ts}\n{sha256hex(body)}")`. Rejected when the signature differs (constant-time compare) or `|now − ts| > 60 s`.
- Response: `x-disky-sig` = hex `HMAC(key, "v1-resp\n{tenant}\n{x-disky-ts of the request}\n{sha256hex(body)}")`. The Worker checks it and the echoed `pins` before caching ([serverless-tiers.md] §5.1).
- Transport: a Cloudflare Tunnel from the VM (no public port), or HTTPS with an allowlist; either way the signature is what authorizes. Path-restricted links never reach `vmd` (the Worker refuses them first).

### 4.4 Search: a fixed contract

The search language is the site's, and which backend answers never changes what is accepted or what it means:
- The Worker parses `q=` under its syntax (`querySyntax.ts`) and sends the resulting `QueryAst`. `vmd` never sees the text, so no syntax can exist only when the VM is up.
- `vmd` maps the AST onto the bench's atoms: `sub` without `/` → a name term (anchors `start` / `end` as `^` / `$`); `sub` with `/` → a path term; `alts` → OR of ANDs; `neg` → AND NOT (OR of them). The semantics are the search language's: case-insensitive, a match of any segment covers its subtree.
- `glob` and `regex` matchers are not in the indexed-only language today and are refused with `422 unsupported`; the language version pins when they enter.
- The hex-run rule ([static-hex-runs]) isn't implemented in `vmd`: a tenant configured `hex_runs: true` refuses searches (`422`) until it is, rather than answering differently from the Worker.

## 5. Multi-tenant layout

`vmd -c vmd.json`:

```json
{
  "listen": "127.0.0.1:7340",
  "threads": 16,
  "tenants": [
    {"name": "gcs", "pack": "/packs/gcs/current.fsb", "shards": 1, "key_env": "VMD_KEY_GCS"},
    {"name": "cw", "pack": "/packs/cw/current.fsb", "shards": 1, "key_env": "VMD_KEY_CW"},
    {"name": "r2-demo", "pack": "/packs/r2-demo/current.fsb", "key_env": "VMD_KEY_R2_DEMO"}
  ]
}
```

- One process, one thread pool for every tenant's passes (a request uses all threads; concurrent requests share them). Each tenant's pack is memory-mapped; the page cache holds the pages its queries touch (gcs: 24 GB of its 30.3 GB; [full-scan-serving-bench.md] § Page-cache serving).
- **Hot swap:** `SIGHUP` (or `POST /v1/reload`, signed by an admin key `VMD_ADMIN_KEY`) re-reads the config. A tenant whose pack file changed is mapped anew (header + columns, ~1 ms) and prefaulted on a background thread; its state is `loading` until then, while the old pack keeps answering requests pinned to it. The swap is an `Arc` exchange; the old map is dropped when its last request finishes. Added tenants load; removed tenants stop answering (`404`). A deployment moves onto or off the shared VM by editing the config: no restart, no other tenant disturbed.
- **Memory:** gcs 30.3 GB (slim pack, 135 scans), cw about the same (its store holds about as many versions, 1.16e9), hf / r2 / the r2 demo / m3 each well under 1 GB. Two gcs-sized tenants and the small ones need ~62 GB mapped; an n2-highmem-16 (128 GB) holds every tenant's pages hot with room for page-cache churn, and the slim pack's measured 32 GB-per-tenant working set means even both large tenants' searches stay resident (§ 11). A process or container per deployment on the same VM is equally supported (one tenant per config); the shared process is the default because one thread pool uses the cores better than two pools fighting.
- **Status object:** `vmd` writes `GET /v1/status`'s content per tenant; the serving deployment's publisher (out of scope) copies it to the store the Worker reads ([serverless-tiers.md] §5.2: `{state, generation, manifest, loaded_at}`).

## 6. The pack writer

### 6.1 `fsb pack-slim GEN -o FILE`

The store → slim pack in one process: the loader (§ As built in [full-scan-serving-bench.md]), then the slim index, written without the full pack (saves the full pack's 237 s write and its 48 GB). The loader's peak is unchanged (110 GB for gcs), so gcs's writer runs on a 128 GB machine; cw likewise; the small deployments on anything.

### 6.2 `dt-cloud vm-pack build -o STORE [-w WORK] [--fsb PATH]`

1. Read `<STORE>/interval-store/CURRENT.json` and the generation's newest manifest (as `fetch-gen` / the site's `newestManifest` choose it).
2. Skip when `<STORE>/vm-pack/CURRENT.json` already names this generation and manifest (idempotent: a rerun or a later scan's catch-up does nothing twice).
3. Mirror what `fsb` reads (`scans.json`, the manifest, the base's and each listed run's `served/path.parquet`) into `WORK`, through `blobstore.Store` (gs://, s3://, r2://, a dir).
4. Run `fsb pack-slim`, then upload `vm-pack/<gen>/<manifest stem>.fsb` and, last, `vm-pack/CURRENT.json` = `{gen, manifest, key, bytes, sha256, scans, written}`.
5. Keep the previous pack (the VM may still serve it); `--keep N` (default 2) deletes older ones under `vm-pack/`.

`fsb` comes from `--fsb`, `$FSB`, or `PATH`; the job image gains it in a Rust build stage when a deployment turns the step on (§ 9).

### 6.3 The `vm-pack` scan step

A soft step, stage `serve`, after `interval-store` (`cloud/src/dt_cloud/scan_steps.py`): `dt-cloud vm-pack build -o <store>` with the interval store's own `store` option (or `R2_ARCHIVE`). Off unless a deployment's profile lists it. It never fails the scan: a failure leaves the VM on the previous pack, and the next scan's run catches up (`converges`). Options: `store`, `timeout`, `keep`. Memory: on a machine of the profile's `post` shape; gcs's (e2-highmem-16, 128 GB) covers its 110 GB loader peak.

## 7. The Worker side (adapter only)

### 7.1 `site/functions/_lib/vmAdapter.ts`

Pure functions, unit-tested, called by no route yet:
- `vmRequest(view)`: a parsed `/api/subtree` or `/api/diff` request (scan(s), path, `QueryAst`, owner scope, classes, staged regions, `w`, `h`, `minArea`, `atten`) → the `/v1/view` body. `share_log2 = ⌈log2(w · h / minArea)⌉`, `atten_log2 = log2(atten)` (an integer; a non-power-of-2 `atten` is refused).
- `subtreeBody(answer, ctx)`: the `/v1/view` answer → `/api/subtree`'s JSON (`tree` with the deployment's `ROOT_LABEL` at the root, `nodes`, `threshold`, `tier: "vm"`, `index: "vm:<gen>/<manifest>"`).
- `diffRows(answer)`: → `/api/diff`'s `rows` (`DiffRow`: path relative to the folder, depth, kind, status from the two sides, `expanded` where it has children) and its four totals.
- `signRequest` / `checkResponse`: § 4.3, over WebCrypto.

### 7.2 What the answer doesn't carry yet

The slim pack holds sizes, validity, names, owners and classes only. A VM answer has no `mtime`, `last_read`, per-node `owners` / `class_bytes` breakdown, `provenance`, or a search's `matches` / `matchCount` lists. When the site is wired, either the Worker hydrates them for the ≤ M drawn nodes (point reads in the path sort it already plans), or the pack gains the columns and the pass sums them ([full-scan-serving-bench.md] "Output columns beyond paths and sums"). Which, per field, is a wiring-phase decision.

## 8. Parity

### 8.1 The oracle

`bench/full-scan/oracle/oracle.py` answers every query by DuckDB SQL over the same generation's rows with no shared code; it gains the cell rule (`share_log2`, `atten_log2`, `floor_bytes`) as a `HAVING` over each node's depth. A difference fails the run.

### 8.2 Fixtures (CI-sized)

For all nine interval-store fixture generations, the exhaustive query mix (`fsb plan -x`, now over cell rules too) through `vmd`'s HTTP API at N = 1 and N = 4 logical shards, and N = 3 physical shard packs, every answer equal to the oracle's (`bench/full-scan/check-vmd`).

### 8.3 Real data

- **Oracle:** gcs's pack through `vmd`, the bench's 69 object-sum queries (their answers already oracle-checked) and a cell-rule mix checked against the oracle.
- **Worker:** a sample of real gcs requests (the probe's and warm-cache's page loads: the root, each bucket, drilled folders; plain, a month diff, an owner pool, a class scope) asked of the live Worker (`gcs.oa.dev`, the job's read grant) and of `vmd` at the same scan with `floor_bytes` = the Worker's own `threshold` and `atten_log2 = 1`: every node the Worker draws must exist in `vmd`'s tree with equal bytes and objects (`bench/full-scan/worker_parity.py`). The node sets differ only where the Worker's own policy differs (its tile cap, coarsening), which the script reports.
- **Shadow parity** in production ([serverless-tiers.md] §5.4 item 3) comes with the site wiring.

## 9. Labels and cost

- The serving VM (out of scope here) carries `app=disky`, `deployment=shared`, `component=vm-mode` (multi-tenant; per-tenant cost is split by pack bytes in the digest, later). A per-deployment VM carries that deployment.
- `vm-pack` runs inside the scan's own task, so its cost is the scan's (`component` of that task); if it moves to its own Batch job it goes through `batch.submit_job` with `component=vm-pack` and the bucket-region check, in the store's region.
- The bench VM used for phase 1's measurements carries `app=disky, deployment=gcs, component=full-scan-bench, purpose=bench`, and is stopped when idle.
- Standing cost: n2-highmem-16 on-demand ~$690/month at list price, spot a fraction (check current prices before the infra go); a 200 GB pd-balanced disk for packs (~$20/month) or local NVMe for the 4–10 s cold start ([full-scan-serving-bench.md] cold-start table).

## 10. Open questions (Ryan's)

1. **Attenuation.** Today's Worker halves the share per level below the folder's children (`atten = 2`). Decision 1 names a share of the total only. `vmd` supports both (`atten_log2` 0 or 1); recommendation: keep `atten_log2 = 1`, since without it a phone's 2^-15 draws nodes up to 15 levels deep and a pass's window walks most directories (§ 11 measures both).
2. **`(other)` visibility.** The Worker hides an `(other)` under its threshold; `vmd` always returns the exact rest (Ryan: "exact `(other)` per parent"). Recommendation: the rule draws it whenever nonzero; the renderer may decline to label a tiny one.

## 11. As built and measured

(Filled as phase 1 lands.)

[full-scan-serving-bench.md]: full-scan-serving-bench.md
[serverless-tiers.md]: serverless-tiers.md
[static-hex-runs]: done/static-hex-runs.md
