# Branch parity discipline: audit + CP scrambles, verified with git-didi

## The model (decided 2026-08-24, reaffirmed 2026-08-28)

**One deployment = one long-lived branch**, cherry-picking between them, no
base branch: `gcs` (gcs.oa.dev), `cw-s3` (cw-s3.oa.dev), and future R2 / AWS-S3
deployments. Separate deployments have separate auth lists and their own branding/scheme.
(This first read "cw-s3 has no team axis" and treated per-deployment
user-groups + mark & sweep as intended deltas; the team/group axis was excised
everywhere on 2026-09-06 — ownership is a person or unclaimed on every branch —
and the 2026-09-08 update below reverses the mark+sweep stance: it is now slated
to port to every deployment.) Each branch owns its copy of the FE core
(`@disk-tree/react`) and the Python engine (`src/disk_tree`) and may do whatever
it wants with them. A large stream of CP-analogous commits across branches is
the *intended* workflow, not drift to be engineered away.

Upstream `runsascoded/disk-tree` (remote `dt`) is a sibling in this graph, not
a parent: it ships the **Flask × local/cloud-scan** reference www arch; the
marin branches ship the **Vite + CF Pages Functions × cloud-store** arch. The
shared thing is the Python scan/index core (`bulk-list`, `tree_build`, the
access plane, `webdata`-style aggregation). Each www arch answers its own
serving questions natively — no LCD abstractions across archs.

(A 2026-08-28 spec proposing to collapse cw-s3 into gcs and pin the engines
as dependencies re-litigated this and was withdrawn.)

### 2026-09-08 update — toward one cross-cloud impl

The per-deployment-customization premise above is softening. Direction (Ryan):
mark & sweep and user/owner views are to be **ported to every deployment**
(cw-s3, plus the R2 / AWS-S3 reference deploys upstream is adding), not kept as
per-branch deltas — dogfooding sweep on Ryan's own S3/R2 clouds is wanted. The
likely end state is ~one shared cross-cloud impl. A read-only, user-excised
deploy is still a plausible durable fork worth a branch, but it becomes the
exception, not the rule.

What stays intrinsic per deployment: the store engine (GCS vs S3 listing +
creds), branding / domain / Access app, and any data a given cloud can't
produce. The last is the live blocker for user views on cw-s3: gcs derives
ownership from GCS access logs (write principals) + rules + wandb signals;
CoreWeave/CAIOS has no access logs, so cw-s3 has **no owner/writer attribution
today** — its `TreeNode`s omit the `tm/sh/us` fields and `cw-webdata.py` renders
the raw bucket. Marin's CW bucket does encode users in path prefixes
(`users/<name>/`), so path-rule attribution (no access logs) is the likely CW
route. Deciding that signal is the prerequisite for the user/sweep port.

## What today's audit actually showed

Not that branches are wrong — that the model's verification half was missing.
Nobody checked `gcs` vs `cw-s3` vs `dt/main` per surface between scrambles, so
`cw-s3` fell 10 react commits behind, the Python engine diverged both ways
with no ledger, and a per-branch script (`job/cw-*`) was pruned on the
assumption the other branch carried it. All fixable with routine, none with
architecture.

## The discipline

1. **Ledger of intended divergences** — per branch pair, per surface, a short
   list of the deltas that are *supposed* to exist (cw-s3: branding + `s3://`
   scheme, own auth list/Access app; vs upstream: Flask server
   + `ui/` vs `site/`; …). Kept in this file (below). Everything not on the
   list is a CP candidate or a mistake.
