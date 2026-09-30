# disk-tree as the shared base for the cloud-usage viewers

Status: **in progress** (2026-09-20). Union view + single-cloud collapse landed (`786b5bb`). Phase-1 hoist of raw `m/gcs` (`76a07ad`) was the **wrong source** — superseded below.

**Direction (decided 2026-09-20):** disk-tree `cloud` is the canonical base for every cloud-storage-usage viewer. mgu already has a mature convergence in flight (`m/cw-s3:specs/convergence.md` — "one codebase, deployments as configuration"), whose own endgame is to upstream the union to disk-tree. So: **mgu finishes its convergence; disk-tree adopts the union tip (`m/cw-s3`) as the base and builds the r2/public layer convergence.md doesn't cover; then adopts mgu's converged `main`.** One convergence, not two.

## Goal

disk-tree `cloud` is the base for:
- **r2.rbw.sh** — public R2 bucket viewer (ctbk, crashes, jc-taxes). **anonymous / no auth.**
- **gcs.oa.dev / cw-s3.oa.dev** (mgu) — cloud-usage maps, auth-gated (owner/marks on gcs).
- future **rac/oa clouds** — other personal/OA blob stores, public or gated.
- (later) a **multi-cloud/local superset** — disk-tree's `ui/` scan-manager (own domain, TBD).

Each deploy differs only in **config** (a `Store` row + wrangler vars/secrets + D1), never in forked view code.

## The key facts (established 2026-09-20 by two branch investigations + `convergence.md`)

**mgu is a fork of disk-tree** (merge-base `109ab11`, 2026-08-19). It has two live deploy branches and an in-flight unification:

- **`m/denovo-land`** (2026-09-16) = the **union** of gcs+cw: every feature of both deploys present, gated so each store behaves as today. It is an **ancestor of `m/cw-s3`** (cw-s3 = union + 57 purely-additive commits).
- **`m/cw-s3`** (the union tip, actively advancing) — the base to adopt. = denovo-land + age-index pyramid, treemap-first-class diff-mode, root-geneses, scan-picker, filter-views. Purely additive over the union; zero deletions.
- **`m/gcs`** is **not** a descendant of the union — a divergent deploy that *stripped* the cw stack (net −2369 lines: `sweep.py`, `cw_digest.py`, `cwBatch.ts`, `plan-sweep/*`) and added its own features (below). Its *additions* are the fold-in signal, but that fold-in is **mgu's job** (convergence.md).

### The variant contract (two axes — already built)

1. **`Store` descriptor** (`site/src/stores.ts`) — compile-time rows, resolved per-URL by `storeForPath()`:
   ```ts
   interface Store { key; label; title; desc; path; scheme; base; ogImage;
     prices: boolean; marks: boolean; sweep: 'owner'|'plan'; lifecycle?; peer? }
   ```
   gcs vs cw = which rows ship + these flags (`marks`/`sweep`/`prices`/`lifecycle`/`scheme`).
