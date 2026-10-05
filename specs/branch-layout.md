# Branch layout: each long-lived branch carries exactly what its deployments run

**Status:** decided 2026-10-01 (Ryan), in progress; direction revised 2026-10-03 (§0). Decisions: (1) delete the Flask server + `ui/` + the interactive CLI; (2) **no `oa` branch** — Slack/Discord posting, healthcheck, warm-cache, lifecycle and GCP helpers stay on `cloud` as a shared toolkit (gcs's and cw's Slack styles as templates); (3) intra-file persistent diffs are intrinsic and their conflicts are wanted — no hooks to avoid them; generic owner/lens machinery stays on `cloud` as bones, gcs's owner *policy* moves to `gcs`; (4) delete the §3 list after re-verifying no branch uses each item; (5) order: deletions → create `local` → removals with `merge -s ours` → retarget m3/app to `local`. The tree is `cloud` → {`gcs`, `cw-s3`, `local` → {`m3`, `tauri-native-app`}}.

## 0. Direction (Ryan, 2026-10-03): a maximal shared base

This supersedes §3's "→ `gcs`" and "→ `cw-s3`" moves.

`cloud` provides **orthogonal abstractions** that each deployment mixes in: users/owners and attribution, GCS / S3 / R2 stores, access-log ingest and processing, staged deletes and sweeps, digests, the site. It also carries **end-to-end examples** of them combined:
- "GCS + users + access logs", modeled on gcs;
- "plain S3", modeled on cw-s3;
- the public r2.rbw.sh demo, deployed from `cloud` itself.

Another deployment can reuse the combined helpers or copy an example.

What leaves `cloud` is only what's **truly OA/Marin-specific**: identities, buckets, domains, project and channel ids, people, OA-specific defaults. The examples may be obviously inspired by the OA deployments but carry no OA values. So `cloud..gcs` and `cloud..cw-s3` should each be a minimal OA-specific diff, mostly config: `wrangler.toml` vars, job env, `Pulumi.<stack>.yaml`, identity data.

The work: audit `cloud` for OA-specific behavior (defaults, constants, registry entries) and replace each with a config key whose OA value the deployment branch sets in the same round, so no branch changes behavior on its next merge of `cloud`. Comments that merely use Marin as an example are cosmetic. Whole modules with no use outside OA move to their branch.

Ryan: *"each with exactly the code (and migrations) they need, and nothing they don't … diffs between branches should reflect exactly the changes in both directions, nothing more and nothing less."*

Today `cloud` is the union of everything: every `[base]` pick from gcs, cw-s3, m3 and the app landed there, whoever used it. This spec classifies what's on `cloud` by who runs it (a read-only audit of all five branches' job scripts, wrangler configs, LaunchAgents and imports, 2026-10-01), proposes a branch tree, and lists the decisions needed before the moves.

## Progress (2026-10-01)

- **Done on `cloud`:** dead code + Flask/`ui/` + interactive CLI deleted (939e7eb); laptop-only code (75674f2), local-filesystem scanning (ed2bf71) and `index` + its lister + `rescan-demo.yml` (4a06f4a) moved to `local`; the scans DB and hybrid storage stay (cw-s3's job reads `disk-tree import`'s blob). The r2 demo has its own page head + OG card (424656d); cw's stale `site/wrangler.toml`/`wrangler.dev.toml` and gcs's pre-squash `migrations/gcs` left `cloud` (1cfded5, 2c7bd9e); the shared `cf/cfn_dashboard.py` + ignore files live on `cloud` once (d4d592c); CI runs on every branch but `dist/**`, with dt-cloud tests and the site's build + vitest.
- **`local`** (on `r` since 2026-10-01; the 2021 branch is tag `archive/local-2021`): `cloud` + the laptop code; m3 and `tauri-native-app` merge it, never `cloud`.
- **All four deployments merged** the restructured base (gcs, cw-s3, m3, app), as local commits pending their pushes/deploys.
- **Conventions learned:** a removal commit holds only removals (children take it with `merge -s ours`; a bundled fix is silently lost); merge a rename-adding commit separately from a removal, or git reads the pair as a rename; tag picks `[cloud]` / `[local]`.
- **Open (Ryan):** `CLAUDE.md` as a regular file + `@AGENTS.md` import on every branch (the gcs/cw symlink conflicts on every `cloud` merge touching it); the gcs/cw unification sequence (digests → sweep executors → `path-index`/`index-write` → identities into D1 → sheet mirror); the ops cron program's per-cron checkout paths.

