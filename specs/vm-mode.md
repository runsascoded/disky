# VM mode, phase 1: an exact in-memory query server beside the Worker

Status: phase 1 built, tested and measured on `w/vm-mode`, 2026-11-14 (§ 11); not wired to any site. Ryan's go: "#28: yes go" (2026-11-13), including a standing VM (one shared n2-highmem-16, ~$230–770/month, 24/7 for a few months, then re-evaluated). Builds on [full-scan-serving-bench.md] (the engine and its measurements) and [serverless-tiers.md] §5 (the hand-off contract), §6 (phasing) and §7 (Ryan's decisions), which bind this work. Nothing here changes a deployment: no live site wiring, no serving VM, no D1, secret or Pulumi apply.

## 0. Summary

- One server binary, `vmd`, holds each deployment's **slim pack** (the bench's slim index as a file, [full-scan-serving-bench.md] § Slim index) memory-mapped, and answers the site's views exactly: the plain map, a diff, owner / class / staged scopes, and search, under the request's scan pins. Measured on one n2-highmem-16 holding gcs's pack and a second gcs-sized one (§ 11.3): gcs's pack is 32.4 GB; at 3 levels the root's plain view takes 92 ms and a root search ~0.9 s; under a phone's cell rule (16 levels) 0.21 s and 1.2–2.1 s.
- **Tenants** are deployments. One process serves several, each with its own key, pack and pins; a tenant is added, replaced (a new pack after a scan) or dropped without a restart (§ 5).
- **The cell rule** is the one in [serverless-tiers.md] §7 item 1: a node is drawn when its bytes are at least `min_share` of the view's total, `min_share = 2^-share_log2` from the viewport, rounded down to a power of 2. The request carries it; the Worker and `vmd` apply the same integer test (§ 2).
- **Shards and merge are separate layers** (§ 3). A pack splits into N path-range shards, each answering on its own with partial rollups; the merge sums them by path and applies the global cell cut. One `vmd` runs every shard in-process; a fan-out over serverless functions (Cloud Run beside GCS, Lambda beside S3) would run one shard per function behind a merging caller. N = 1 and N > 1 give identical answers (tested).
- **The pack writer** is a scan step (`vm-pack`) after the interval store's append: it mirrors the current generation's newest manifest, writes the slim pack, and publishes it next to the store under `vm-pack/` with a pointer; a large deployment's step submits it as a Batch job of its own (a spot n2-highmem-16 by default) and doesn't wait (§ 6).
- **Parity:** the DuckDB oracle (`bench/full-scan/oracle/oracle.py`, no shared code) checks `vmd`'s answers on the repo's fixtures (14,976 answers through HTTP, every shard layout) and on gcs (134 / 134 against the bench's oracle-checked answers); a comparison with the live Worker on real gcs requests is written and waits for a read grant (§ 8, § 10 item 3).

## 1. Scope

In phase 1 (this branch):
1. `vmd`: the HTTP server, multi-tenant, signed requests and responses, shards + merge, the cell rule, the answer shape (§ 2–5).
2. The pack writer: `fsb pack-slim` (store → slim pack without the full pack), `dt-cloud vm-pack build` (mirror, write, publish), `dt-cloud vm-pack submit` (the writer as its own Batch job), the `vm-pack` scan step, and `fsb` in the job image behind a build arg (§ 6).
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
- **Owners are the store's attribution** (each object version's folded `us` slices, [interval-store.md] §2.2), as the bench's oracle defines them. The live ledger (D1 `owner_prefixes` / `actions`, which the Worker folds into owner pools and lenses at query time) isn't applied in phase 1: an owner-scoped answer is exact for the store's attribution, and differs from the Worker's wherever a live claim repaints a path. The wiring phase sends the claims inside D as regions (`owner_regions: [{prefix, owner}]`, newest action winning, as `ledger.ts` folds them), which the pass applies per row like staged prefixes.
- **Cell rule** `cell`: `share_log2`, `atten_log2`, `depth` (below).

### 2.2 The cell rule

For a folder at depth `dp`, a node at depth `d` (`r = d − dp ≥ 1`) with bytes `x` (in a diff, `max(x_from, x_to)`), in a view whose total is `T` (in a diff, `max(T_from, T_to)`), is **drawn** when it is nonempty, `r ≤ depth`, and

```
x · 2^share_log2  ≥  T · 2^(atten_log2 · (r − 1))
```