2. **Env config** (denovo 11/12's real win — per-deploy constants → env): `INDEX_VARIANTS` (which parquet tiers: gcs `path,user`, cw `path`), `BASE_SCOPE`, `EDGE_TRUSTED`, `D1_DB_ID/NAME`, `WARM_PATHS`, `ROOT_LABEL`. Python writer (`cloud/src/dt_cloud/index_footer.py`) + TS reader (`site/functions/_lib/index.ts:indexKey`) mirror the variant set.
3. **Auth as config**: server `BASE_SCOPE` + `EDGE_TRUSTED` (whole-host CF-Access edge gate vs app-gated D1 session); client `VITE_AUTH_MODE` (`edge`|`app`).

## Division of labor

### mgu's lane (convergence.md — do NOT duplicate here)

Finish the 4 "seams" + fold gcs's config-delta, landing one converged `main`:
1. Two sweep executors → plan-first model + per-cloud adapters (`GcsStore`/`CaiosStore`), one `/sweep`.
2. Two mark ledgers → gcs's `actions` WAL; cw's marks become action kinds.
3. Two D1 lineages → one renumbered `IF NOT EXISTS` lineage applied to both databases (the `site/migrations/{cw,gcs}/` split is the interim guard). **Unbuilt; hardest.**
4. Two digests → engine + per-store content profile.
Plus fold gcs's A-delta (`m/gcs:specs/cp-from-cw-s3-2026-09-16.md`, 50 files) so `cw-s3..gcs` is deployment-only.

gcs's fold-in BASE features (all mgu-owned): guest-chip/grant-subject auth + read-only scope + rotate, help/edu-drawer (also on cw → converge), plans-absorbs-marks refactor, page-scope-bar, lifecycle engine, over-time x-range picker, treemap reflow-on-resize.

### disk-tree's lane (what convergence.md does NOT cover — OA's two deploys never needed it)

1. **Public/no-gate auth mode.** Every mgu deploy requires a `baseScope`; `requireViewer` always gates; there is no anonymous mode (only read-only guest share links). r2.rbw.sh + future public clouds need a real `BASE_SCOPE=public` / gate-bypass. **Build it in `site/functions/_lib/auth.ts` + client `AUTH_MODE`.**
2. **Env-generalize `makeStore`.** `site/functions/_lib/index.ts` hardcodes `endpoint: 'storage.googleapis.com'` + `BUCKET = 'oa-gcs-usage-dvx'` (only HMAC creds are env-driven). Lift `endpoint`/`bucket`/`region`/`prefixes` into `Env` or the `Store` descriptor so r2/s3/gcs each point at their own store. `S3Store` is already S3-compatible; just thread the config.
3. **r2 variant + D1 + ingestion.** Add an `r2` `Store` row (ctbk/crashes/jc-taxes) + wrangler vars; stand up a D1 + the `dt_cloud` tier/index-sync ingestion (trivial at ctbk/crashes scale) over R2. Deploy `site/` at r2.rbw.sh.
4. **Shared packages as the superset.** `@rdub/treemap` + `@disk-tree/react` live here. cw-s3's `site/` uses `pendingCell` + `tipMode:'dock'` (cw additions) — keep DT's packages a superset (port the deltas; `tipMode` already ported in `76a07ad`).
5. **Dynamic edge-OG** into `site/` (DT owns it; mgu has only static per-user avatars) — later.

### Adoption / merge protocol

- disk-tree `cloud` re-seeds `site/`+`cloud/` from `m/cw-s3` (union tip), on top of DT's `main` (`ui/` superset, packages with DT's 199 commits, `src/disk_tree`).
- DT builds its lane on that base. When mgu lands its converged `main`, DT adopts it (shared cw-s3 ancestry → tractable merge).
- Endgame (convergence.md step 4 + this doc): DT `cloud` = converged app + DT's generalization layer = the base gcs/cw/r2/future all fork off. gcs/cw history retired.

## No conflict with disk-tree's own work