## 1. Who runs what

| deployment | branch | what actually runs |
|---|---|---|
| r2 demo (r2.rbw.sh) | `cloud` | `deploy-r2.yml` (site, `wrangler.r2.toml`), `daily-ingest.yml` (`disk-tree bulk-list r2://` → `dt-cloud path-index` → `index-sync`), `rescan-demo.yml` (`disk-tree index r2://`); public, read-only, no staging |
| gcs.oa.dev | `gcs` | Batch `job/run.sh`: `dt-cloud` `access ingest`, `job submit-listing` (`disk-tree bulk-list gcs://`), `stage`, `labels`, `path-index`, `rules`, `lifecycle pull`, `index-sync`, `healthcheck`, `warm-cache`, `digest`, `index-gc`, `sweep`; site with owners/claims/lens, sweep executor, Slack |
| cw-s3.oa.dev | `cw-s3` | Batch `job/cw-run.sh`: `disk-tree bulk-list -a s3://`, `import -e stream`, `dt-cloud` `lifecycle pull`, `index-write`, `index-sync`, `index-gc`, `publish-r2`, `warm-cache`, `cw-digest`, `over-time-groups`, `plan-sweep`; site with the meta store, plan-sweep executor, Slack |
| disk.rbw.sh (laptop) | `m3` | LaunchAgents via disky.app: `disk-tree index` / `capture`, AWS Batch `dt-cloud path-index -g` + `index-sync`, drainer `disk-tree dispatch -s --trash`; site with `EXECUTOR=laptop`, `STORE=laptop`, `HOME` |
| disky.app | `tauri-native-app` | Rust walker + agent host for m3's scripts |

The Flask server + `ui/` (and most interactive `disk-tree` CLI: `du`, `filter`, `diff`, `histogram`, `library`, `staged`/`dispatch` routes in Flask) has **no live deployment**: disk.rbw.sh serves `site/` since m3's Phase 4, the pywebview app is superseded by Tauri, and m3's spec plans to sunset it.

## 2. Proposed tree

```
cloud ── shared engine + path store + site viewer + the r2 demo
 ├── oa ── what gcs and cw share and nothing else uses (Slack, lifecycle, GCP, healthcheck, warm-cache, index-gc, sweep/staged UI shared by the two, signed-in OA auth extras)
 │    ├── gcs
 │    └── cw-s3
 └── local ── laptop / APFS / macOS (capture, apfs, extents/reclaim/overcount, volumes, trash, drainer, laptop executor, app-link, walker seam, freed_bytes)
      ├── m3
      └── tauri-native-app
```

Rule for every future `[base]` pick: it goes to the lowest branch whose deployments all use it — `cloud` only if the r2 demo uses it or ≥ 2 of {oa, local} do.

Mechanics (once per move): each removal from a parent is one commit there; each child records it with `git merge -s ours <that commit>` so its own copy survives and later merges stay conflict-free. Migrations follow cw-s3's plan: one `site/migrations/` per branch, names only (wrangler's `d1_migrations` records file names, no dir); never rename a migration already applied on that branch's D1; shared code tolerates a missing migration (probe, as `drain.py` does for `freed_bytes`).

## 3. Classification (abridged; the full audit is in this session's log)

**Stays on `cloud`** — engine core (`blobfs`, `listing`, `listing_format`, `config`, `find/{groups,tiers,bulk*,import_listing,aggregate_duckdb,aggregate_stream,index}`, `backends/{base,gfind,local,s3,url}`, `storage/{base,parquet,hybrid}`, `scan_manifest`), `dt_cloud` `path-index` / `index-sync` / `index_footer` / `viz` / `secrets`, site viewer (`subtree`, `diff`, `series`, `age-pyramid`, `store`, `data`, `v1/files`, `_lib/{index,view,scope,filter,series,shared,zstd,agePyramid,edgeCache,auth}`, `src` viewer components), `packages/treemap`, `TimeSeries`, the r2 workflows, migrations `0001`–`0006`.