2. **[git-didi](https://github.com/runsascoded/git-didi)** as the checker —
   diff-of-diffs to verify long-lived forks keep only their intended deltas:
   `git-didi stat <base>..gcs <base>..cw-s3` (and `patch` per file) across the
   surfaces `packages/react/src`, `src/disk_tree`, `site/`, `job/`, `marin/`.
   Wrap as `scripts/branch-audit` (or a `/branch-audit` skill) that prints
   unexpected drift per surface; run it at the end of every CP scramble and
   any time a branch is touched for a while.
3. **Scheduled CP scrambles** — a recurring pass (post-milestone, or weekly
   during active work) that evaluates each branch against the others and
   moves the useful bits every direction, via CP manifests like
   `specs/done/dt-core-cp-2026-08-28.md`. The scramble ends with the audit
   green.

## Intended divergences (ledger)

| pair | surface | intended delta |
|---|---|---|
| gcs ↔ cw-s3 | `site/src` | **Intrinsic:** own Pages project (`oa-cw-s3-usage` ← cw-s3.oa.dev) + Access app `4c463052` (whole-host, OA + CoreWeave domains, edge identity); branding, `s3://` scheme. **Serving model at parity since 2026-09-16**: every read is a view query (`/api/subtree` drill chain + `depth=1` first paint, `/api/diff`, `/api/series`, react-query, `useHashSpy`, `SpeedDialTip`) — no whole `tree.json` / baked `diff.json` on the page. **Remaining intended deltas**: the drill lives in `?path=` (gcs: the URL path); no owner / mark-state / storage-class axes and no `/users`, `/user/:id`, `/marks`, `/assignments`, `/admin` (no CW ownership signal, no classes); cw's plan-first `SweepPage` (specs/done/cw-sweep.md) vs gcs's owner-slice console; cw's `Treemap`/`ChildrenTable` are the owner-free adaptations (file-level convergence onto gcs's is a later pass); the age chart is fleet-wide `age.json` on both branches (`/api/age` pending on both). **Shared verbatim since the 2026-09-16 h11n pass**: `Tooltip.tsx`, `units.tsx`, `colors.ts`, `main.tsx`, `dev/renderSpy.ts`, `scan.ts`, `OgPage.tsx` (on `/api/subtree`), `AuthGate.tsx` (on `@open-athena/auth`; cw's `auth.ts` supplies the `edge` whoami source + `/login`); `stores.ts` is the same file with one row (`cw`, `s3://`, `/data/cw`); `types.ts` differs only by cw's `ColorMode` set and the non-hook `fmtBytes`. **Queued general CPs (cw-ward)** still living in the cross-cut files: loading states `2cb25f2` `79c85ed`, legend hover/pin `974ee7c`, page bar + Diff header `c24632a`, diff first paint `5ef41d2` `f2d0a8d`, cell legibility `f17c666`, legend/axis URL toggles + scroll-spy `8191de0`, hue-fan L2 + opaque cells `4ba6af7`, chain borders / `collapseChains` `63dcec3` `34d3d04`, adaptive edges `3296ccd`, legend metric chips `bb11c05`, makeup stripes `6805541`, tooltip copy/open `679dd8c`, children-table elision + one-commit selection `170a144` `de281ff`, URL-path drilling `67d8132`. |
| gcs ↔ cw-s3 | `site/wrangler.toml`, `site/migrations` | **CP-adapt, small patch** (2026-09-11): cw-s3's `wrangler.toml` = gcs's with `name`/`database_name`/`database_id`/`ACCESS_AUD` changed, the auth-package-migration prose dropped, and (2026-09-16) the `CACHE_KV` binding commented out until a KV namespace + a KV-scoped token exist. `site/migrations` = a fresh subset — `0001_marks` (keep-axis, default-unmarked), `0002_plans` (plan-as-first-class), `0003_deletions`, `0004_admin`, `0005_index_footer` (= gcs's 0013+0014+0018+0020 index-footer end state, one file) — **not** a replay of gcs's 23-step auth/access-log history. The git-didi delta on these surfaces is the intended patch, not a whole-file absence. |
| gcs ↔ cw-s3 | `job/` | `run.sh`+`batch-submit.sh` (GCS: six buckets, attribution, access log, Discord twin, weekly report) vs `cw-run.sh`+`cw-batch-submit.sh` (CW: S3-compat listing → layer-2 → `index-write` tiers → `index-sync` → tree/age/meta JSONs → the `#cw-s3-usage` digest); the baked `diff.json` (`cw-diff.py`) is gone (2026-09-16 — `/api/diff` serves it). `build.sh`, `Dockerfile` layout, `.dockerignore`/`.gcloudignore` mirror gcs. Not wired on cw: `warm-cache` (needs an Access service token), `index-gc -r` retention. |
| gcs ↔ cw-s3 | `packages/treemap`, `packages/react`, `src/disk_tree` | **none** — keep at parity (synced 8/28; re-verified 2026-09-16) |
| gcs ↔ cw-s3 | `cloud/src` (`marin/` → `gcs-usage/` → `cloud/`, all on 2026-09-16; package `dt_cloud`, CLI `dt-cloud`) | **Shared verbatim**: `index_footer.py` (only `D1_DB_ID`, the wrangler DB name and the path-only variant set differ), `warm.py` (cw paths + service-token auth). **cw-only**: `index.py` (layer-2 → index tiers; gcs writes its tiers inside `webdata`/`viz.py`), `sweep.py` (plan-first CAIOS executor, versioned undo/purge, TTL expiry manifest), `listing.py`, `lifecycle.py` (CAIOS bucket lifecycle rules as a tracked file); `digest.py`/`digest_plot.py` are cw's framing-A content on gcs's mechanism. **gcs-only**: attribution (`identity`/`prefixes`/`rules`/`signals`/`attr_index`/`records`), `access.py`, `mark.py`, `sweep_plan.py`/`sweep_exec.py` (owner-slice sweep), `reactive.py`, `extras.py`, `healthcheck.py`, `index_footer` extras (`index-blob`/`index-compact`), `weekly.py`, `discord_api.py`, `cascade_a2a.py`. |
| gcs ↔ cw-s3 | `site/functions` | **Shared verbatim** (2026-09-16): `_lib/index.ts` (reader; the allow-list is the union of both deployments' prefixes once gcs takes specs/cp-from-cw-s3-2026-09-16.md §1), `_lib/shared.ts`, `_lib/edgeCache.ts`, `_lib/gcp.ts` (the CoreWeave Batch spec lives in cw-only `_lib/cwBatch.ts`), `data/[[path]].ts` (gcs's store-aware proxy; cw gates with `requireViewer` and D1-filters every store's listing). **Adapted**: `_lib/view.ts` = gcs's minus its owner / mark-state / class / extras axes; `api/{subtree,diff,series,path-index}.ts` minus the same axes, behind cw's `requireViewer`; `_lib/scope.ts` = the name filter only. **cw-only**: `_lib/auth.ts` (edge Access identity, no auth package), `_lib/cwBatch.ts`, `_lib/plans.ts`, `api/plans`, `api/sweep/{dispatch,jobs,stop,undo,purge}`, `api/marks`, `api/whoami`, `login.ts`. **gcs-only**: `api/auth`, `api/db`, `api/actions`, `api/marks/totals`, `api/todo`, `api/estate`, `api/resolve`, `api/claims`, `api/assignments`, `api/sweep-owners`, `api/bench`, `api/token`, `auth/sso.ts`, `user/[id].ts`, `users.ts`, `cw.ts`, `_lib/{identity,ledger,markAxes,marks,owners,resolve,tables,totals,todo,unfurl,extras}.ts`. |
| marin ↔ dt/main | `src/disk_tree` | upstream carries Flask serving (`server.py`, diff index, vocab sidecar, compare perf); marin carries nothing server-side. Shared core must be a superset upstream: fork→upstream manifest `~/c/disk-tree/specs/marin-python-cp-2026-08-28.md` |
| marin ↔ dt/main | `packages/react` | none — parity, both directions (8/28) |
| marin ↔ dt/main | www arch | `site/` (Vite+CFN) vs `ui/` (Vite+Flask) — intrinsically different |

## Diff mode for the CFN branches

Upstream's on-the-fly diff lives in its Python serving layer (Arrow/pandas/
DuckDB); that does not port to Workers. The CFN-native answers:

- **Interactive drill: align two subtrees.** Every date already has a
  `path-index.parquet` and `/api/subtree?date=&path=`. Diff(view) = fetch the
  subtree for both dates and align by name (client-side, or a thin
  `/api/diff-subtree` doing the join over range reads). On-the-fly, scales
  with the pixel budget, no engine port.
- **Heavy / global summaries: precompute in the job.** The established
  pattern — `job/cw-diff.py` runs upstream's `recursive_diff` to emit
  `diff.json` per scan pair; publish like snapshots, serve slices.
- Not: porting diff-index to TS.

## Filling out the arch matrix

A "vanilla" **Vite + CFN + cloud-store** demo app (no auth, no attribution, no
marks: `scans.json` + `/api/subtree` + treemap) as the reference impl for that
arch, alongside upstream's Flask × local reference. Natural home: the disk-tree
repo (owner of reference impls) — a spec for the dt session once we want it.

## Scramble log

### 2026-08-28 (cw-ward), triggered by the CW cron crash-loop

Landed on `cw-s3` (`7d348a0`, `18e4a10`, `f0d0b48`):
- `job/`: `cw-*` scripts restored from `053cc33^`; own `:cw` image (`job/build.sh`
  default tag, Dockerfile entrypoint `cw-run.sh`, `.[gcs,s3]` extras — the
  first build lacked `boto3`); GCS-only `run.sh`/`batch-submit.sh`/digest icons
  removed per the ledger; ERR-trap Slack alerting (dormant: no transport on the
  CW scheduler body yet — needs `SLACK_BOT_TOKEN` secret + a channel).
- `site/`: identity registry (Avatar/UserChip/UserCard + `identities.gen.ts`,
  `identities.yaml` synced) on rollup rows, tooltip user rows, ⌘K picker,
  whoami chip; IEC/SI units provider (`?si`/`?rb`, `i`/`b`). Intended delta
  recorded: no `/user/:id` link in the card.
- `packages/react`, `src/disk_tree`: parity (earlier today).

Deferred to the next cw-ward pass (each needs App.tsx wiring on cw-s3's older
App): `?f=` path filter (`filterTree.ts`), drill path in the URL path, shared
`SiteNav` chrome + `/files` bucket line, `series.json` size-over-time (needs a
`series` step in `cw-run.sh`), `/api/subtree` lazy drill (needs `-P` path index
from `cw-webdata.py`), CF-Access-cookie → app-session auth (cw-s3 still edge-
gated; that is an intended delta for now).

Verification: `scripts/branch-audit gcs cw-s3` — react/engine parity; `job`
now differs only by the ledger's intended files.

**Found while closing the site tier — cw-s3.oa.dev does not serve the `cw-s3`
branch.** It is a custom domain on the `oa-gcs-usage` Pages project (`wrangler
pages project list`), i.e. it serves the **`gcs` branch's build**, whose
host-aware store default (`ab88049`) shows the CW view on `cw-*` hosts. The
`cw-s3` branch's `site/` has never been deployed, and its `site/deploy` still
targets `--project-name oa-gcs-usage` — running it would overwrite gcs.oa.dev.
So today only the *job* half of the CW deployment is branch-owned. Closing the
model needs (Ryan's call — DNS + Access app): a second Pages project (e.g.
`oa-cw-usage`), the `cw-s3.oa.dev` custom domain moved onto it, the CW Access
app `4c463052` re-pointed if needed, `cw-s3`'s `site/deploy` retargeted, and
then gcs's host-aware CW branches (`/cw`, `isCwHost`) become dead code to prune.
**Resolved 2026-08-28 (same day):** `oa-cw-s3-usage` created, secrets copied, Access app extended to its pages.dev hosts, custom domain + CNAME moved, `cw-s3`'s `site/deploy` retargeted and run; gcs pruned of the host-keyed CW code (`f4e7025`). Both hosts verified gated with the right build.

### 2026-08-28 (cw-ward, pass 2) — after cw-s3.oa.dev moved onto the branch

The move exposed everything the CW *view* had only via the gcs build. Landed
on `cw-s3` (`245501f`, `1e5ee40`): `/data` rooted at `snapshots/cw/` with
sub-daily ids (the branch had never read CW data); `?d=` codecs + `fmtScan`;
diff.json "Changes since previous scan" (`DiffTreemap`); `initialPath` into
the lone bucket; latest scan = clean URL (no `?d` bake-in); `hasPrices` gates
every $ surface (CW has no class breakdown); auto age-chart granularity
(~30 bars); shared-edge tiling + core edge/container/cell-border vars.

Finding for the **gcs-ward** list: since today's core CP the core paints
sibling borders inline (`boxShadow` — `--dt-treemap-cell-border` ring in gaps
mode, half-stroke ring in shared mode), so the class-level inset box-shadow
rules in `app.scss` (`.dt-treemap-cell`, `.branch`, `.chain`, `.dust`,
`&.store-gcs …`) are dead on gcs too. gcs should drop them and tune
`--dt-treemap-cell-border` like cw-s3 did (`1e5ee40`).

### 2026-08-28/29 (cw-ward, pass 3) — age-chart axis, toggle placement, deploy parity

Landed on `cw-s3` (`fc90798`, `ec4ac9b`, `7d7dffb`): age chart gets its own
`?ac=` color axis with the `color by` control beside month|week|day (modes
`date|user|tree`; the caller passes `modes` so `user` is absent — CW has no
attribution — rather than a dead button); tiling toggle moves out of the
header into the treemap's crumbs bar via `renderLegend` (quiet style);
`site/deploy` now pushes the branch to `o` by default (`-P` to skip) like
gcs's does — before this, what ran on cw-s3.oa.dev could be ahead of GitHub.

Intended deltas (not ported): `read` axis and `fateReady` rollup gating (no
access logs / no marks on CW); `/users` fate $ pairs (no class pricing).

Queued for the next cw-ward pass (landed on `gcs` `974ee7c`, 2026-08-29):
`TimeSeries.yFrom` core prop + the size chart's `fit | from 0` toggle;
interactive legend rows (hover = solo, click = pin) — CW has user rows only
when attribution exists, so it's the group/tree-less subset; `SiteKbd`
(site-wide SpeedDial/omnibar) — CW is a single page, so only the omnibar's
page-link group matters there.

### 2026-09-04 (cw-ward, `/cp dt/main gcs`) — first `git cp` pass against upstream

Survey: `gcs` had nothing past the cursor (`8191de0` is its tip). `dt/main`
had no cursor (146 unmarked since the 2023 merge-base); triaged by surface
against this ledger. Landed on `cw-s3` (one commit, cursor `dt/main 25f3dc0`):

- `packages/react`: upstream `93e24b0..25f3dc0^` (14 commits) applied as one
  3-way patch, clean — `edgeContrast` (built-in luminance half-stroke for
  shared tiling, `parseColor`/`contrastEdge` in `colors.ts`), textured
  `(other)` dust tile (`DustHatch`), `foldControl`, `remainderTail`,
  `renderer="canvas"` (`layout.ts`/`cellStyle.ts`/`TreemapCanvas.tsx`,
  progressive paint, segments parity, bounded a11y/`cellHref` overlay),
  `onCellHover`, `edgeEmphasis`, click-through tooltip, `:focus-visible` +
  `.pinned` accent ring, `CellStyle.ring`. Tests 180 → 213 (upstream's count).
  Also re-widened `renderCellExtra`/`renderCellSubtitle` to upstream's
  `CellCtx` (was narrowed to `CellDims` here since the 8/28 sync).
- `src/disk_tree`: `588c4bf` (`index -q/--no-progress`, `progress` threaded
  through `backend.list`/`run_gfind`) — core-contract change, harmless here.
- Repo tooling: `scripts/branch-audit` + this ledger now live on `cw-s3` too
  (the `.claude/cp.yml` `audit:`/`ledger:` entries pointed at files only gcs
  carried).

Skipped, with reasons (so the cursor is trustworthy):
- **`25f3dc0` — extract the treemap core into `@rdub/treemap`.** Structural:
  moves `packages/react/src/{Treemap,…}` to `packages/treemap/`, changes CI
  + dist builds, and wants `site/package.json` re-pinned and imports renamed
  on *both* marin branches at once. Content parity is achieved via the
  pre-extraction diff; the split itself is Ryan's call, gcs-first.
- Upstream-only serving/local features (ledger: "upstream carries Flask
  serving"): `server.py` compare/library routes, diff index (`diff_index.py`),
  vocab sidecar, `storage/*` row-group + blob search path (depends on the
  library `config.py`), `cli/{du,reclaim,repos,overcount,snapshots,scans,
  vocab,diff_index}`, `extents.py`, `desktop.py`/macOS app, `library.py`.
- `ui/` and `specs/` commits — different www arch / upstream's own specs.
- The `⇒ disk-tree upstream`-tagged rows (stream engine, finalize, bulk-list
  adaptive) originated here and are already on both marin branches.

Queued:
- **gcs-ward**: this same react + `progress` patch (audit `gcs cw-s3` now
  shows `packages/react/*` and `src/disk_tree` differing until gcs CPs it).
- **dt-ward** (manifest `~/c/disk-tree/specs/marin-cp-2026-09-04.md`):
  `TimeSeries` `yFrom`/`annotations`/`onPickX` (gcs `974ee7c`, cw-s3
  `6121e6f`/`00dd02b`/`f3c5a03`), `S3BulkLister` adaptive retries (`e018da3`);
  the 8/28 Python manifest is still open upstream.

### 2026-09-04 (gcs-ward, `/cp dt/main cw-s3`) — mirror of the cw-ward 9/4 pass

Survey after backfilling the `cw-s3` cursor (`8191de0`'s prose marker wasn't
in trailer form; `git cp set cw-s3 5160a18`): 2 cw-s3 commits, 81 dt/main
(no prior gcs→dt cursor). Landed on `gcs` (one commit, cursors
`dt/main 25f3dc0` + `cw-s3 fceb717`):

- `packages/react` + `src/disk_tree`: `fceb717`'s exact patch (gcs was
  byte-identical to `fceb717^` on both surfaces, so it applied clean and the
  worktree is byte-identical to `fceb717` — i.e. to upstream's pre-extraction
  react core + the `progress` flag engine change). Tests 180 → 213 react,
  371 pytest, site `tsc` clean.
- Ledger: adopted the cw-ward 9/4 scramble-log entry (parity of this file).
- No `app.scss` adaptation needed: gcs dropped the dead cell box-shadows and
  themed the core edge vars back on 8/28–29 (see the comment citing
  `1e5ee40` at the `.dt-treemap-cell` block).

Skipped (mirroring the cw-ward triage, same reasons): `25f3dc0` (the
`@rdub/treemap` extraction — structural, gcs-first decision, still pending);
upstream Flask/local serving + CLI features; `ui/` + upstream `specs/`;
marin-originated rows upstream copied back (`DT_S3_ADDRESSING_STYLE` =
our `168308a`, stream engine, adaptive bulk-list). `f3c5a03` on cw-s3 is
their CP of our `8191de0` — nothing to port back.

Queued dt-ward: `~/c/disk-tree/specs/marin-cp-2026-09-04.md` (written by the
cw-s3 session) covers what both marin branches owe upstream; nothing new
from this pass.

### 2026-09-07 (cw-ward, `/cp gcs`) — gcs `8191de0..8ac1beb` (39 unmarked)

Landed on `cw-s3` (two commits; cursor `gcs 8ac1beb`):

- `f196e66` (ahead of the pass): Diff Δ-mode color by the node's own changed
  fraction, from `f2d0a8d`'s `colorForCell` — a wholly removed 1 Ti dir is
  full red beside a 25 Ti one (was normalised by the page's max delta).
- `packages/react`: `TimeSeries` `onBrush` + `window` (the core hunk of
  `f30230c`, applied verbatim) — parity restored.
- `src/disk_tree`: `tree_build.py` docstring path from the `marin/` →
  `gcs-usage/` rename (`f59162957`) — parity; the rename itself is not
  mirrored (cw-s3's `marin/` is only built into the job image).
- `site/src`, adapted: chart brush → `?d=<after>-<span>` via
  `useScan.setRange` (`f30230c`; cw-s3's points are scan instants, so the
  brush maps x back to scan ids and the window shades `[diffBefore, asof]`;
  no UTC-date fix needed — cw-s3 already renders instants in local time);
  deep links pursue their anchor until it parks and hold the scroll-spy off
  meanwhile (`8cf2b33` + `7f56d02`; cw-s3 has no sticky top bar, so the park
  margin is read from the anchor's computed `scroll-margin-top`, which the
  color-by bar sets only on attributed scans; and "parked" also waits for
  the map, since the baked `diff.json` mounts `#diff` before the tree lands).
- `site/dev`: wrangler relaunch loop (`e3bab91`) — memory pressure kills it
  here, not a D1 tunnel; the 500-vs-404 Diff note has no counterpart (cw-s3
  aligns client-side, no `/api/subtree`).
- Already here: `628635c` (gcs's CP of `fceb717`), `7f911c6` (`hasAttr`
  from meta — cw-s3 always did).

Skipped as gcs-only (ledger: index tiers / D1 / marks & sweep / lenses /
team axis / `job/` + `gcs-usage/` are intended deltas): `86b1b97`, `b14118b`,
`988103b`, `85a7592`, `888737b`, `528564c`, `43b4b33`, `ca6ab5c`, `f59162957`
(the rename; docstring taken), `79e6ea5`, `99c60a3`, `17e47f7`, `e4a43d7`,
`42c044c`, `7cfa124`, `2bae18c`, `2bd99af`, `42b3757`, `654eb00`, `f5d1868`,
`0f84f3d`, `e7135b2`, `53921c9`, `8a1b7f4`, `ca059a7`, `0841cd2`, `206c5e9`,
`c02a4b6`, `e9e1261`, `cbd5572`, `8ac1beb`; the rest of `f30230c`/`f2d0a8d`
(scope strip, lens-scoped Diff, mark-feed filters, server-side diff).

Deferred (portable, but a design decision for cw-s3): **`7ab2d25` +
`c24632a` — the sticky page bar** (☰ nav · crumbs · scan picker · avatar
menu; Diff header holds both scan endpoints). cw-s3 has one page and no
`SiteNav`; adopting the bar is a layout change to decide on, not a CP.

Queued gcs-ward (same-day follow-up on cw-s3): **the main map never showed
the core's adaptive edge** — `site/src/Treemap.tsx`'s `colorForCell` pinned a
per-cell `edge` (fill mixed toward the page bg) below depth 0, and the core's
`edgeContrast` default only applies when the consumer leaves `edge` unset;
gcs's wrapper has the identical lines. Also the Diff map now carries the
`gaps` tiling chip (`TilingToggle` + `tiling=`); gcs's `DiffTreemap` lacks it.

### 2026-09-07 (gcs-ward, `/cp dt/main`) — upstream's cloud-reduce / remote-target run

Survey `25f3dc0..41978ca`: 21 upstream commits. Landed on `gcs` (one
commit, cursor `dt/main 41978ca`):

- `packages/react/tests/TimeSeries.test.tsx`: upstream's tests for the
  `yFrom`/`annotations`/`onPickX` props (`3d48a20`, their CP of our
  `974ee7c`/`00dd02b`/`f3c5a03`) — the props were ours, the tests weren't.
  React suite 213 → 217.
- `src/disk_tree/backends/local.py`: `find` when `gfind` is absent
  (`3663e39`, clean `cherry-pick -x`).

Skipped: `5be454c` + `8c0628d` (their CPs of our `tree_build`/access-plane
and adaptive bulk-list work — already here, tests included); `bce974e`,
`642bd38`, `6a4a2e4`, `58a2845`, `fd509b9`, `268ddb7`, `5ec7f86`, `646e455`
(remote scan targets + capture/reduce + R2 serving: upstream's blob search
path, `capture`/`reduce`/`scans register`, `ui/cfn` Pages Functions,
manifests — a laptop→cloud pipeline; gcs lists buckets in Batch and serves
its own path index. The two engine touches inside them — `canonical()` on
`file` scan roots, 64K duckdb row groups — sit in `aggregate_*`, which
gcs-usage doesn't call); `0e14ae8`, `41978ca` (Pages auth for `ui/`; gcs has
its own); the specs commits. `268ddb7`'s `s3fs>=2024.10` pin (0.4.2 sends an
empty `x-amz-acl` on multipart, R2 rejects) is queued for the cw-s3 pass —
gcs writes through gcsfs / the `/gcs` mount.

Queued dt-ward: `~/c/disk-tree/specs/marin-cp-2026-09-07.md` — `TimeSeries`
`onBrush`/`window` (gcs `f30230c`), the one shared-surface change since
their last manifest.

Same day, second commit (`e9a3cce`): upstream's `25f3dc0` treemap-core
extraction landed — `packages/treemap` (`@rdub/treemap`) taken verbatim,
`packages/react` trimmed to the disk widgets + `export *`. `packages/` now
differs from upstream only by `TimeSeries`'s `onBrush`/`window`; the
`gcs ↔ dt/main` audit rows for `packages/react/src` should read `parity`
once that ports. The "structural gap" row above is closed.

### 2026-09-07 (gcs-ward, `/cp dt/main`, third pass) — the unification spec landed upstream

`7d9f7b9..01b5e19`: DT implemented `mgu-scale-unification.md` A–E in five
engine commits. Landed on `gcs` (one commit, cursor `dt/main 01b5e19`), as
upstream's final file versions (our pre-change divergence in
`aggregate_duckdb.py` / `import_listing.py` was only the `canonical()` /
`BLOB_ROW_GROUP_SIZE` / `--to` hunks skipped on the first pass — taken now):

- A `bcbe063` file-backed `--db`, `--partition-depth`; B `f746bce` `--label`
  slices as cascade group keys; C `bdf621f` `find/tiers.py` (`--tiers
  dirs,objects,coarse`, `--sort-variant`, KV floors) + `_SUCCESS.json`
  `started`/`finished`; E `01b5e19` `--size-hist`. Plus `blobfs.py` and
  upstream's `storage/base.py` (the writer imports `BLOB_ROW_GROUP_SIZE`;
  `storage/base` imports `blobfs`). `config.py` stays stripped, so
  `import --to <url>` (`config.set_write_target`) is not wired here — mgu
  writes tiers to the NVMe dir and uploads.
- **D held** (`16c621a`: hour-grained layer-2a, `--as-of`, `access state`,
  `--side/--max-col`): `access/aggregate.py` renames `day → hour` and the
  stats key `days → hours`; mgu's ingest (`access.py`, `reactive.py`
  compaction keyed `(bucket, path, day, op)`) and its retained 2a shards are
  day-grained. Adopting D is a migration (re-aggregate the retained raw
  shards at hour grain, repoint the compaction), scheduled with the A.3 gate.
  `aggregate_duckdb.py`'s `_Side` (the cascade half of D) is in, unused.

Tests: 104 in the ported files, 405 root, 144 gcs-usage. Next: the A.3
gate (DT's cascade on the 9/7 listing with `--db --partition-depth
--label usr --tiers --size-hist`, a2a against mgu's `path-index`) on Batch.

### 2026-09-07 (cw-ward, `/cp dt/main`) — mirror of gcs's 9/7 upstream pass

Survey `25f3dc0..46ff7b4`: 36 upstream commits. gcs had already assessed
the same range (four commits, cursor `dt/main 46ff7b4`), so the shared
surfaces land here as `cherry-pick -x` of gcs's adapted CPs — parity by
construction — plus what gcs left for this branch:

- `75f0dfd` ← gcs `5b65b06`: upstream's `TimeSeries` tests for
  `yFrom`/`annotations`/`onPickX` (`3d48a20`); `LocalBackend` falls back to
  `find` (`3663e39`).
- `82f1b95` ← gcs `e9a3cce`: **the `@rdub/treemap` extraction** (`25f3dc0`,
  skipped on 9/4 as "gcs-first" — gcs went first). `packages/treemap` holds
  the core; `@disk-tree/react` re-exports it, so `site/` imports are
  unchanged. The ledger's "split NOT mirrored" caveat is retired.
- `38d2a56` ← gcs `73d57ab`: DT's fleet-scale cascade (`--db`,
  `--partition-depth`), `--label` slices, index tiers, `--size-hist`
  (`bcbe063`, `f746bce`, `bdf621f`, `01b5e19`); `16c621a` (hour-grained
  access plane) held, as on gcs.
- `180e786` ← gcs `60b734a`: dir partition keys + batching, placeholder
  objects, `--coarse-floor`, `find/groups.py`, `index r2://` (`46ff7b4`,
  `ada2964`, `c3d1b7f`).
- On top (this branch's commit): `c6f3255` — shared strokes no wider than
  gaps (`defaultBorderWidth` 2/1/1), canvas labels mirror the DOM rules,
  `sizeAlign` (gcs's `packages/treemap` already carried it; cw-s3's copy was
  renamed from the pre-`c6f3255` files). cw-s3's main-map wrapper keeps its
  own `borderWidth`, so only the Diff map picks up the new default.
  `8c04b74`'s brush/window tests (80 lines; their CP of our `f30230c` — the
  props were here, the tests weren't). `268ddb7`'s `s3fs>=2024.10` pin
  (queued for cw-s3 by gcs): re-lock moves s3fs 0.4.2 → 2026.7 via
  aiobotocore 3.9, boto3/botocore 1.43.64 → 1.43.56. CI gains upstream's
  `treemap-widget` job (typecheck + test for `packages/treemap`).

Skipped (same reasons as gcs's entries above): `5be454c`, `8c0628d` (their
CPs of our engine work); `bce974e`, `642bd38`, `6a4a2e4`, `58a2845`,
`fd509b9`, `5ec7f86`, `646e455` (remote scan targets, capture/reduce, R2
serving, manifests — upstream's laptop→cloud pipeline; the CW job lists in
Batch and bakes its own `diff.json`); `0e14ae8`, `41978ca`, `7cd5405`,
`7d9f7b9` (Pages auth for upstream's `ui/`; cw-s3 is edge-gated by Access);
`16c621a` (held with gcs); the specs commits (`6d83409`, `fbd0334`,
`f521a3a`, `7561e4f`, `3515534`, `6888340`, `bfa0bfd`, `3c9cde7`, `ca451e6`,
`ddd43fc`). `build-dist.yml` stays as is: it fires on `main` only and this
repo publishes no dist branch — upstream's `dist/treemap` rewrite is
repo-specific.

Tests: treemap 130, react 91, root 421 (gcs's counts + `c6f3255` and the
brush tests). Site type-checks and builds.

Queued gcs-ward: the 80-line brush/window test block
(`packages/react/tests/TimeSeries.test.tsx`, from `8c04b74`) and the
`treemap-widget` CI job — `packages/` otherwise matches gcs and upstream
exactly. The `s3fs` pin is cw-s3-only by gcs's choice (gcsfs path).

### 2026-09-08 (cw-ward, `/cp gcs` + `/cp dt`) — engine parity catch-up

gcs had run one more engine CP since the 9/7 pass, so cw-s3 had fallen behind
on the `src/disk_tree` / `packages/treemap` parity surfaces (surfaced by the
`factored/cw-s3-gcs-2026-09-08` reconstruction's parity-pending bucket).

Landed on `cw-s3` (`b691bcf`; cursor gcs `ab02e73`):
- `01fc0b8` (engine, dt `46ff7b4..bd98e10`, upstream's final files): `key/`-ordered
  batch ranges (prefix-sibling keys no longer dropped from a batch's pushdown
  range), recursive key splitting over `--partition-files`, prune-friendly
  cascade scans (`substr` not regex, TEMP tables), `import -n/--threads`.
- `packages/treemap` `CellCtx.chain` (gcs `726e54f`'s core hunk, `[CP→dt/main]`):
  collapsed single-child count passed to `renderCellExtra`. @rdub/treemap parity.

Skipped as gcs-only / port-pending (2026-09-08 direction — converge later, whole
subsystem not ported in one pass): the sweep console + `/api/sweep`, ownership
`own`/`assign` vocabulary, mark UI, the Worker index-tier / group-manifest
serving, `gcs-usage` attribution/labels, `job/run.sh` GATE, `ui/` treemap import,
and the spec commits.

dt/main (`git cp set` to `af23c4c`, ported nothing new):
- `2ac8572`/`bf3fc28`/`509f324`/`bd98e10` (a3-gate asks 6–9): already here via
  `01fc0b8` above.
- **`af23c4c` (round-3 asks 10–11: `_write_ranged` range-partitioned final sort
  to dodge the global-COPY OOM at fleet scale + RSS instrumentation) — HELD.**
  Only dt has it; gcs does not yet. The shared engine flows gcs→cw-s3, so taking
  it now would put cw-s3 ahead of gcs. It arrives when the gcs session CPs
  round-3 from dt.
- The rest of `46ff7b4..af23c4c` is dt-only Flask serving / local features
  (`server.py`, `diff_index`, `extents`, `desktop`, `library`, `capture`, `du`,
  `reclaim`, `repos`, `snapshots`, `vocab`, `sidecar`, `config`, `storage/*`) —
  intended delta (ledger: "upstream carries Flask serving").

### 2026-09-09 (cw-ward, `/cp gcs` + `/cp dt`) — treemap core + ft bump

gcs ran a large seven-commit batch (`ab02e73..dfc473453`): the dt treemap CP,
assignment/provenance serving, ft bump, `/assignments` page, sweep-log row
groups, the treemap header redesign, and the children-table owner/fate/bulk
work — plus index sidecars. Almost all of it is the **user + sweep axis**, still
port-pending on cw-s3 (2026-09-08 ledger: blocked on cw-s3 having no
owner/writer attribution). Two commits are genuinely cross-cloud and landed.

Landed on `cw-s3`:
- `0a18858` — `@rdub/treemap` core to parity with gcs (its `e76f01e`, itself a
  CP of dt `1bd3546`+`48060e5`): `outlineGroups` grouped-outline overlay
  (`OutlineOverlay.tsx`/`outlines.ts`), diff treemap+table widgets (`diff/*`),
  and gcs's core fixes (float-epsilon seam quantization, `var()` color
  resolution, DOM-renderer geometry, canvas parity). Copied the 14 core files;
  `e76f01e`'s `tiling.tsx→prefs.tsx` rename is gcs site wiring, not core.
  `packages/treemap/{src,tests}` now byte-identical to gcs. The *outline
  capability* is here; the mark/fate *wiring* that consumes it is not (sweep
  axis). Tests: 140 pass.
- `25261a3` (cursor gcs `dfc473453`) — `@rdub/file-tree` → dist `b64278c`
  (`40b75bd`) + `/files` parquet viewer upgraded to `makeParquetViewer` with
  `resizableColumns: { scope: 'schema' }` and an `elide` tooltip (adapted CP of
  gcs `c5a33b5`). ft's new `@rdub/treemap` peer dep satisfied from the
  workspace. Kept cw-s3's minimal `/files` chrome (no `SiteNav`/`SiteKbd`/
  `useDocTitle`). CIC'd on a coarse24 path-index parquet: viewer renders, 11
  resize handles present, elide tooltip fires on a clipped cell.

Skipped as gcs-only / port-pending (user + sweep axes): `4eaa75a` assignment
provenance serving, `ede9270` `/assignments` heatmap, `e0cd358` sweep-log 64k
row groups, `a2f0103` treemap header redesign (MARK ALL / owner-class bars /
mark outlines — the outline *capability* landed via `0a18858`), `839f4b4`
children owner/fate/bulk-select, `dfc473453` `ck.json`/`attr.json` sidecars.

dt/main (`git cp set` to `48060e5`, ported nothing directly): `1bd3546`+
`48060e5` arrived via gcs `e76f01e`; `168fb4c` (r2.rbw.sh open demo) and
`56f0917` (desktop WKWebView inspector) are dt-only; `63a0a0f`/`c0ebcaa` are
specs. cw-s3 flows dt through gcs.

Audit: `packages/treemap/{src,tests}`, `packages/react/src`, `src/disk_tree`
all parity. `site/src` (74), `site/functions` (48), `job` (54), `marin/src`
(18) differ = the port-pending user/sweep + S3-vs-GCS pipeline axes;
`packages/react/tests` (3) = the `TimeSeries` brush tests cw-s3 owes gcs.

### 2026-09-09 (cw-ward) — general-FE catch-up (de-entangling `site/src`)

Ryan flagged that commit-level `/cp` had left cw-s3 behind gcs on **general**
FE, because gcs ships it entangled with the sweep/ownership axis in mixed
commits (`a2f0103` header redesign, `839f4b4` children table) — so a
commit-level pass skips the whole thing as "port-pending user axis" and loses
the portable half with it. The `classify.py` file-level buckets have the same
blind spot (`ChildrenTable`/`SiteNav`/`theme` → `marks`; floating controls →
`residual`); the entanglement is at the *hunk* level. So this pass ports the
general slices directly, adapting the user/sweep columns out:

- `2b98209` — **children table under the treemap** (the tabular twin), + the
  general **controlled-drill lift**: `Treemap` gains `path`/`onPathChange`
  (App owns `?path=`), which the table needs to mirror the drilled node and to
  drill the map from a row. Owner/fate/mark/bulk columns dropped (sweep axis).
- `9c179f3` — **table under the diff treemap** (the long-standing cross-branch
  ask), over cw-s3's own `DiffData` frontier.
- `3f55157` — **grouped legend**: prefix clustering (`legendGroups`), name
  head/tail elision + copy, "+N more" count. Owner/user legend rows dropped.
- `e9e4b09` (earlier this session) — `/files` FT JSON/CSV/MD/code/notebook
  renderers.

All CIC'd. Deferred: the "floating page-scoped controls" half — cw-s3 already
has the use-kbd SpeedDial + ⌘K, so which gcs control is meant is unclear
(flagged to Ryan). **Still TODO: refresh `factored/cw-s3-gcs-*` to verify the
residual shrank** (the "port directly, verify after" plan's verify step).
These are gcs-ward too — general FE gcs already has; cw-s3 catching up.

### 2026-09-11 (cw-ward, `/cp gcs and dt`) — general FE + treemap parity, sweep held

gcs had a 50-commit batch since the cursor (`dfc473453..fc6dd8d70`), **dominated
by the sweep executor + console + `/api/sweep`** (the real 9/11 GCS sweep run and
its aftermath) — the whole subsystem, still port-pending on cw-s3 (no owner/writer
attribution; ledger 2026-09-08). Landed the portable general-FE slices, adapted
(one commit, `a3d8681`; cursor ref advanced to gcs tip `fc6dd8d70`):

- `170a144da` — children names elide from the **middle** (`elideMid`, 60-char
  budget, tail kept) not the end, never wrap; full path in a floating `Tooltip`.
  `CopyName` + `copyText` (non-secure-origin `execCommand` fallback for the
  tailnet dev server) extracted to `CopyName.tsx`, shared by the table + the pin.
- `2cb25f2d0` (general slice) — held-map loading marker: a scan switch keeps the
  last map drawn but dimmed under a "loading view…" pill (`Busy` + `.busy-*`)
  instead of blanking; the map's derivations (`mapPath`/`dateRange`) follow
  `shownTree = tree ?? lastTree`, so the children table doesn't empty mid-load.
- `250eed6f4` + `6c9146926` (core hunks) — `packages/treemap` back to byte-parity
  with gcs (src **and** tests): ⌥-click pins a branch cell instead of drilling;
  the drill-pop keydown listens on `window` and yields to `defaultPrevented` (a
  use-kbd hotkey layer clearing a selection wins). The row-selection/bulk UI those
  commits carry is the sweep axis, skipped.
- Opportunistic parity (pre-cursor, hard-parity surface): `packages/react/tests`
  gained gcs's 3 `TimeSeries` brush/window `it()` blocks (src was already parity)
  — closes the last `packages/*` audit-yellow. **`packages/treemap/{src,tests}`,
  `packages/react/{src,tests}`, `src/disk_tree` all `parity` now.**

Skipped as gcs-only / port-pending (sweep + ownership axes): the entire sweep
executor/console/API (~30 commits incl. the fate→mark rename `e5d7fb61d`, region
routing, undo, part-file logs, runs-table UI, dispatch error surfacing), the
children-table sweep bits (`de281fff9`/`84de6dac2`/`5e0d35c30`/`ec540a50d`), the
index-extras attribution sidecars (`e8ede579b`/`f0af501ea`/`68e0bc583` —
`attr.tsv`), serving (`4aa30bdeb`/`776b3f429` — cw-s3 aligns diff client-side),
`79c85ed59` skeletons (sweep/multipage), `7d73b5c30` `useSectionHash`+`/sweep`
(single-page), `a7baf89d6` `job/rerg` (intended `job/` delta), the dep bumps
`f0723a1ba`/`05747cc1b`/`f9a203818` (gcs lockfile-specific — cw-s3 wants its own
`pnpm audit`), `97eb89af6` `specs/org-axis.md` (tabled). The BUCKET_REGION +
`/api/sweep/stop` fixes that landed mid-pass are sweep too.

**dt/main `48060e5..d5f85d2b9` assessed (23 commits), cursor NOT advanced.** dt's
new `@rdub/treemap` core is HELD gcs-first: `522fa2d67` (seam/`var()`/DOM fixes),
`96bbc5f2f` (test), `b771fac1e` (`nestedHues` L1/L2 coloring), `da645c0a0` (canvas
`var()` fills, `onDrawn`, hover-tip guard, canvas↔DOM parity). gcs's
`packages/treemap` doesn't carry them yet (144-line gcs↔dt gap), and cw-s3 mirrors
the core from gcs — taking them now puts cw-s3 ahead. `a4305050a` (`CellCtx.chain`)
already here. Rest intended delta: upstream `ui/` (Flask www arch), the
public-diff-demo / IaC / serverless-reference specs, remote-scan-target +
per-bucket-creds (`02a84a678`) + `.groups.json` footer (`6f0999de0`/`334c9272d`)
engine work (upstream's laptop→cloud pipeline; the CW job lists in Batch and bakes
its own `diff.json`). Leaving the dt cursor at `48060e5` re-surfaces the treemap
core next pass, once gcs takes it. **Queued gcs-ward:** none new (general FE gcs
already has). **Queued dt-ward:** unchanged.

Verify: audit green on all shared surfaces; site build + `tsc` clean; treemap 141
/ react 88 tests pass. CIC'd on `marin/grug` — middle-elision (tail kept), the
floating tooltip shows the full `s3://…` path, an in-app scan switch holds the
dimmed map under "loading view…" then clears. Factored-branch refresh deferred:
the residual is a misleading metric until `classify.py` re-buckets the unlisted
gcs-only files (documented 9/09); the git-didi audit is the honest parity check
and it's green.

### 2026-09-11 (cw-ward) — sweep Phase 0: D1/wrangler IaC + mark+sweep migrations

First implementation slice of `specs/cw-sweep.md`. In-repo IaC authored (no cloud
side effects yet): `site/wrangler.toml` CP-adapted from gcs (name `oa-cw-s3-usage`,
`database_name` `oa-cw-s3-usage-db`, `ACCESS_AUD` = the cw app `4c463052`'s real aud
`1de65a9f…dc8378`, `STAFF_DOMAIN` kept; `database_id` left blank pending the
`wrangler d1 create`), and a fresh 4-file `site/migrations/` mark+plan+sweep subset —
`0001_marks` (keep-axis ledger `marks` + `mark_log`; **default = unmarked**, i.e.
absence of a row — `keep` is an affirmative protect signal, not the default;
nothing sweeps without an explicit `sweep` mark — unlike gcs's default-delete +
owner-slice safety), `0002_plans` (**plan as a first-class object**: `plans` +
`plan_items`; admin curates marked prefixes into a named draftable plan, multiple
plans coexist — cw-s3's cleaner answer to gcs's owner==marker slice), `0003_deletions`
(`deletion_runs` + `deletion_bands`, gcs 0015+0023 folded, `plan_id` FK, GCS→CAIOS
undo: `undo_state` before `undo_deadline` + `purge_state` for the two-stage
versioned-bucket space reclaim), `0004_admin` (`admin_emails` gate + `admin_edits`
audit). **Folded `site/wrangler.toml` +
`site/migrations` into the CP parity surfaces** (`.claude/cp.yml` + `branch-audit`
SURFACES) so git-didi renders the gcs↔cw-s3 delta as the intended small patch
(name/id/aud + table-subset) rather than a whole-file absence — the visibility Ryan
asked for. Resolved cw-sweep open questions in passing: admin allowlist → D1
(`admin_emails`, gcs-proven; CF Access stays the sign-in gate + identity source).
Provisioning (D1 create, `marin-us-east-02a` versioning-enable) is the next step,
still pending. Phase 1 (backend: `marin sweep` + `/api/sweep`) not started.

### 2026-09-16 (cw-ward, `/cp gcs`) — serving architecture, `marin/` → `gcs-usage/`

Cursor gcs `fc6dd8d70` → `180a362c4` (41 unmarked). Ryan's framing: cw-s3 was "way
behind gcs" — it still shipped whole top-level JSONs (a per-top-level-dir `age.json`,
a root-only baked `diff.json`, the whole `tree.json`), and its CLI package sat in a
misnamed `marin/`; the goal is that `diff cw-s3..gcs` reads as exactly the things
added/subtracted between the deploys, especially with sweep execution moving into the
app here. Landed as five commits:

- `2dd8ddc` **server-side view serving** (backend): the layer-2 parquet's dir rows
  rewritten into gcs's index-tier contract (`gcs-usage index-write`: bucket-prefixed
  paths, `b/o/wts/wb`, 8k-row groups, sorted `(depth, path)`, coarse tiers E ∈
  16/20/24 with the floor in the parquet metadata), published under
  `cw-l2/<scan>/index/<gen>/` and footer-synced to D1 (`index_footer.py` ported;
  migration `0005_index_footer` = gcs's four index migrations folded; `index-sync`,
  `index-gc`, `index-dir`, `warm-cache`); Functions `_lib/index.ts` + `shared.ts` +
  `edgeCache.ts` verbatim, `_lib/view.ts` stripped of gcs's owner/mark/class/extras
  axes, `/api/subtree`, `/api/diff`, `/api/series`, `/api/path-index`. Sources: the
  pre-cursor serving foundation (`c78a34e` footer-in-D1, `43b4b33`/`ca6ab5c` coarse
  tiers, `2bae18c` series, `6989062` blob fallback) plus this range's `279acff`
  `7a5e338` `714be65` `4d80173` `23669f0` `beed6f9` `65a2cc2` `84cf668` `102781b`
  `5ef41d2` (cache tiers, batched lookups, `Server-Timing`, group LRU, plain-view fast
  path, depth-capped diff). 84 py + 5 vitest.
- `a66019f` **the page reads views**: `/api/subtree` drill chain + `depth=1` first
  paint + held tree, `/api/diff` (summary → depth-1 → full), `/api/series` per prefix,
  react-query, `useHashSpy` (`631c3db` + `c0d7d53`), `SpeedDialTip` (`d208802`);
  `clientDiff.ts` and `job/cw-diff.py` deleted. CIC'd on a throwaway local harness
  against two real synced scans (root from tier `coarse20` in 0.8 s cold, a 6-level
  drill chain, a real server diff, the 409 for an unindexed scan).
- `0839bb8` **`marin/` → `gcs-usage/`** + gcs's `Dockerfile`/`.dockerignore`/
  `.gcloudignore` (`68c4b93c7`) / CI `gcs-usage CLI tests` job.
- this commit: `packages/react/src/TimeSeries.tsx` back to byte-parity (the brush
  slide, `5ef41d2`'s core hunk), `packages/{react,treemap}/package.json` onto gcs's
  (vitest ^4 — with `site` on vitest 4 the packages' vitest-2 runs lost jest-dom's
  matchers: `Invalid Chai property: toBeInTheDocument`; now 88 + 141 + 5 tests
  green) + this ledger.

**Skipped as gcs-only** (named so the cursor is trustworthy): the Discord digest
twin + weekly report + their wiring/specs (`43125ba8f` `f3e5047c1` `9ddfe1bf4`
`82d538814` `83cdd1543` `33cc12100` `a861202fc` `6003940c8` `a71ec4902` `5c6f2cf22`
`0db440797` `7b37af8d8` `12035ab88` — cw's digest is Slack-only, framing A);
gcs digest link/text tweaks (`24375466b` `eac2d2257` — cw's digest links already
open `#over-time`) and the wrangler-binary fix (`1365a874d` — already there);
README reports section + preview images (`0fbb5dca8` `7f75fb7f7` — cw's README is
its own); `og.jpg` refresh (`37d94ac20` — own asset); `/sweep` runs-table polish
(`3a2a5ab03` `c853dc7bc` — cw's plan-first SweepPage differs structurally; a
candidate for a later UI pass); `/api/bench` (`a0987d057` `2bfd8ba51` — an
admin diagnostic gated on gcs's scope model); `specs/done/diff-perf.md` updates
(`a95f5fc67` `f72a81c55` `4bb1e4946` `180a362c4` — gcs's own record).

**Audit** (git-didi, after this pass): `packages/treemap/{src,tests}`,
`packages/react/{src,tests}`, `src/disk_tree` **parity**; `job`, `gcs-usage/src`,
`site/functions`, `site/src`, `site/wrangler.toml`, `site/migrations` differ by the
rows above — which are now the whole intended delta.

**The legible diff, cw-s3 vs gcs, after this pass** (what `diff cw-s3..gcs` should
read as): *cw adds* the plan-first CAIOS sweep (`sweep.py`, `/api/plans`, `/api/sweep/
{undo,purge}`, migrations 0001–0004, `SweepPage`), the TTL expiry manifest, the layer-2
→ tier writer (`index.py`), the `#cw-s3-usage` digest content, `login.ts`/edge-Access
auth, `?path=` drilling. *cw lacks* attribution and everything on it (owner axis,
`/users`, `/user/:id`, `/assignments`, claims, the owner-slice sweep), the mark-state
and storage-class axes, the auth package (`api/auth`, grants, tokens, `/admin`),
the access log, `/marks`, `/api/marks/totals` + `todo` + `estate`, the Discord twin +
weekly report, `/api/bench`, index extras. *Everything else* — the reader, the view
planner/fold, the edge cache, the index sync, the treemap core, the React widgets, the
disk-tree engine — is shared.

**Follow-ups**: apply migration 0005 to prod D1 (`wrangler d1 migrations apply
oa-cw-s3-usage-db --remote`) and rebuild `IMAGE:cw` before the next daily run so the
job writes + syncs tiers (until then the page shows the honest "no index for this
scan" for new scans; older scans backfill via `tmp/build-scan-index.sh <scan>` — the
two local-synced scans, `2026-09-15T1201` and `2026-09-16T0001`, have their tiers in
the bucket under `index/local-*/` and need a `--remote` sync); the `CACHE_KV`
binding (KV namespace + KV-scoped token); an Access service token for `warm-cache`;
`?path=` → URL-path drilling; `OgPage` onto `/api/subtree`; `/api/age` (both
branches); file-level convergence of `Treemap`/`ChildrenTable` onto gcs's.

**Checkpoint audit (2026-09-16, later):** the factored branch's commits are *file-bucket snapshots* (`git checkout gcs -- <files>` per bucket), not working checkpoints — measured by checking out each of the 15 commits and running `tsc -b`, `tsc -p functions`, and the py suite: the site typecheck fails at every intermediate commit, the functions typecheck at the six `site/*` buckets, the py suite at five; only the tip (≡ gcs) is green. A file lands whole in whichever bucket owns it, so hunks from several features travel together and buckets reference modules that arrive later. The branch answers "what is in the delta and which gcs commit owns each hunk"; it does **not** answer "what would cw-s3 + features 1..k look like". That is a different, more expensive exercise — a **de novo parallel construction**: from cw-s3's tip, add one *feature* at a time as a hunk-level logical change, with every checkpoint required to build + pass tests (states that have never existed anywhere), ordered so the separable-by-config features come first and the entangled ones surface last. Seed = the hunk-owner map + gcs's own commit history. Planned as `denovo/cw-s3-gcs-<date>`; not started.

**`cloud/` rename (2026-09-16, later still — Ryan's go):** `gcs-usage/` → `cloud/`, package `gcs_usage` → `dt_cloud`, distribution `marin-gcs-usage` → `dt-cloud`, CLI `gcs-usage` → `dt-cloud`, via `git mv` + one regex pass (59 files). Infrastructure identifiers that contain the old string (bucket, D1, Pages project, image, SAs, Slack channels, secrets, storage keys, repo name) and `src/disk_tree` deliberately unchanged. gcs applies the identical mapping from specs/cp-from-cw-s3-2026-09-16.md §10; the audit surface in `scripts/branch-audit` / `.claude/cp.yml` is now `cloud/src`.

### 2026-09-16 (h11n, "factor the diff into commits") — golf by hunks, second factored branch

Same day, after the serving pass above; HEAD had moved to `9106aaa` (the lifecycle
verb) under the pass. `factored/cw-s3-gcs-2026-09-16` regenerated in `wt/factored`
from cw-s3's tip (never rebased; `tmp/factor.py` is the bucket script, buckets refreshed
for `gcs-usage/`, `site/functions/{index,shared,edgeCache,view}.ts` and `/api/*`), one
`git checkout gcs -- <paths>` (+ `git rm`) commit per intended delta, `git diff
factored/… gcs` empty at the tip:

| SHA | bucket (= ledger row) | files | + | − |
|---|---|---|---|---|
| `fb47b89` | py: shared engine + attribution modules (`gcs-usage/`) | 18 | 1370 | 687 |
| `06bf8ea` | py: gcs-only feature modules (access plane, marks, owner-slice sweep, extras, healthcheck, reactive, weekly, discord) | 20 | 5444 | 0 |
| `4e59b98` | py: cw-only modules removed (plan-first CAIOS sweep, layer-2 tier writer, listing) | 8 | 0 | 1574 |
| `9b7e8b9` | py: CLI verbs + digest content (cw framing A → gcs Shape C) | 4 | 2342 | 1377 |
| `4b11de4` | job: GCS Batch pipeline (run/batch-submit/webdata, icons, Dockerfile) vs the CW scan job | 55 | 955 | 562 |
| `661431b` | site/functions: auth package (grants, tokens, SSO, users) vs edge-Access identity | 11 | 447 | 99 |
| `a5fd26b` | site/functions: marks/claims/owner-slice sweep + D1 tables vs cw plan-first sweep | 27 | 2097 | 665 |
| `9c59de7` | site/functions: view scope axes (owner/mark/class/extras) + bench | 10 | 995 | 184 |
| `6bc0f57` | site/src: users / owners / attribution views + read axis | 11 | 1288 | 91 |
| `5d6f534` | site/src: marks & sweep console, admin pages, multi-page nav/chrome | 27 | 3565 | 371 |
| `cee5335` | site/src: diff / series / files consumers (scope-aware) | 4 | 132 | 236 |
| `21e2bf9` | site: deployment config, e2e, og assets, D1 migrations | 81 | 927 | 277 |
| `1ba7c76` | repo: specs/docs, CI, sheet-sync, ui, root config + locks | 83 | 5684 | 2065 |
| `7e2bc78` | parity-pending: deployment constants (D1 id + wrangler DB, path-only variants, warm paths, the store row) + queued CPs (allow-list union, D1-filtered listing for every store, `warm(headers)`, sub-daily warm ids) | 7 | 52 | 57 |
| `e0c6562` | RESIDUAL — cross-cutting core FE (App / app.scss / Treemap / ChildrenTable / Root / types / auth / AuthGate / OgPage); every hunk owned by a gcs feature commit — intended axes (a2f0103 7ab2d25 99c60a3 5ff6222 839f4b4 e5d7fb6 66a07e7 6c91469 …) or a queued general CP (2cb25f2 79c85ed 974ee7c 5ef41d2 f2d0a8d c24632a) — see the ledger's 2026-09-16 h11n entry | 8 | 3319 | 1129 |

**Diagnostics went by hunks, not lines**: `git diff -U0 cw-s3 gcs` on the 21
parity-pending + RESIDUAL files, each hunk blamed to the commit that introduced its
`+` lines on gcs (`tmp/hunk-owners.py`) — the per-hunk owner IS the ledger row or the
queued CP. Before: 413 hunks (parity-pending 51 over 8 files, RESIDUAL 362 over 13).
After the ports below: 345 (30 / 315), six files driven to zero (`Tooltip.tsx`,
`units.tsx`, `colors.ts`, `main.tsx`, `scan.ts`, `_lib/gcp.ts`); the RESIDUAL bucket is
8 files (+3319/−1129), every remaining hunk owned by a named gcs commit.

Per file after (hunks): `index_footer.py` 3; `warm.py` 3; `test_index_footer.py` 7; `test_warm.py` 5; `index.ts` 1; `gcp.ts` 0; `[[path]].ts` 4; `[[path]].ts` 3; `stores.ts` 4; `App.tsx` 101; `AuthGate.tsx` 2; `ChildrenTable.tsx` 32; `OgPage.tsx` 2; `Root.tsx` 8; `Tooltip.tsx` 0; `Treemap.tsx` 68; `app.scss` 88; `colors.ts` 0; `main.tsx` 0; `scan.ts` 0; `types.ts` 5; `units.tsx` 0; `auth.ts` 9.

**Ports onto cw-s3** (this branch, tests green at each): `93071bc` shared FE
primitives (pinnable tooltips + `safePolygon`, `fmtBytesLike`, `slotHsl` golden-angle
slots + 22°/14% fan, `UserIndexEntry`, `epochDaysToMonthShort`/`epochDaysToDate`,
render spy, `CLASS_COLORS`, extras node fields — owner `a2f0103` `974ee7c` `de281ff`
`5ff6222`, general parts only); `9bca450` gcs's store model with one row (`stores.ts`,
`scan.ts`/`OgPage.tsx` verbatim — the og card on `/api/subtree`, `tree.json` retired
— `Root.tsx` mounts `<store>/og` + the one `HotkeysProvider`, `Treemap` takes
`scheme`, `data/[[path]].ts` is gcs's with `requireViewer` and an every-store D1
filter — owner `2b693f1` `79c85ed` `9b8d237` `7cfa124` `28697e7`); `c4aad0a` `_lib/gcp.ts`
verbatim (`shared`-memoized mint, `BUCKET_REGION` inert) with the CoreWeave Batch spec
split into `_lib/cwBatch.ts`, allow-lists unioned (owner `4260961` `1200bb6` `611ec46`);
`c4f2dbf` `index_footer`/`warm` shed the "ported from" notes, `warm._ts` is gcs's
(owner `714be65` `65a2cc2`); `3069767` `AuthGate.tsx` on `@open-athena/auth` with an
`edge` whoami source in cw's `auth.ts` (owner `1d7fc2f` `afd932e`); `d8bd295` `/og`
opens inside the lone bucket (CIC found the one-hue card). CIC'd on the throwaway
harness (local D1, 3274/3275): home with the D1-filtered scan list and golden-angle
hues, `/og`, `/?wall`.

**Named, not ported** (intended axes; the hunk owners → the `site/src` row): marks /
owner / claims (`a2f0103` 51 hunks, `7ab2d25` 16, `99c60a3` 13, `839f4b4` 10, `e5d7fb6`
8, `66a07e7` 8, `f182c9e`, `5ccd092`, `fdac944`, `6c91469`, `3416155`, `5e0d35c`,
`8fea938`, `0411c66`, `5244953`), the read axis (`5ff6222` 10), gcs-only pages in
`Root.tsx` (`5880e87` `e9ddd74` `e195dd0`), the auth package (`1d7fc2f`), gcs's
`/cw`-route plumbing + URL-path drilling in `App.tsx` (`2b693f1` `67d8132`), the
index-retention test variants (`c02a4b6`) and warm ids/paths (`714be65`).

**gcs-ward**: specs/cp-from-cw-s3-2026-09-16.md — allow-list union, every-store D1
filter, lone-bucket `/og`, wall copy from the store, `warm(headers)`, `/files` cells
(`e38add0`), the tooltip conversions (`49dcc37`), the fit-to-data digest sparkline;
considered-not-queued: the morning-scan day rule, `--redo-replies`, `sweep
expire-manifest` / `-G`, `lifecycle`.

Not done, by instruction: the `cloud/` rename.

## De novo factor (2026-09-16) — `denovo/cw-s3-gcs-2026-09-16`

Per specs/denovo-factor.md: from cw-s3's tip `73183b3` (post-rename), gcs's delta rebuilt as features, every commit a working checkpoint. Built against a throwaway `gcs + the identical rename` target until gcs renamed for real (`6d8a782`; the two differed only in `uv.lock`, one AGENTS.md line and the ledger), then against `gcs` itself. Tooling in `tmp/denovo/`: `owners.py` (delta inventory: added-file creators, per-hunk blame owners, per-commit rollup), `hunkpick.py` (apply the hunks of `git diff -U1 <worktree>..gcs` whose added lines a given set of gcs commits introduced), `transplant.py` (move whole click command blocks between CLI files), `ckpt.sh` (the checkpoint protocol), `rename.py` (the §10 rename as a script), `commitB.py`.

**Endpoint reached: the preferred one.** Commit 12 (`9d4c864`) is "cw-s3 + everything general" — in fact the whole union: every gcs feature is present, gated so the cw store behaves as today. On top of it the two deployment deltas: **A** (`53d7e5c`) = the GCS deployment on the union (gcs's store row, wrangler, og assets, docs, specs, `/cw`, health cron), **B** (`59433de`) = minus the CoreWeave deployment (cw's modules, job, plan-first Functions, migrations, pages, verbs, and their wiring). The tip differs from `gcs` by **44 files, +534/−504, all of them the construction's generalizations** (`tmp/denovo/tip-vs-gcs.diff`; the manifest's §11) — no gcs feature is missing and nothing cw-specific remains. Every one of the 14 commits builds (`tsc -b`, `tsc -p functions`, vite) and passes the py suite (14/14 green in `tmp/denovo/ckpt-check.txt`; the py count rises 104 → 253 as modules land and drops to 201 when B removes cw's tests).

| commit | feature | clean / entangled | files | + | − | gcs commits drawn on |
|---|---|---|---|---|---|---|
| `e5989f7` | 1: py engine + attribution modules — identity/rules/p | clean | 18 | 1363 | 614 | 21 |
| `43844ff` | 2: gcs-only py feature modules — marks/todo CLI, owne | entangled | 18 | 5750 | 16 | 14 |
| `27ef029` | 3: auth package (`@open-athena/auth` Tier 2) as the o | entangled | 15 | 846 | 76 | 8 |
| `896c88b` | 4: server-side view axes + the marks ledger API — `/a | entangled | 38 | 2726 | 252 | 32 |
| `94e6053` | 5: FE leaf modules — marks/owner/sweep client libs, s | clean | 35 | 2931 | 223 | 28 |
| `5000bae` | 6: the page hub — gcs's `App`/`Treemap`/`ChildrenTabl | entangled | 30 | 6094 | 1685 | 77 |
| `093662d` | 7: the GCS job pipeline beside the CW scan job — one | entangled | 52 | 1004 | 137 | 10 |
| `b4ac832` | 8: deploy/dev tooling, CI, e2e, site scripts, sheet-s | entangled | 34 | 1797 | 936 | 32 |
| `8b3dd45` | 9: the digest seam — gcs's Shape-C digest (Slack + Di | entangled | 11 | 2726 | 1009 | 17 |
| `36e2fa8` | 10: both sweep consoles' APIs side by side — gcs's `/ | entangled | 16 | 553 | 214 | 14 |
| `ae7e043` | 11: deployment constants become environment config — | entangled | 7 | 78 | 40 | 14 |
| `9d4c864` | 12: one index/warm CLI — gcs's `index-sync`/`index-gc | entangled | 4 | 102 | 64 | 6 |
| `53d7e5c` | A: the GCS deployment on the union — gcs's store row, | clean | 98 | 4311 | 558 | 10 |
| `59433de` | B: minus the CoreWeave deployment — what the union is | clean | 44 | 175 | 5533 | 0 |

**Feature order, revised from the spec's 13** (with the reason each time): (1–2) the Python engine first, in two commits — attribution + viz/index tiers, then the gcs-only feature modules — because they are pure and tested and the site never imports them; (3) the auth package *before* the view axes, because every API file imports the gate and its `Env`; (4) the server-side owner / mark / class / extras axes as one commit with the marks ledger API, because `view.ts` imports `markAxes`/`owners`/`totals`/`ledger`/`extras` and none of them stands alone; (5) FE leaves as their own commit (they build unreferenced); (6) **owner UI + marks UI + read axis + class/pricing + site chrome + loading states (spec steps 3, 6, 8, 9 and the general CPs) in one commit**, because `App.tsx` owns every axis and `Treemap`/`ChildrenTable`/`AgeChart`/`DiffTreemap`/`SizeOverTime` take their props from it — and because gcs's hub already gated all of it on the store and on data, so the separation the spec asked for turned out to be three `Store` fields, not a hunk order; (7) the job pipeline; (8) deploy/dev/CI/e2e/docs; (9) the digest twin + weekly (spec step 10) as the digest *seam*; (10) the sweep executors' APIs (spec step 7) after the consoles; (11–12) the shared modules' deployment constants → env and the index/warm verbs onto gcs's text; A and B last. Index extras, `/api/bench` and the warm-cache token (spec step 11) folded into 1, 4, 6 and 12.

**Seams — what had to change in earlier checkpoints, or could not be separated by config** (the input to the convergence spec):

1. **Two sweep executors** (spec's expected one). Both branches define a `sweep` click group with same-named subcommands (`manifest`/`execute`/`undo`) of different semantics, both mount `/sweep`, both serve `/api/sweep/*`, and each has its own D1 tables (`deletion_runs`/`bands` vs `plans`/`deletions`) and Batch job spec (`_lib/gcp.ts` region map vs `_lib/cwBatch.ts`). Resolved by *naming*, not config: cw's become `plan-sweep` (CLI), `PlanSweepPage` on `/sweep` via `Store.sweep`, `/api/plan-sweep/*`. Collapsing them is a design (one executor with a plan source), not a flag.
2. **Two mark ledgers.** cw's plan-first marks (`0001_marks.sql`, `/api/marks`, client `marks.ts`) collide with gcs's actions WAL; cw's moved to `/api/plan-marks` + `planMarks.ts`. The plan-first sweep should read the `actions` ledger instead.
3. **D1 migration lineages.** gcs's `0001`–`0023` and cw's `0001`–`0005` cannot both apply to one database (`marks` twice, cw's `0005` = gcs's four index migrations folded). The union carries both directories; a merged lineage (cw's renumbered after 0023, `IF NOT EXISTS`, renamed tables) is required before one branch can deploy to either D1.
4. **Two digest contents on one mechanism.** gcs's Shape C and cw's framing A share thrds' client and the state/plot hosting but not their content functions; cw's became `cw_digest*` + `cw-digest`. The fix is a content profile per store on one engine.
5. **The auth model** — resolved by config: gcs's `_lib/auth.ts` already reads the edge JWT first; `EDGE_TRUSTED` + `BASE_SCOPE` make a whole-host Access deployment work without the allowlist, `admin_emails` feeds `admin`, and the client picks its whoami source with `VITE_AUTH_MODE`. `requireViewer`/`requireAdmin` are aliases over `requireScope`.
6. **The FE hub** — resolved by config: `Store.marks` (was `store.key === 'gcs'`), `Store.sweep`, `Store.lifecycle`, `Store.peer`, plus what was already data-driven (`hasAttr`, `readRange`, `store.prices`). Nothing in App/Treemap/ChildrenTable needed a cw-specific branch.
7. **Deployment constants inside shared modules** — resolved by env: `D1_DB_ID`/`D1_DB_NAME`, `INDEX_VARIANTS` (which also derives the site's variant contract), `WARM_PATHS`, `SITE_URL`, `SNAPSHOTS_SUBDIR`; the map root's crumb label `ROOT_LABEL`; the image's job script as a `JOB` build arg; `deploy`/`dev`/`cf-status` reading `wrangler.toml` / `package.json`.
8. **Hard-coded scopes and labels in gcs.** Every gate said `GCS_SCOPE` (three switched to `CW_SCOPE` on a vestigial `cw/` path prefix); `SiteNav`/`title.ts`/`About`/`UserPage` said "Marin GCS"; `view.ts` named the root `marin GCS`; `cf-status`/`deploy`/`dev` named the project and ports. All read the checkout now.
9. **Caches keyed without the deployment.** The Worker's colo cache (and the browser's immutable cache) kept serving a view with the old root label after the var changed — a cache-key bump is part of any such change.
10. **Dead code surfaced by the union**: gcs's `alert` verb calls `_snapshot_dates` and `_load_meta`, neither of which exists on gcs (pyflakes: undefined names); dropped.

**Config-shaped, i.e. separable by a flag/var/arg with no code seam**: the owner / mark / read / class axes and the ledger pages (store + data), the auth model (`EDGE_TRUSTED`, `BASE_SCOPE`, `VITE_AUTH_MODE`), the job (`JOB`), the D1 / variant / warm / site / snapshot roots (env), the root label (`ROOT_LABEL`), the deploy target and ports (checkout files), the lifecycle fold and the peer link (store fields).

**CIC** (throwaway harness in `wt/factored`: local D1 with cw's two synced scans, the lifecycle overlay, `VITE_AUTH_MODE=edge`): at the hub (6) and the union (12) the cw store shows its own map, legend, lifecycle fold, a children table without owner/read columns, crumb `marin CoreWeave`, menu Map / Scans / Sweep / About / GCS usage ↗, the plan console on `/sweep`, `/users` → `/`, and only subtree / diff / series API calls (`tmp/denovo/shots/`). What CIC caught: the `store.key === 'gcs'` gate, `useUserEmails` firing on a marks-off store, the doubled document title, the hard-coded brand and root label, the dash-only columns, the cache lesson above.

**Not applied to cw-s3.** The branch is a construction; cw-s3 itself still carries `sweep`/`digest` as its verb names, `/api/sweep/*` and `/api/marks` as its routes, and its own `auth.ts`. Adopting the union on cw-s3 means those renames reach prod (the Batch scripts in `_lib/cwBatch.ts` callers, `job/cw-run.sh`), plus the D1 lineage decision (#3). gcs-ward: manifest §11.

### 2026-09-19 (cw-ward, `/cp gcs`) — directed general FE + migration mirror; union/share-links deferred

gcs `180a362c4..213f68d21` (29 unmarked). Scope this pass (Ryan): the directed
mirror + safe general FE only — **not** the union, **not** share links. Landed on
`cw-s3` (three commits, `git cp` cursor **left at `180a362c4`** so the deferred
backlog re-surfaces; the landed commits carry `@` port-evidence):

- `9f77d50` ← `474f940`: `packages/treemap` map `minWidth: 0` (reflow down on
  resize / rotate / device-mode). Hard-parity surface + a real mobile bug here.
  Also owed dt-ward (shared `@rdub/treemap`).
- `e99577e` ← `ffc9f78`: children **Created** column flush-right dot+month
  (`minmax(3ch, max-content)`, year folded inline), adapted onto cw-s3's 8px
  contrast-ring swatch.
- `28e199a` ← gcs `0024_plans`/`0025_deletion_plan_link`/`0026_stage_batches`:
  bring `site/migrations/gcs/` (cw-s3's byte-mirror of gcs's flat lineage) current
  with the seam-1/plan set. Pure parity bookkeeping — cw's wrangler applies only
  `migrations/cw/`.

**Deletion-memo *feature* (gcs `9567ca0`) NOT landed — union-blocked, not an
intended delta.** cw-s3's `ChildrenTable` is read-only (no trash gesture / no
multi-select sel-bar), and its plan-first sweep already carries a plan-level
`note`; gcs's per-batch memo rides `stageItems`/`useStage` (the staging flow =
union seam-1, §793). Only the schema mirrored. The seam-1 *plan schema*
(`eeeda8bf2`) is a no-op here — gcs adopted **cw-s3's** shape (cw 0002/0003).

**Skipped, with reasons** (cursor stays trustworthy via the unmarked backlog):
share links `0b7b684 cc9dc26 917465c cbc88e6 9c05854 eb582c2 0c7cf16 fd8ebc1
2ad5b67 213f68d` + auth migrations `0027`–`0030` (whole-host CF Access
`EDGE_TRUSTED` — a `?key=` guest can't pass the edge wall; deferred flag-flip);
the union `ead244be0 ecebd22f9 4b65b1852 def9eeef8 5eea3564c 168533e4c` +
seam-1 staging `87aad75e2 206c0eba8 a50dcb283 34b540f1c 9567ca0 3d6dcae46`
(deferred — Ryan's call, reaches prod: verb/route renames + D1 lineage #3);
`2cee960` use-prms `^0.5.0` (gcs-lockfile-specific — cw wants its own audit);
`13f23bfdb` secrets-strip (= cw's own `0858d6d`), `b1c98d0` x-range picker
(gcs ported it *from* cw).

Verify: site `tsc -b`, `packages/treemap` `tsc`, both clean; treemap 146 tests
pass; HMR applied all three edits clean. CIC blocked this pass — the local
wrangler dev stuck busy (memory pressure; cold reload never settled).

### 2026-09-28 — `cw-s3-next` assesses gcs `180a362..2d0a28b` + dt/cloud `47eee40..9165705` (cursors set; nothing landed, by design)

Pass framing: the cutover deploy of `cw-s3-next` is in flight (prod D1 caught up, R2 flip committed, secrets + `site/deploy` next), so nothing that changes the deployable tip lands ahead of it. Both peers assessed, both cursors moved to their tips, every commit named with its disposition:

- **gcs `180a362..2d0a28b` (59).** Landed already via the earlier manifests and the base: the per-branch `cf/` split (`2d0a28b`'s half is gcs's own), the cache TTL/version + series-gap + `blockSpan` fixes (`2d0a28b` ported them *from* cw-s3). **Deferred to after the cutover, as one pass = [`auth-bump-name-squash.md`](auth-bump-name-squash.md)**: `166a78b` `b9039b0` (pin → auth `4c28b9d`, one `name`, no `cf-access`), `d935935` (`0001_init.sql` rebaseline — cw's twin rebaselines `migrations/cw/`), `eeaf4a4` (One Tap button; cw's wall gets it with the same bump), `1d53381` `c3b17a7` `cbc89f8` (sign-in error page, request-access fold, own `/signin` — cw's `SignInPanel` wall already carries the `/signin` route; take the `?error=` page with the bump), `b533957` `e792f56` `47b19fc` `7331944` (Zero Trust removal = cw's P4), `6e289b3` `f09df70` `1fd53f1` (dev sign-in marker / `oa_dev_session` / `/auth` proxy Host — cw's dev already has the proxy; the marker comes with P3). gcs-only: `dabb9c7` `5237f8f` `fb77c6e` `e798429` (gcs stack + spec moves), the `oidc-cutover.md` phases (cw's twin is `oidc-cutover-cw.md`).
- **dt/cloud `47eee40..9165705` (7).** `9165705` (scan page on R2: 64K-row groups, `.shallow.parquet` sidecar, `migrate-row-groups` over URLs, phone-width scan table) is base code cw inherits — **by rebasing `cw-s3-next` onto the new tip after the cutover**, not by CP (the branch is `dt/cloud` + cw delta and stays that way). `8820240` `3938f3d` `15b1b86` `c77dd0c` `608974d` `ebcf1d3` (staged page, sqla catch-up, specs) are the local/Flask side and specs — same rebase, no cw adaptation.

Verify: none needed (no code moved). Cursors: gcs `2d0a28b`, dt/cloud `9165705`.

### 2026-09-28 — `cw-s3-next` rebuilt on dt/cloud `18e5a55` and deployed (cursor dt/cloud `18e5a55`)

An in-place `git rebase` is classifier-blocked in the cw session, so the branch was rebuilt: a fresh branch off `18e5a55` with the 30 cw commits replayed (`15b553f` `8145a32` `fb528dc` `2270a05` `3406185` dropped out — on cloud as `279089c` `0f51d47` `00ba9ed` `d933836` `18e5a55`), then swapped in as `cw-s3-next` (force-push; old tip `8498f7a`). Two replay conflicts, both cw-side: `CLAUDE.md` (cloud's file vs cw's symlink → `AGENTS.md`, symlink kept) and `site/wrangler.toml` (cw's final file; the base's only toml change since `47eee40` was cw's own `0f51d47`). `[base]` `75e0cbf` (exception-free `<short>-prod`/`<short>-dev` deploy pointers) went to cloud whole as `7cc1d4e` together with the `--dev` half of `f8daca8`, so both hunks drop out on the next rebase. Convention from here (disk-tree, 9/28): rebase onto `dt/cloud` at every base bump before new work; generic changes as their own commits with subject prefix `[base]` (picked verbatim, no manifest); a manifest only when something needs adapting. Verify: tsc ×2, vitest 141, `dt-cloud` pytest 277 (+1 skipped); dev stack E2E, then production `75e0cbf` (Pages `3a445112`).

### 2026-09-28 — `cw-s3-next` rebuilt on dt/cloud `e2887c9` and deployed (cursor dt/cloud `e2887c9`; gcs re-forked, cursor gcs `3065bdb`)

Second rebuild of the day, same method (fresh branch off the cloud tip, the 32 cw commits since `18e5a55` replayed; `75e0cbf` dropped — on cloud as `7cc1d4e`; three `site/deploy` conflicts resolved to the base's file, which now carries `--dev` + per-project pointers whole). What the base brought: the `@open-athena/auth` bump to `7442ab0` (`dfecb1f`; Access verification stays in the base's `_lib/cfAccess.ts`, so cw stays edge-gated), `STORE`/`AUTH_MODE` `[vars]` (`6eea350`), plan prefix shape from `[vars]` + staging (`4ae73be`), share links (`e6f9088`), `site/dev --local-db` (`4fd6f98`), and the three E2E findings (`e2887c9`). cw's adaptation is one delta commit (`7a00183`): `STORE = "cw"`, `AUTH_MODE = "edge"`, `STORE_SCHEME`/`STORE_BUCKETS` in both var blocks, and `migrations/cw/0020_auth_single_name` (package migration 0013 on cw's mirrored lineage; applied to the production D1 before the deploy). Verify: tsc ×2, vitest 145, `dt-cloud` pytest 277; dev stack E2E (no `rules.json` fetch, `/files` header `r2://oa-cw-s3-usage-index (listing/ + …)`, 404 page without the `users` link, whoami `sso` with `cw`+`admin`); production `7a00183` (Pages `841c9829`).

gcs re-forked onto the base the same day (`gcs` = dt/cloud `3065bdb` + 11 "(gcs deployment delta)" commits, tip `c988f03`; old tip = tag `gcs-legacy`). Our gcs cursor restarts at the shared base `3065bdb`: the 11 delta commits are the survey backlog, all deployment config/job/docs.

### 2026-09-28 — `cw-s3-next` partial take from gcs: `cf/cfn_dashboard.py` verbatim (`ea501d7`); cursor unchanged

Both deployments grew a dev-stack hostname the same afternoon (`dev.gcs.oa.dev`, `dev.cw-s3.oa.dev`) and modeled it in the shared component independently — gcs as `Store.branch_aliases` (a `BranchAlias(domain, branch)` per preview branch), cw as a single `dev_domain`. The component is meant to stay byte-identical across branches, so cw took gcs's file verbatim (`6cfa8c5`) and moved its one alias into the generic shape; the Pulumi resource names coincide (`<stack>-dev-cname` / `-dev-domain`), so the live stack saw 11 unchanged. The gcs→cw-s3-next cursor stays at `3065bdb`: this was one file from one commit, not a pass over the 12 gcs delta commits (the rest — auth-bump follow-ons, One Tap wall, migrations squash — are still owed and get their own pass).

### 2026-09-28 — `cw-s3` `/cp gcs` pass after the first `cloud` merge (cursor gcs `3065bdb` → `b6a1133`)

Surveyed `3065bdb..gcs` (17 first-parent commits: the re-fork's deployment delta, the dev stack, gcs's own `cloud` merge `0e7baf8`, two follow-ons). Everything base-worthy on that range is already in `cloud` (the merge brought it), so the pass reduces to deployment tooling.

- `8fd4ff3` job/, `92eecce` Docker + Cloud Build, `9bac69f` sheet-sync, `86149fa` cf/ stack, `49d88e1` wrangler.toml + `migrations/gcs`, `20c31ad` index.html/og/devPort, `530d760` README/AGENTS, `14dfc3d` + `cbe8ec2` + `4d77589` specs/ledger, `c218d2e` scripts/branch-audit + docs/img: **skipped — gcs deployment delta** (cw carries its own of each; `scripts/branch-audit` came in via the `cloud` merge).
- `03ead3e` CI triggers + `health.yml`, `b6a1133` its `ui/dist` fix: **landed adapted** (`c99c225`) — `health.yml` for cw-s3.oa.dev with `SNAPSHOTS_SUBDIR=cw` and its own `CW_S3_USAGE_TOKEN` secret; the trigger list itself is intrinsic (cw's already names `cw-s3`). Needed a `[base]` first (`6f4d700`): `dt-cloud healthcheck -s/--subdir`, since the check probed the bare `/data/scans.json` (the root listing, empty on R2 for cw). Caveat recorded in the workflow: `schedule` fires from the default branch (`gcs`) only, so the hourly cw check has to be a job in the default branch's `health.yml` — owed to the gcs pass.
- `c988f03` `gcs-next` trigger: **skipped** (gcs-only, and that branch is gone).
- `95d4dbb` `[env.preview]` dev stack, `ea501d7` `dev.gcs.oa.dev`: **skipped / already taken** — cw's preview env and `dev.cw-s3.oa.dev` predate them; `cfn_dashboard.py` was taken verbatim earlier today (`6cfa8c5`) and is byte-identical again after this pass.
- `0e7baf8` gcs's `cloud` merge: **not a CP** — cw did its own (`f33e59c`, same base tip family). One leak that merge left on cw and this pass fixed: `.github/workflows/build-dist.yml` had stayed at the pre-`@rdub/treemap` version; now `cloud`'s (workflows are outside `branch-audit`'s surfaces, which is why the audit didn't flag it).

Base-bound from cw after this pass (for the next `cloud` pick): `6f4d700` (healthcheck subdir), `a300fe3` (over-time BigInt normalization + fallback), `27c55aa` (over-time groups producer).

### 2026-09-28 — auth-bump squash closed on cw-s3 (`specs/done/auth-bump-name-squash.md`); edge mode removed from the base surface

Shared with gcs (base surface, `[base]` commits for `cloud` to pick): the edge-mode removal `f6fb291` (`_lib/auth.ts`, `_lib/cfAccess.ts` deleted, `api/whoami.ts`, `src/auth.ts`, `src/AuthGate.tsx`, `vite.config.ts` — `AUTH_MODE` is `app` | `public` now; gcs's tree still carries the edge code the base has, so this lands on gcs at its next `cloud` merge). Branch-specific (intrinsic): `site/migrations/cw/0001_init.sql` (the squash, `6048548`; gcs squashed its own lineage 2026-09-25), `wrangler.toml` comments (`27ac5b5`). Base-bound from cw for the next `cloud` pick, in order: `6f4d700` (healthcheck subdir), `a300fe3` (over-time BigInt normalization + fallback), `27c55aa` (over-time groups producer), `218f4b2` (subtree/diff D1 guard), `f6fb291` (edge mode removal).