in exact integer arithmetic (u128). Every drawn node's parent is drawn (bytes only shrink down a path and the right side only grows), so the drawn set is a tree.
- `share_log2 = ⌈log2(viewport area / cell area)⌉`: `min_share` is cell area / viewport area rounded down to a power of 2 ([serverless-tiers.md] §7 item 1). A 390×844 phone at 8 px² cells: 16 (2^-15.3 rounded down); a 2560×1440 screen: 19; today's default canvas, 1280×768 at `minArea` 12: 17.
- `atten_log2`: the share halves per level below the folder's children, as today's Worker's does (`ATTEN_DEFAULT = 2`, i.e. `atten_log2 = 1`): the default everywhere (Ryan, 2026-11-14: § 10 item 1). `vmd` and the oracle take an absent `atten_log2` as 1 (`cell.rs` `ATTEN_LOG2`); the adapter always sends it, from the view's `atten` (default `ATTEN_DEFAULT`). `0` (the pure share) is still accepted, asked for by name.
- `depth`: the window, at most 64 levels below the folder. Default: `1 + ⌊share_log2 / atten_log2⌋` when `atten_log2 > 0` (no deeper node can pass: 17 levels for a phone's 2^-16), else 8.
- **`(other)`**: under every drawn node above the window's deepest level (and the folder), one `(other)` child holds the exact rest (its bytes and objects less its drawn children's, per side) exactly when it is nonzero (on either side of a diff), with `folded` = its nonempty children not drawn. It is returned whatever its size, never cut by the share; the renderer may decline to *label* a tiny one, never drop or resize it (Ryan, 2026-11-14: § 10 item 2). Signed per side: directory totals don't enter, so on object sums a rest is never negative.
- Legacy forms, for the bench and the oracle only: `min_ppm` (`x · 10^6 ≥ min_ppm · T`) and `floor_bytes` (`x ≥ floor_bytes · 2^(atten_log2 · (r − 1))`, an absolute threshold, used to compare with today's Worker, whose threshold is `T · minArea / (w · h)`, § 8.3).

## 3. Shards and merge

Two layers with a serialized boundary between them, so the same code runs them in one process or across machines.

### 3.1 A shard

A **shard** is a contiguous range of a pack's object rows `[lo, hi)` (rows are in depth-first path order, so a range is a path range). It answers a query by the slim pass over the folder's rows inside its range, and returns a **partial answer** (`ShardAnswer`, `src/shard.rs`):
- `total`: its bytes and objects per side;
- `nodes`: the window nodes it can't rule out, each `{path, depth, file, matched, partial, a, b, kids}`, sorted by path (segment order). `partial` marks a node whose rows cross the shard's edge (always sent); `kids` counts its nonempty children wholly inside the shard. A directory whose every version's bytes together (`dever`, § 11.4) can't reach its depth's threshold is summed flat, so nothing under it is sent;
- `root_kids`: the folder's nonempty children wholly inside the shard.

What a shard may leave out depends on its `floor`:
- `local` (one round): a node wholly inside the shard is dropped when it fails the cell rule against the shard's **own** total. The global total is at least the shard's, so this keeps a superset of what the merge draws. Partial nodes are always kept.
- `totals`, then `{"total": T}` (two rounds): the first returns the shard's totals alone; the caller sums them and asks again with the global total, and the shard drops what fails the rule against it, sending the fewest nodes.
- `all`: every node the pass emitted (tests).

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
- Each function holds one physical shard pack (`fsb shard`; gcs's 30.3 GB at N = 8 is ~3.8 GB per shard, plus its sub-dictionary), mapped from local disk or read into memory at cold start, and runs `vmd` with that pack as its one tenant: `POST /v1/shard` (`shard: 0`) is its partial answer.
- A merging caller sends the request to every shard with `floor: local` (one round, more nodes back) or `floor: totals` then `floor: {total: T}` (two rounds, fewer nodes), then merges them: `POST /v1/merge` on any `vmd` (or `src/merge.rs` linked in), the same code `/v1/view` runs in-process. `check-vmd` runs exactly this across HTTP (7 physical shards, both round counts) and requires the in-process view's answer.
- Cost and latency of that deployment (cold starts per shard, per-request price, the merge's bytes) are measured when it's proposed; nothing here assumes it.

## 4. The server's API

### 4.1 Endpoints

| Method, path | Who | What |
|---|---|---|
| `POST /v1/view` | the Worker | one view or diff, answered whole (shards merged) |
| `POST /v1/shard` | a merging caller | one shard's partial answer (`shard`: its index; `floor`) |
| `POST /v1/merge` | a merging caller | shards' partial answers (`answers`, from any machines) merged and cut: the view's answer (no pack needed) |
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
- `504 deadline`: the answer took longer than `deadline_ms`; nothing partial is returned ([serverless-tiers.md] §3.4). Phase 1 checks after the pass (a pass isn't interrupted).

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
- **Hot swap:** a watcher re-reads the config and stats each tenant's pack file(s) every `watch_s` (default 5 s). A tenant whose pack file changed (length, mtime: a new pack is written beside the old and renamed over it, or the config names a new file) is mapped anew (header + columns, ~1 ms) and prefaulted on a background thread; its state is `loading` until then, while the old pack keeps answering requests pinned to it. The swap is an `Arc` exchange; the old map is dropped when its last request finishes. Added tenants load; removed tenants stop answering (`404`). A deployment moves onto or off the shared VM by editing the config: no restart, no other tenant disturbed.
- **Memory:** gcs 32.4 GB (slim pack + `dever`, 138 scans, 720M object versions). cw's is about 4–5 GB, estimated, not measured: its bucket `marin-us-east-02a` holds 92.8M objects / 960 TB ([cw adaptive-listing]) and its store 93.2M coalesced versions ([cw-static-names]), roughly 1/8 of gcs's versions (its longer paths, mean 172 characters to gcs's 119, cost a little more per version); cw has no owner axis (every `usr` empty), and does have staging. hf / r2 / the r2 demo / m3 each well under 1 GB. Two gcs-sized tenants and the small ones: 69 GB of pages measured (§ 11.3), a worst case for a second large deployment; an n2-highmem-16 (128 GB) holds every tenant's pages hot with room for page-cache churn, and the slim pack's measured 32 GB-per-tenant working set means even two gcs-sized tenants' searches stay resident (§ 11). A process or container per deployment on the same VM is equally supported (one tenant per config); the shared process is the default because one thread pool uses the cores better than two pools fighting.
- **Status object:** `vmd` writes `GET /v1/status`'s content per tenant; the serving deployment's publisher (out of scope) copies it to the store the Worker reads ([serverless-tiers.md] §5.2: `{state, generation, manifest, loaded_at}`).

## 6. The pack writer

### 6.1 `fsb pack-slim GEN -o FILE`

The store → slim pack in one process: the loader (§ As built in [full-scan-serving-bench.md]), then the slim index, written without the full pack (saves the full pack's 237 s write and its 48 GB). The loader's peak is unchanged (117 GB for gcs, § 11.3), so gcs's writer runs on a 128 GB machine; cw's, at ~1/8 of gcs's versions, should peak near 15 GB (an estimate: not run); the small deployments on anything.

### 6.2 `dt-cloud vm-pack build -o STORE [-w WORK] [--fsb PATH]`

1. Read `<STORE>/interval-store/CURRENT.json` and the generation's newest manifest (as `fetch-gen` / the site's `newestManifest` choose it).
2. Skip when `<STORE>/vm-pack/CURRENT.json` already names this generation and manifest (idempotent: a rerun or a later scan's catch-up does nothing twice).
3. Mirror what `fsb` reads (`scans.json`, the manifest, the base's and each listed run's `served/path.parquet`) into `WORK`, through `blobstore.Store` (gs://, s3://, r2://, a dir).
4. Run `fsb pack-slim`, then upload `vm-pack/<gen>/<manifest stem>.fsb` and, last, `vm-pack/CURRENT.json` = `{gen, manifest, key, bytes, sha256, scans, written}`. Before the pointer, the store's newest is read again: when another manifest (or generation) landed while this one was packed (two scans' jobs overlapping), the pack stays written but the pointer and pruning are left to the newer job (`"superseded": true`), so a late job never moves the pointer back. (A read-then-write, not a compare-and-swap: two jobs finishing within the same second could still race; the next scan's job repairs it.)
5. Keep the previous pack (the VM may still serve it); `--keep N` (default 2) deletes older ones under `vm-pack/`.

`fsb` comes from `--fsb`, `$FSB`, or `PATH`; the job image carries it when built with `FSB=build` (§ 6.4).

### 6.3 The `vm-pack` scan step and its Batch job

A soft step after `interval-store` (`cloud/src/dt_cloud/scan_steps.py`), over `options.vm-pack.store`, else the interval store's own `store` option, else `R2_ARCHIVE`. Off unless a deployment's profile lists it (`steps`). It never fails the scan: a failure leaves the VM on the previous pack, and the next scan's run catches up (`converges`). Options: `store`, `keep`, `batch`.

**With `batch` (Ryan, 2026-11-14, "sg go": § 10 item 4)**, the step submits the writer as a Batch job of its own and returns: `dt-cloud vm-pack submit -o <store> -k <keep> …` (`vm_pack.submit`):
- Nothing is submitted when `vm-pack/CURRENT.json` already names the store's current generation and newest manifest.
- One job per pack: its id is `vm-pack-<deployment>-<gen>-<manifest stem>` (`job_id`: lowercased to Batch's rule; past 63 characters cut, with 8 hex of the whole id's sha256), so a resubmit for the same pack (a resumed run, a retried step) meets the job already there: a 409, counted as submitted (`exists_ok`). Rate limits and 5xx are retried for 5 minutes (`retry_for`).
- Through `batch.submit_job`: cost-labelled `app` / `deployment` from `$DISKY_LABELS`, `component=vm-pack`, and the scan's `scan_id` / `scan_run` (`label_batch_spec`); a `gs://` store's bucket region is checked against the job's (`check_region`) unless `cross_region`.
- The job: `bash -c 'dt-cloud image-sha >&2; exec dt-cloud vm-pack build -o <store> -w /stage/vm-pack -k <keep>'` in the image, one task, default a **spot n2-highmem-16** (`machine`, `spot`), 7,700 MiB per vCPU for a highmem machine (3,700 standard, 900 highcpu; another family names `memory_mib`), a 375 GB local SSD for the work dir (`ssd_gb`; 0: the 100 GB boot disk, `/tmp/vm-pack`), 2 retries when spot (a preemption; a rerun after the pack is published is a no-op), 4 h cap.
- **Not waited on.** The step's outcome is the submit's, never the job's: a failed job leaves the VM on the previous pack, and the next scan's job writes the newest (the job builds the store's newest manifest when it starts, not the one it was submitted for, and leaves the pointer alone when a newer one landed meanwhile: § 6.2 step 4). A failed job's id is taken until Batch deletes it, so that same pack isn't retried automatically; the next scan's is a new id. By hand: `dt-cloud vm-pack submit` again after `gcloud batch jobs delete <id>`, or `vm-pack build` anywhere with the memory.
- `batch`'s keys (`vm_pack.BATCH_KEYS`, checked when the profile is read) are `submit`'s options: `image` (default `$JOB_IMAGE`, the scan task's own: then `fsb` must be in it, and the step checks), `service_account` (`$JOB_SA`), `region` (`$BATCH_REGION`; put it beside the store), `project` (`$GCP_PROJECT`), `machine`, `spot`, `ssd_gb`, `memory_mib`, `env` (`{VAR: value}`), `secrets` (`{VAR: secret name}` from the project's Secret Manager, or a `projects/…` path), `cross_region`. A store outside GCS needs its credentials and endpoint in `env` / `secrets`; nothing is copied from the scan task's environment but `$DISKY_LABELS` / `$SCAN_ID` / `$SCAN_RUN_ID`.

**Without `batch`**, the step builds in its own task (`dt-cloud vm-pack build -w <work>/vm-pack`): for a small deployment, whose pack takes little memory and which may have no Batch (the r2 demo's GHA ingest). Needs `fsb` in the task's image; skipped (said in the note) without it.

An example profile fragment (generic: each deployment names its own region, store and machine):

```yaml
steps: [vm-pack]
options:
  vm-pack:
    keep: 2
    batch:
      region: <the store bucket's region>
      # machine: n2-highmem-16   # the default; cw's ~4–5 GB pack would fit an n2-highmem-4
      # env / secrets: a non-GCS store's endpoint and credentials
```

### 6.4 `fsb` in the job image

`job/Dockerfile`'s build arg `FSB` (Ryan, 2026-11-14, "behind an arg sg": § 10 item 6): `build` adds a stage on `rust:1.91-slim-bookworm` that compiles `bench/full-scan` (`cargo build --release --locked --bin fsb`, debug info off) and copies `fsb` to `/usr/local/bin/fsb`; `none` (the default) leaves it out, so no deployment's image changes until it opts in. A deployment turns it on in its own `job/build.json`:

```json
{"build_args": {"FSB": "build", "…": "…"},
 "context": ["…", "bench/full-scan/Cargo.toml", "bench/full-scan/Cargo.lock", "bench/full-scan/src"]}
```

(`context` only when the config narrows it; the default `.` holds them.) The bookworm build stage keeps the binary's glibc floor at 2.36, below the final `python:3.12-slim` image's.

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

For all nine interval-store fixture generations, the exhaustive query mix with cell-rule twins (`fsb plan -c`) through `vmd`'s HTTP API at N = 1 and N = 4 logical shards and N = 3 and 7 physical shard packs, every answer equal across layouts and to the oracle's; every 5th also merged across HTTP from the 7 shards' `/v1/shard` answers (`bench/full-scan/check-vmd`). Not yet in CI: it needs Rust and DuckDB on a runner (a job like the site's, ~10 minutes); today it runs on a bench node.

### 8.3 Real data

- **Oracle:** gcs's pack through `vmd`, the bench's 69 object-sum queries (their answers already oracle-checked); the cell-rule mix with and without the pruning bound (the bound's own check).
- **Worker:** a sample of real gcs requests (the probe's and warm-cache's page loads: the root, each bucket, drilled folders; plain, a month diff, an owner pool, a class scope) asked of the live Worker (`gcs.oa.dev`, the job's read grant) and of `vmd` at the same scan with `floor_bytes` = the Worker's own `threshold` and `atten_log2 = 1`: every node the Worker draws must exist in `vmd`'s tree with equal bytes and objects (`bench/full-scan/worker_parity.py`). The node sets differ only where the Worker's own policy differs (its tile cap, coarsening), which the script reports.
- **Shadow parity** in production ([serverless-tiers.md] §5.4 item 3) comes with the site wiring.

## 9. Labels and cost

- The serving VM (out of scope here) carries `app=disky`, `deployment=shared`, `component=vm-mode` (multi-tenant; per-tenant cost is split by pack bytes in the digest, later). A per-deployment VM carries that deployment.
- `vm-pack` with `options.vm-pack.batch` is its own Batch job (§ 6.3), through `batch.submit_job`: `component=vm-pack` beside the deployment's `app` / `deployment` and the scan's `scan_id` / `scan_run`, on the job and its allocation policy, with the bucket-region check, in the store's region. Without `batch` it runs inside the scan's task and its cost is the scan's.
- The bench VM used for phase 1's measurements carries `app=disky, deployment=gcs, component=full-scan-bench, purpose=bench`, and is stopped when idle.
- Standing cost: n2-highmem-16 on-demand ~$690/month at list price, spot a fraction (check current prices before the infra go); a 200 GB pd-balanced disk for packs (~$20/month) or local NVMe for the 4–10 s cold start ([full-scan-serving-bench.md] cold-start table).

## 10. Open questions (Ryan's)

1. **Attenuation. Decided (Ryan, 2026-11-14: "ok sg"): keep halving, `atten_log2 = 1`.** Today's Worker halves the share per level below the folder's children (`atten = 2`); decision 1 named a share of the total only. Without halving nothing bounds the window but a cap, and the pure share draws more, smaller cells for the same screen: measured at the gcs root, a phone's rule with halving (16 levels) 0.21 s and 1,519 cells, the pure share capped at 8 levels 0.41 s and 12,079 cells (§ 11.3). Done: the default in `vmd` (`cell.rs` `ATTEN_LOG2`, an absent `atten_log2`), the oracle, and the adapter (`atten` defaults to `ATTEN_DEFAULT`, always sent); `atten_log2: 0` stays available by name.
2. **`(other)` visibility. Decided (Ryan, 2026-11-14: "sg"): always returned, exactly when nonzero; the renderer may skip labelling tiny ones.** The Worker hides an `(other)` under its threshold; `vmd` returns the exact rest (Ryan: "exact `(other)` per parent") whatever its size (§ 2.2), and the adapter passes it through. Whether to label a tiny one is the renderer's, in the wiring phase. Summary statistics beside it (e.g. how much of the view is in `(other)`s) are under discussion, not built.
3. **A read grant for real-data Worker parity.** `worker_parity.py` needs to call `gcs.oa.dev` from the bench VM. Options: let it read the scan job's grant (`gcs-snapshot-site-token`, read-only) from Secret Manager there, or mint a personal agent token (`/api/token`) for it. Recommendation: a dedicated read grant for VM-mode checks (it becomes the shadow-parity job's later).
4. **The pack writer's memory. Decided (Ryan, 2026-11-14: "sg go"): `vm-pack` runs as its own Batch job on an n2-highmem-16 (spot, in the store's region, `component=vm-pack`), not inside `post`.** `fsb pack-slim` peaks at 117 GB on gcs (the loader holds every version row once, as the bench's did); gcs's `post` machine (e2-highmem-16, 128 GB) would have fit it with ~10 GB to spare. cw's, at ~1/8 of gcs's versions, should need ~15 GB (estimated). Done (§ 6.3): `options.vm-pack.batch` makes the step submit the job (`dt-cloud vm-pack submit`, one id per pack, labels from `$DISKY_LABELS`, the region check) and return without waiting; nothing fails the scan. Opt-in per deployment; no deployment's profile is changed here. A later lever: the loader streaming levels to disk, which would let the writer back onto a smaller machine.
5. **cw's pack on a GCP box.** cw's store is on CoreWeave object storage; a shared VM in GCP would pull cw's pack (~4–5 GB, estimated from its 93.2M coalesced versions, [cw-static-names]) per cw scan across clouds (egress on CoreWeave's side, ingress free), or cw's `vm-pack` job writes it there and the VM pulls it. A cw `vm-pack` job on GCP Batch would itself read cw's `path` sorts across clouds; cw's own store being smaller, the transfer is smaller than first estimated. Alternatives: a second, CoreWeave-side `vmd` for cw (same binary, its own tenant config), or a shared box placed where the larger daily transfer is cheaper. Not decided here; the multi-tenant design works either way.
6. **`fsb` in the job image. Decided (Ryan, 2026-11-14: "behind an arg sg").** Done (§ 6.4): `job/Dockerfile`'s `FSB` build arg, `none` by default; a deployment sets `"FSB": "build"` in its own `job/build.json`'s `build_args` (gcs and cw in their own sessions). No deployment's build config is changed here.
7. **Where the crate lives.** `bench/full-scan/` is now production code (the pack writer in the job image, the server `vmd`) beside its bench drivers and results. Proposal: when the site is wired (the server's first live use), move the crate to `vm/` (or `engine/`) at the repo root, keeping `src/`, `tests/`, `oracle/` and the `check-*` drivers there, and leave `bench/full-scan/` the bench's (its `run-*` drivers, `results/`, the summaries, `batch/`), pointing at the crate by path. Not moved now: it would touch the Dockerfile's `COPY`s, every driver's `cargo build` and the specs' paths in one go, for no behaviour change while nothing serves from it; a deployment turning `FSB` on names the paths in its `context` once (§ 6.4), and the move would rename them.

## 11. As built and measured

### 11.1 Where the code is

The engine and the server are `bench/full-scan/` (the crate graduates to its own directory when the site is wired):
- `src/cell.rs` the cell rule; `src/shard.rs` / `src/merge.rs` the two layers; `src/slim_scan.rs` the pass (`shard_pass`, `run_sharded`); `src/slim.rs` the slim index (+ `slice`: physical shards; `dever`: § 11.4); `src/serve.rs` + `src/bin/vmd.rs` the server.
- `fsb pack-slim`, `fsb shard`, `fsb plan -c` (the exhaustive mix plus cell-rule twins).
- `check-vmd` (+ `oracle/vmd_check.py`), `run-vmd` + `vmd_bench.py` + `vmd_summary.py` (the measurements), `vmd_client.py` (the signed client), `worker_parity.py` (§ 11.3).
- `cloud/src/dt_cloud/vm_pack.py` (`dt-cloud vm-pack build|submit|current`: `job_id`, `job_spec`, `submit`), the `vm-pack` step (`scan_steps.py`, `options.vm-pack.batch`), tests `cloud/tests/test_vm_pack.py` + `test_scan_steps.py`.
- `job/Dockerfile`'s `FSB` build arg (the `fsb-build` stage).
- `site/functions/_lib/vmAdapter.ts` (+ `.test.ts`).

### 11.2 Parity

- **Fixtures, through HTTP** (`check-vmd`, the nine interval-store generations): every query of `fsb plan -c` (the exhaustive mix and its cell-rule twins, legacy `min_ppm` included; directory-total views excluded, regexes refused with `422` as the language requires: 864 of them) asked of four tenants serving the same store: one pack, 4 logical shards, 3 and 7 physical shard packs. **14,976 answers, every tenant's equal, and every one equal to the DuckDB oracle's.** Every 5th (2,999) was also answered by the 7 physical shards one by one (`/v1/shard`) and merged by `/v1/merge`, in one round (`floor: local`) and in two (`totals`, then `{total}`): equal to the in-process answer each time.
- **The refactored pass** (`check-slim`: the slim pack's answers against the full index's, at 1 and 4 threads and with 1-row chunks): 0 different on every fixture, with the pruning bound and the fast object path on.
- **gcs, through HTTP:** the bench's 69 object-sum queries (67 answerable; its two regexes refused) on the gcs pack and on its stand-in, against the bench's oracle-checked answers (the bench's final-code rerun, `results/2026-11-12.jsonl`'s queries; the pack holds 138 scans, those answers' scans among them): **134 / 134 equal**; at 8 logical shards 67 / 67; with both tenants queried at once 134 / 134. The synthetic x1800 tenant's 69 answerable queries: equal to its oracle-checked answers.
- **Pruned against walked:** the cell-rule mix on gcs with the `dever` bound and without (`FSB_NO_PRUNE`): 51 / 51 equal; the deep-window searches against the first build too: 12 / 12.
- **Today's Worker, fixtures:** not a usable comparison. `expected.json` (what `intervalStore.test.ts` requires of `buildView` over fixture `g1`) draws each directory from its own rolled-up row and paths only directory rows hold, while `vmd` sums object versions; the fixture's directory rows are built not to add up (`gen.py`), so 44 of its 125 object tiles and none of its 114 directory tiles have a `vmd` counterpart with the same sums. On gcs the two agree (the bench's `dirs_vs_files.py`: 27 / 27 views equal), which is what the real-data check below is for.
- **Today's Worker, real gcs requests:** not run. `worker_parity.py` is written (the probe's page loads on the root and buckets, plain, `o=owned|unowned`, `cl=n`, two searches, a month diff; every node the Worker draws must be in vmd's answer with equal sums), but it needs a gcs read grant on the bench VM, and using the scan job's grant (`gcs-snapshot-site-token`) from there was refused by this session's permission check. § 10 item 3.


### 11.3 Measured (bench VM, 2026-11-14)

`disky-full-scan-bench`: n2-highmem-16 (8 cores × 2 threads, 125 GB), us-east1-c, spot, a 200 GB pd-balanced boot disk (196 MiB/s). gcs's store: generation `2026-10-09b.g3.c001` with the `2026-11-13T1734.m001` manifest (138 scans), read in region. Every latency is a client's wall time over HTTP to `vmd` on the same VM (signing and JSON included), 16 threads. Drivers: `run-vmd`, `vmd_bench.py`; raw lines `bench/full-scan/results/2026-11-14-vmd/` (the query files stay on the bench node: they name gcs's paths); tables from `vmd_summary.py` (`summary.md` there). The three-tenant cold start's 312 s is from its run log (its JSONL stops before the line).

**Tenants.** "Two deployments" is gcs's pack plus a second gcs-sized pack (a copy of gcs's): a worst case for a second large deployment, not cw's size. cw's store holds ~93.2M coalesced versions ([cw-static-names]; its bucket 92.8M objects, [cw adaptive-listing]), about 1/8 of gcs's, so its pack should be ~4–5 GB, near the x1800 tenant's; it isn't readable from GCP without egress, so it wasn't loaded. Plus the synthetic x1800 fleet (113M object versions, 3.8 GB pack) as a small third. All three mapped in one `vmd`.

**The pack writer** (`fsb pack-slim`, the store → the slim pack): 553 s (load 363 s, build 24 s, write 160 s to pd-balanced); peak RSS 117 GB (the loader's, as the bench's); the pack 32.37 GB (30.41 GB of columns + 1.95 GB of `dever`), 720,399,014 object versions, 244M directories, 172M names. x1800: 50 s, 3.8 GB.

**Memory.** Mapped, `vmd` holds no anonymous memory to speak of (0.3–0.8 GB after a mix: answers and buffers); its RSS is the packs' pages: 68.6 GB with both gcs-sized packs and x1800 resident (prefaulted). The VM's 125 GB leaves ~55 GB of page cache headroom; a third gcs-sized tenant would not fit hot.

**Cold start** (caches dropped, `vmd` started, every tenant prefaulted from pd-balanced, then the root's plain view):

| Tenants | Packs | Until every tenant is hot | Then the root's first answer |
|---|---:|---:|---:|
| gcs | 30.4 GB | 148 s | 94 ms |
| gcs + stand-in + x1800 | 64.6 GB | 312 s | 109 / 106 ms |

(The rate is the disk's: 196 MiB/s. On local NVMe the bench measured 10.5 s per slim pack; on a 1 TB pd-ssd 40 s.)

**gcs's object-sum mix, both large tenants loaded**, one query at a time (`two-objects-v2`: the bench's 67 answerable queries, `min_ppm` 500, 3 levels; warm median of 5 (cold) ms; every answer equal to the bench's oracle-checked one):

| Query | gcs | stand-in | bench, in-process (2026-11-13, slim, 16 vCPU, on the NVMe twin) | Rows summed | Nodes |
|---|---:|---:|---:|---:|---:|
| `root` | 91.9 (95.8) | 93.6 (106) | 107 | 720,399,014 | 290 |
| `root-depth6` | 128 (129) | 135 (133) | 404 | 720,399,014 | 939 |
| `root-month` | 91.9 (92.9) | 94.8 (93.6) | 104 | 720,399,014 | 401 |
| `bucket0` | 46.9 (49.5) | 47.6 (49.8) | 55.7 | 338,179,283 | 372 |
| `bucket3` | 19.7 (18.4) | 31.5 (25.1) | 60.4 | 83,287,054 | 353 |
| `d3-0` | 23.6 (22.6) | 22.7 (22.8) | 23.1 | 142,013,740 | 286 |
| `d6-0` | 64.3 (72.1) | 62.5 (59.6) | 48.6 | 4,184,357 | 9 |
| `diff-day-root` | 113 (114) | 114 (116) | 127 | 720,399,014 | 287 |
| `diff-month-root` | 115 (115) | 115 (115) | 116 | 720,399,014 | 425 |
| `rare0` | 918 (947) | 917 (950) | 1,018 | 228,816 matched | 3 |
| `common-zarr.json` | 948 (908) | 936 (939) | 1,081 | 71,044,981 matched | 28 |
| `and` | 1,079 (1,065) | 1,089 (1,060) | 1,245 | 230,476 matched | 14 |
| `anchor-exact` | 936 (947) | 937 (947) | 1,059 | 71,045,101 matched | 28 |
| `path-term` | 1,740 (1,746) | 1,742 (1,732) | 1,771 | 2,022,280 matched | 13 |
| `search-diff-month` | 929 (904) | 928 (937) | 1,111 | 71,045,695 matched | 44 |
| `owner-one` | 167 (167) | 167 (167) | 251 | 720,399,014 | 30 |
| `unowned` | 169 (171) | 169 (170) | 252 | 720,399,014 | 255 |
| `class-one` | 167 (167) | 168 (167) | 158 | 720,399,014 | 216 |
| `class-owner` | 246 (246) | 246 (246) | 356 | 720,399,014 | 16 |
| `staged` | 2.8 (4.1) | 2.9 (4.5) | 3.5 | 10,339 | 27 |
| `staged-search` | 110 (113) | 113 (116) | 1,261 | 255 matched | 0 |

Over all 67: gcs p50 113 ms, p90 1,048 ms, max 1,740 ms; the stand-in p50 114, p90 1,032, max 1,742. HTTP, JSON and signing add under 1 ms; the gains over the bench's in-process numbers are the pruning bound (`root-depth6`: 404 → 128 ms; a staged search 1.26 s → 0.11 s, which now walks only the staged rows' directories).

**The cell rule's windows** (`two-cells`): the site's kinds of query under a phone's rule (share 2^-15, halving per level: 16 levels), a desktop's (2^-19, 20 levels), and a pure share (2^-15, no attenuation, 8 levels). Warm median ms on gcs (the stand-in within noise), `v1` the first build (26bf4f69d), `now` with the pruning bound (`dever`, § 11.4), the fast object path and the staged skip; both builds' answers equal (51 / 51 on gcs with the bound against without it, `FSB_NO_PRUNE`):

| Query | phone (16 levels): v1 → now | desktop (20): v1 → now | pure share (8): v1 → now |
|---|---:|---:|---:|
| `root` | 1,631 → 222 | 1,804 → 493 | 980 → 449 |
| `bucket0` | 481 → 147 | 624 → 315 | 437 → 235 |
| `bucket3` | 224 → 125 | 372 → 264 | 329 → 237 |
| `d3-0` | 831 → 275 | 854 → 299 | 831 → 286 |
| `d6-0` | 148 → 139 | 252 → 243 | 147 → 141 |
| `root-month` | 1,605 → 250 | 1,805 → 477 | 992 → 427 |
| `diff-day-root` | 1,880 → 313 | 2,005 → 595 | 1,158 → 552 |
| `diff-month-root` | 1,852 → 290 | 2,077 → 583 | 1,177 → 548 |
| `rare0` | 1,720 → 2,806 | 1,772 → 2,881 | 1,399 → 1,612 |
| `common-ckpt` | 1,734 → 1,347 | 1,756 → 1,489 | 1,470 → 1,397 |
| `and` | 1,931 → 2,967 | 1,909 → 3,030 | 1,586 → 1,771 |
| `anchor-exact` | 1,977 → 1,445 | 1,967 → 1,705 | 1,512 → 1,627 |
| `search-diff-month` | 1,873 → 1,455 | 1,855 → 1,738 | 1,537 → 1,639 |
| `owner-one` | 1,400 → 505 | 1,556 → 774 | 1,257 → 971 |
| `unowned` | 1,941 → 474 | 2,028 → 736 | 1,185 → 694 |
| `class-one` | 1,415 → 410 | 1,514 → 603 | 946 → 629 |
| `staged` | 1,383 → 5 | 1,382 → 10 | 763 → 22 |

The searches in that table ran on an intermediate build; the final one, warm median of 3, gcs alone (the deep-window searches and two plain/scoped references), all three builds' answers equal (12 / 12):

| Query | v1 | final, bound on | final, `FSB_NO_PRUNE` |
|---|---:|---:|---:|
| `root@phone` | 1,632 | 209 | 1,422 |
| `root@flat8` | 978 | 408 | 802 |
| `owner-one@phone` | 1,386 | 495 | 1,572 |
| `owner-one@flat8` | 1,248 | 954 | 1,300 |
| `rare0@phone` | 1,700 | 1,940 | 1,837 |
| `rare0@flat8` | 1,415 | 1,424 | 1,350 |
| `and@phone` | 1,896 | 2,120 | 2,000 |
| `and@flat8` | 1,602 | 1,550 | 1,499 |
| `common-ckpt@phone` | 1,685 | 1,245 | 1,830 |
| `common-ckpt@flat8` | 1,452 | 1,255 | 1,372 |
| `search-diff-month@phone` | 1,858 | 1,325 | 2,040 |
| `search-diff-month@flat8` | 1,452 | 1,365 | 1,431 |

**Both large tenants at once** (`pair`: the object-sum mix on gcs and on the stand-in from two clients at the same time): p50 227 ms, p90 1.90 s, max 3.30 s on gcs (alone: 113 ms / 1.05 s / 1.74 s), the stand-in alike; the root's plain view 179 ms (92 alone), a rare term 1.68 s (0.92), an owner scope 332 ms (167). Two passes share the 16 vCPUs, so each takes about twice as long; every answer still equal to the oracle-checked one (67 + 67).

**8 logical shards** (one pack, `shards: 8`, run one after another in-process): the root's plain view 101 ms (92 as one shard), a rare term 879 ms (918), an owner scope 171 ms (167), a staged scope 4.4 ms (2.8); all 67 answers equal to the oracle-checked ones. Splitting into shards costs little: each shard's pass is the same chunks, and the merge handles a few hundred nodes.

**What it shows:**
- **Two gcs-sized tenants on one n2-highmem-16 serve at single-tenant speed** when queried one at a time: gcs and its stand-in are within noise of each other and of the bench's in-process numbers. The packs' 69 GB of pages fit the 125 GB box with room to spare, and `vmd` itself holds under 1 GB.
- **Against the targets on the gcs root** (p50 ≤ 0.5 s, max ≤ 2 s, [full-scan-serving-bench.md] "Cost model"): the object-sum mix meets both (p50 113 ms, max 1.74 s). Under the cell rule, plain views, diffs and owner / class / staged scopes stay at or under ~0.6 s at the root with a phone's or a desktop's window; searches take 1.2–2.1 s there, and a path term 1.74 s even at 3 levels.
- **The cell rule's deep windows cost what the walk costs.** A phone's rule draws nodes up to 16 levels down and a desktop's 20, so every directory in the window is walked unless something rules it out. The `dever` bound rules out a directory whose every version's bytes together can't reach its depth's threshold: plain views 4–8× faster at the root (1.63 → 0.21 s), scopes 2–3×, staged scopes 100× (only staged rows' directories are walked). It can't help a rare search much: the threshold is a share of the *matched* total, which is small, so few directories fall under it; there the final build is ~12% slower than the first (1.94 vs 1.70 s at a phone's window), common terms 26–29% faster.
- **Concurrency shares the cores**: two large tenants queried at once each run at about half speed (p50 227 ms, max 3.3 s). The box serves one heavy query at a time at full speed; queueing (or a second box) is the lever when demand overlaps.
- **Cold start is the disk's**: 148 s per 30 GB pack from 200 GB pd-balanced, 312 s for both large packs and the small one; local NVMe brings a pack to ~10 s ([full-scan-serving-bench.md]). The first answer after that is the in-RAM time.
- **The writer needs a 128 GB machine for gcs** (117 GB peak, 9 minutes).

### 11.4 The pruning bound (`dever`)

The slim index gains one column, `dever` (per directory, u64: the sum of `size` over every object version under it, whatever its scan; gcs 1.95 GB). It is an upper bound on the directory's bytes at any scan, under any owner, class, staged or search filter, so a directory with `dever < thr(depth)` can't be drawn, nor can anything under it (a node never outweighs its parent); its rows are summed flat (or, under an undecided search, by the below-window loop, which still decides each path's match). `thr(depth)` is `Cut::min_bytes` against the running total, a lower bound on the view's, so the bound only ever keeps more than it needs; the merge's cut is exact. Packs written before it have no `dever` and aren't pruned. `FSB_NO_PRUNE` turns it off, which is how the answers were compared (51 / 51 on gcs's cell mix, 12 / 12 on the deep searches, and every fixture through `check-vmd`).

Tighter bounds would prune more, at more pack bytes: the maximum over scans of the directory's live bytes (exact for plain views at any scan), or a per-directory name filter for searches ([full-scan-serving-bench.md] "Open": renumbering names by folder). Not built.

[full-scan-serving-bench.md]: full-scan-serving-bench.md
[serverless-tiers.md]: serverless-tiers.md
[static-hex-runs]: done/static-hex-runs.md
[interval-store.md]: interval-store.md
[cw adaptive-listing]: https://github.com/Open-Athena/disky/blob/cw/specs/done/adaptive-listing.md
[cw-static-names]: https://github.com/Open-Athena/disky/blob/cw/specs/cw-static-names.md