**→ `oa`** (gcs + cw only) — `dt_cloud` `healthcheck`, `warm`, `lifecycle`, `index-gc`, `site.py`; site `gcp.ts`, `slack.ts`, `stagedSlack.ts`, `slack/actions`, lifecycle UI; `health.yml`; `site/deploy`, `cf-status`.

**→ `gcs`** — `find/bulk_gcs.py`, `access/{aggregate,parsers,read_sizes}`; `dt_cloud` attribution / digest / sweep_exec / staged_plan / stage / batch / gcp / cascade_a2a / sheet_mirror / access / identity …; site ownership stack (`actions`, `assignments`, `estate`, `owners`, `token`, `users`, `user/[id]`, `sweep/*`, `v1/objects`, `_lib/{identity,claims,owners,ownerTotals,ledger,extras,unfurl,sweepDispatch,sweepReflect}`, ~15 `src` files); `deploy/sheet-mirror`; gcs e2e.

**→ `cw-s3`** — `dt_cloud` `index-write`, `over-time-groups`, `publish-r2`, `cw-digest`, `plan-sweep`, `overtime`, `sweep`; site `overTime`, `planDispatch`, `runReflect`, `cwBatch`, `stores.ts` meta store; `tiers cut|plan`, `recompress`, `listing-format` (cw one-shots).

**→ `local`** — `apfs`, `extents`, `reclaim`, `overcount`, `volumes`, `repos`, `capture`, `staged*`, `drain`, `d1`, `trash`, walker seam + `dt-walker` regex, site `appLink`, `laptopDispatch`, `~` path display, migrations `0007_agents`, `0008_freed_bytes`; and (decision 1) the Flask server + `ui/`.

**Delete (dead everywhere)** — `disk_tree.notify` + `digest` CLI, `batch.py` + `iac*` + `iac/`, `access/{state,schema}` + `parsers/{s3,r2}` + `cli/access`, `tree_build`, `backends/ssh`, `storage/{duckdb,sqlite}` + `/api/backend*`, legacy `migrate*`, `desktop.py` + `packaging/macos`, `reduce` (+ `reduce.yml`), `snapshots`, `fetch|pull|sync`; `dt_cloud` `reactive`, `listing`, `weekly`, `compare`, `census`, `over-time-write`, `index-compact`, `prune-r2-listings`, `stamp-published`; site `api/{age,claims,bench}.ts`, `cw.ts`, stale e2e/assets; `ui/` `auth.tsx` + `AccessPage` + `@open-athena/auth` dep + OG capture; `extra-mp4s.py`, `scripts/r2-ingest.sh`, broken `release*.yml`. Each with its tests.

## 4. Fix first, independent of the move

`dt_cloud/index_footer.py` defaults `D1_DB_ID` to **gcs's production D1** and `INDEX_VARIANTS` to `path,user`, falling back to OA credentials: a misconfigured r2 or m3 run would write footers into gcs's prod D1. Remove the defaults (require them), set gcs's values in gcs's job env. Do this now.

## 5. Decisions for Ryan

1. **Flask `ui/` + the interactive CLI**: no deployment runs them. Keep on `cloud` as the open-source local tool, move to `local`, or retire (the wheel then stops bundling `ui/dist`)?
2. **`oa` branch**: create it for gcs+cw-shared code, or keep that code on `cloud` gated by vars?
3. **Entangled gcs code in shared site files** (the user lens / by-user tiers in `view.ts`/`index.ts`/`scope.ts`, ~350 lines; `o=`/`cl=` cache-key params): extract behind a scope hook (medium-hard) or leave in `cloud`, gated and inert elsewhere?
4. **Dead-code deletion**: approve the §3 delete list as a whole, or review item by item?
5. **Order**: proposed — §4 fix; dead-code deletion on `cloud`; create `local` and `oa` from `cloud`; one removal commit per destination; children `merge -s ours`; m3/app retarget to `local`, gcs/cw to `oa`.

## 6. Open facts to verify before moving migrations

Which migrations each D1 has applied (nothing applies them automatically): cw prod per memory `0001` (squashed), `0002`–`0004`, `0006`, `0007` (`0005` likely); gcs prod `0001` (squashed), `0027`, `0028` (`0029`–`0031` likely); r2 demo `migrations/cw` (numbering uncertain); m3 through `0007` (`0008`, `0009` likely). Check each with `wrangler d1 migrations list --remote` (read-only) before any renumbering.