- **Dynamic edge-OG**: unique to DT (mgu's `og-user/*.jpg` are static avatars; no functions-side OG endpoint). No reconcile.
- **diff-index**: DT owns the persisted `<a>-<b>.parquet` *backend* (`/api/compare`); cw-s3's treemap-first-class `DiffTreemap` is the *frontend* that consumes compare output. Complementary — fold-in picks DT backend + cw FE.
- **viz-widgets / aggregation-extensions**: gcs specs cede these to DT (`@disk-tree/react`, `import --pivot-sum`/`--mean-mtime`). Already here.

## Per-deploy config (thin deltas)

| deploy | store row | schemes/buckets | auth | owners/marks | OG |
|---|---|---|---|---|---|
| r2.rbw.sh | r2 | ctbk, crashes, jc-taxes | **public (new)** | off | dynamic (DT) |
| gcs.oa.dev | gcs | gs://… | app-gated (`BASE_SCOPE=gcs`) | on | dynamic (adopt) |
| cw-s3.oa.dev | cw | s3://… | edge (`EDGE_TRUSTED`) | off | dynamic (adopt) |
| disky.rbw.sh | `ui/` superset | file+r2+gcs+s3+ssh | none/local | off | dynamic |

## Phased plan

1. ✅ Union view + single-cloud collapse in `ui/` (`dab2d2e`, `786b5bb`).
2. ✅ Establish the discovery + division (this spec). Superseded the raw-gcs hoist.
3. ✅ Re-seed `cloud` from `m/cw-s3` (union tip) over DT `main`; reconciled the `packages/*` delta (`renderTipDefault`, `Series.dots/strokeWidth`); `ui/` unregressed (`cbb6e33`).
4. ✅ **DT lane — r2 is LIVE** (2026-09-21, [[r2-rbw-sh-deploy]]): public/no-gate auth (`PUBLIC_READ`), env-generalized store seam (`storeCreds`/`storeReady`/`STORE_*`, incl. the data proxy), `r2` `Store` row + `VITE_STORE` selector + `wrangler.r2.toml`, ingestion (existing `disk-tree bulk-list` + `dt-cloud webdata`/`index-tiers`/`index-sync` — zero new code), `disk-tree-demo-db` D1, daily cron. r2.rbw.sh = the union Map over ctbk/crashes/jc-taxes.
5. **Adopt mgu's converged `main`** when it lands (merge; DT's lane rides on top). See *Rebase readiness*.
6. **Dynamic edge-OG** into `site/` (+ diff-index-in-site) — DT base features not yet folded into the shell.
7. **Age index / diff-index → adopt `pyrmts` + `multiscan`** (2026-09-22, resolving the earlier "wait for cw's redesign"): the redesign landed as **pyrmts** — the shipping `(shard×bin)`-tier pyramid library (`~/c/pyrmts`, Python build + TS/CFW serve; used by ctbk/awair/crashes), whose recent **multiscan** work (Phases 1–2c) is exactly our need. See *pyrmts adoption* below. r2's age chart stays hidden until adopted; do NOT build the ad-hoc `write_age_pyramid` into the r2 job.
8. **Superset** (`ui/` → disky/blobby domain), unchanged multi-cloud/local scan-manager.
9. **Retire KLC** (`keep_last_ckpt`) — a mark+sweep-era action being phased out everywhere. Its *plumbing* (the `ck.txt` index-extras sidecar + `node.k` flag) is **done** on `cloud` (`76cdd17`); the *mark action* (persisted enum, live D1 rows, `sweep_plan.py` planner) rides mgu's D1-lineage merge. See *KLC retirement*.

## Rebase readiness — what makes `cloud` the base cw+gcs move onto (2026-09-21)

**State of `cloud` HEAD:** `site/`+`cloud/` ≈ the cw-s3 union tip (as of `cbb6e33`) + DT's **additive, backward-compatible** r2/public/store-seam/public-auth/data-proxy layer + DT's superset `packages/*` + the `ui/` superset. It builds clean and does **not** regress a gcs/cw build (`VITE_STORE` unset = the default store; `STORE_*`/`PUBLIC_READ` unset = GCS defaults + the old gate). So HEAD is a **safe target that adds capability without breaking** either deploy.

Caveat: the union was adopted by file-copy (`git checkout m/cw-s3 -- site cloud`), so `cloud` and the mgu branches share only the **old** fork base — a "rebase onto" is conceptual (replay the delta), not a fast-forward.

**cw — the near-ready one** (it's the union carrier):
- `cloud`'s `site/cloud` = cw-s3 at adoption + additive layer. cw has moved a little past (age-index, "review round" UI polish, a `packages/treemap` reflow fix).
- Path: cw replays its post-adoption delta onto `cloud`; residual conflict is only the handful of files DT also touched — `_lib/{auth,index}.ts`, `data/[[path]].ts`, `packages/{treemap,react}`. All small + BC.
- **To minimize even that:** DT should upstream its store-seam + public-auth + data-proxy commits into cw's line (or cw CPs them) so those files converge *before* the replay, and **packages unify at DT's superset** (cw stops editing its net-negative fork; consumes `@rdub/treemap`/`@disk-tree/react` via `workspace:*`).

**gcs — needs the convergence first** (it's a divergent peer, not a union descendant): gcs stripped the cw stack and added its own BASE features (guest-chip auth-as-config, plans-absorbs-marks, help/edu-drawer, page-scope-bar, lifecycle engine). It cannot cleanly move onto the cw-flavored union until those are expressed as **config on the union** — which *is* `convergence.md`'s 4 seams + config-fold. So gcs's prerequisite is mgu's convergence, and disk-tree HEAD is its **target**, not yet portable-onto.

**What still needs doing for `cloud` to be the complete base (ranked):**
1. **mgu lands `convergence.md`** — the single funnel that extracts both deploys' BASE features into config-gated form and unifies the 4 seams (sweep→plan-first+adapters, marks→`actions` WAL, one D1 lineage, digest engine+profile). Nothing else lets gcs become config-only. **Critical path.**
2. **Unify `packages/*`** at DT's superset — one `@rdub/treemap`/`@disk-tree/react`; cw/gcs consume, stop diverging. (DT already holds the superset API; the remaining work is cw/gcs adopting it.)
3. **Fold DT's own base features into `site/`** — dynamic edge-OG (replaces static) and the persisted diff-index backend — so the base *has* them and gcs/cw adopt on move rather than carry as delta.
4. **Adopt the `pyrmts` age index** once cw's redesign settles (base feature; r2 waits).
5. **Decide where the convergence lands** — cleanest is for mgu to push its convergence commits **onto `cloud`** (one convergence, on the base) rather than a separate mgu branch DT re-adopts. That makes cw/gcs fork off `cloud` directly.

**US-first, to keep cw/gcs deltas config-only + legible:** don't US gcs's BASE features piecemeal now (it races the convergence — let `convergence.md` be the funnel). Do US, here, *before* the moves: DT's dynamic-OG + diff-index into `site/` (#3), and DT's `_lib` store-seam/public-auth commits into cw's line (#2/cw section) so the shared files are pre-converged.

## KLC retirement (2026-09-21)

`keep_last_ckpt` (KLC) — the "keep the latest checkpoint, sweep older ones" mark action — is being phased out everywhere (base + gcs + cw). It only made sense in the old mark+sweep world; agent-driven keep marks (e.g. "keep every 10th, back from latest") replaced the need. Removal splits by blast radius:

- **Plumbing — done on `cloud` (`76cdd17`).** The `ck.txt` index-extras sidecar only fed `node.k`, the precise checkpoint-shape flag gating the KLC button. Excised edge/ingestion-only (no marks schema): `dt_cloud.extras` stops emitting `ck.txt` (`write_extras(pfx_df, out_dir)` → `attr.tsv` only); `_lib/extras.ts` `ExtrasView` drops `ck`; `view.ts` stops setting `node.k`; client drops `TreeNode.k`. `looksCkpt` keeps offering the (still-present) button via its heuristics. **`attr.tsv` provenance is untouched** (separate owners concern). Also fixed an r2 500 (empty `ck.txt` on checkpoint-free buckets; reader hardened in `74c48b2`).
- **The mark action — rides mgu's convergence.** `keep_last_ckpt` is a persisted enum with live rows in gcs/cw D1s, in the `CHECK` constraints (all three lineages), edge validators + `totals` decomposition, the client `klcSplits`/`Treemap` barber-pole ring, and a parallel Python planner (`sweep_plan.py`). Enum removal + row migration (`keep_last_ckpt` → `keep`) belongs in the D1-lineage merge, not a separate pass that races it. When the enum is gone, DT does the base-side cleanup of `looksCkpt` + the KLC button/CSS/glyph.

## US'd from cw-s3 + CI (2026-09-22)

Adoption content-boundary is **`eb3fb63`**. US'd the stable, non-age part of cw-s3's post-boundary delta onto `cloud`: `6b742e2` (diff drawer hover→movement table), `009e6fd` (lifecycle sort), `c4ac05a` (over-time index / obs-axis Phase 1). **Held** the age-index Phase B pyramid redesign (`0519ab7`/`7813a03`/`8f70e51`/`f93024d`) per the phase-7 deferral. The 1C diff treemap + its `contrastEdge` adaptive borders were already here (the `packages/treemap/src/diff/` module + edge logic are byte-identical to cw-s3's; only the core diverges — cloud's superset vs cw-s3's `inlineSizeMinWidth`, a US-next candidate).

CI: `.github/workflows/deploy-r2.yml` gates an auto-deploy to r2.rbw.sh on push to `cloud` behind the hermetic suites (site vitest+tsc, `dt_cloud`+engine pytest incl. e2e backends) + a post-deploy smoke. `dev.r2.rbw.sh` (a Pages preview target for the pre-deploy Playwright e2e) is the next infra step.

## pyrmts adoption (2026-09-22) — the age/diff/over-time engine + redundant-scan compression

`/read pyrmts` established: **pyrmts** (`~/c/pyrmts`) is a mature multi-scale timeseries pyramid library — pre-compute `(shard×bin)`-tier aggregates once, serve any range×bin-budget query in O(log) bins from the edge. Python build (`pyrmts`/`pyrmts-engine`/`pyrmts-ops`) + TS serve (`pyrmts`/`pyrmts-cfw`/`pyrmts-geo`/`pyrmts-react`). A consumer provides only: (a) a pyramid config YAML, (b) a raw→base-tier ingester, (c) storage/D1 bindings, (d) thin CLI/handler shims — pyrmts owns the rest.

**Full spec: [`pyrmts-adoption.md`](./pyrmts-adoption.md)** (2026-09-22) — the authoritative home for the mapping, the two-index model, the ingest/serve wiring and the open questions pyrmts is reviewing. What follows is the summary.

**Why adopt here:** it supersedes disk-tree's *three* ad-hoc things — the `write_age_pyramid` age index, the over-time index (`d249b55`, US'd from cw), and the diff-index — with one cross-consumer engine. **Two orthogonal indexes, both on by default (pyrmts correction 2026-09-22):** the **multi-scan store** bounds storage and serves the over-time line (`seriesFor`/`seriesAcrossGroups`) but does *not* give cheap diffs between arbitrary scans (a pair straddling an archive junction is O(fleet)); the **always-on diff-index** (pyrmts main `e03c490`, a dyadic changeset hierarchy) serves the diff-treemap via `diffOverSpan` in O(log N + changes-in-span). Age = the pyramid's created-date `binCol`. Keep the dTM off the MS archives.

**The big win — `multiscan` for the public demo:** disk-tree's r2 demo stores **one full index per daily scan, each ~a duplicate of the last** — the exact O(N) blowup multiscan (Phases 1–2c, landed 2026-09-21/22, *originated in cw-s3*) compresses by folding a re-observation axis into shards (interval-encoded SCD-2; the monoid/tier ladder is untouched — the scan axis is a *stack*, not a rollup). cw-s3 measured on real path-indices: **~0.020% churn/12h scan → 5.9× @N=6, ~78× @N=81, ~335× @daily·1yr, ~626× @N=730 — compression *grows* with scan count on near-static data.** Our snapshot path-index (keyed `(path)`, no created-date axis) maps cleanly: a **constant `binCol`** (`dt=0`) reduces the key to `(*dims)`. `multiscan` CLI takes `--engine python|duckdb` (the DuckDB out-of-core backend is byte-identical to the Python oracle and is what production N needs); Phase 2c added a scan-location manifest so reads route via `resolveScan` and individuals can be verify-then-dropped.

**Use `scheme: exponential` from the start** (pyrmts heads-up, 2026-09-22, dist @ pyrmts main `23c1587`; `pyrmts:specs/multi-scan-consolidation.md` §"grouping policy" / Phase 2d). Ryan flagged *our demo* as the motivating case for it. Fixed-K grouping ("every K scans → one archive") leaves archive count O(N/K) — unboundedly many tiny archives for a low-churn demo kept forever. **Exponential** (Bentley–Saxe / logarithmic method) coalesces old scans into power-of-2-sized archives so archive **count stays O(log N)** — for daily-scans-kept-forever that's ~9 archives/year vs ~365. Declared in the pyramid config: `multiScan: { scheme: exponential, base: 2, dataset, tier, shard, drop: true }`, run via `multiscan seal <config>` — **idempotent**, so a post-scan stage in `daily-ingest.yml` just fires it each cycle and it compacts as needed. **D1 gotcha:** exponential *compacts* (deletes superseded manifest rows as blocks merge), so the routing-manifest → D1 sync must **mirror the row set, not append-only** (fixed-K is append-only; `MultiScanD1Index` in `pyrmts-cfw` backs the manifest). Read side is all there: `seriesFor` (over-time lines), `resolveScan`/`readMultiScan` (routing), footer-pruned per-path reads stay O(pruned). Dovetails with the federated-scans north-star (`specs/federated-scans.md`) — each location consolidates independently, the union reader routes via the manifest.

**Read-path gotcha (exponential-only, pyrmts heads-up 2026-09-22) — bake into `pyrmts-adoption.md`:** an archive's **key changes as it compacts** — a scan starts in a size-1 archive, merges into size-2, size-4, …, and the key encodes `(first-scan-label + block-size)`, so the archive holding a given day moves over time. The manifest + `resolveScan`/`seriesAcrossGroups` always route to the *current* archive (self-correcting), **but the embeddable viz must resolve through the manifest per read — never cache or hard-code an archive URL; cache the `(dataset, scan/path)` query, not the resolved key.** `seriesAcrossGroups` re-lists each read (`list_multiscans → filter tile → per-group seriesFor`), so it's automatically current. Reassuring corollary: dropping individuals after `seal` loses nothing — `extract_scan` reconstructs any single day's tree from its current archive, so "tree as of day X" survives compaction. (Fixed-K keys are stable — this is exponential-only.) pyrmts confirmed the plan shape (per-location dataset = its own dyadic ladder, constant-`binCol` snapshot pyramid, `seal` as idempotent daily-ingest stage, mirror-not-append D1) and will sanity-check the union-path-index→`Pyramid` mapping + per-location dataset scoping once `pyrmts-adoption.md` is up.

**Division:** cw-s3 owns the DuckDB producer + CFW reader (being upstreamed into pyrmts); cw is wiring multiscan for its 12h GCS fleet. disk-tree adopts pyrmts's `path-index` consolidation. **Next step: a `specs/pyrmts-adoption.md`** scoping the disk-tree side (pyramid config for the union path-index, the constant-binCol snapshot contract, the `scheme: exponential` `multiscan seal` post-stage in `daily-ingest.yml`, and the `pyrmts-cfw` D1-manifest read path in `site/functions`). No code yet — needs its own spec + light cw coordination.

## Coordination

mgu convergence lives in `m/cw-s3:specs/convergence.md` (+ `denovo-factor.md`, `branch-parity-discipline.md`; gcs side `gcs-toward-union.md`, `sweep-plan-union.md`). Two live mgu sessions: main clone (`7b789415`, the convergence) + cw-s3 worktree (`41e25a3f`). Write a handoff spec into `/Users/ryan/c/oa/marin-gcs-usage/specs/` once DT's `cloud` base is adoptable, so the mgu session knows DT owns the r2/public/makeStore layer + will adopt their converged `main`.

## Naming

`dt_cloud` (renamed from `gcs_usage` on mgu, CLI `dt-cloud`). ✅ **DONE (2026-09-22):** its `webdata` verb was renamed to **`dt-cloud path-index`** (it builds the served path-index tiers + snapshot; `index` would collide with `disk-tree index` = "scan a source"). The function `write_webdata`→`write_path_index`, the CLI command function `build_path_index`; callers (`r2-ingest.sh`, `daily-ingest.yml`) updated. Applied on `cloud` (the base) rather than waiting for convergence — mgu absorbs it on rebase; recorded in the mgu handoff (`specs/disk-tree-as-base.md`).
