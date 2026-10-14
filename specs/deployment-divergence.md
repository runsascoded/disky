# Deployment divergence: what differs between `gcs`, `cw-s3` and `cloud`, and what shouldn't

Status: audit, 2026-10-10. A read-only survey of `cloud` @ `487058b4`, `gcs` @ `4c2661f5` and `cw-s3` @ `4de39d7d`. Both deployment branches have merged `cloud` at its tip (their merge-base with `cloud` is `487058b4`), so `git diff cloud <branch>` is exactly each deployment's delta. No live state was queried (GCS, R2, D1, Batch, Scheduler). Where a claim depends on live state, the text says so. File refs are to the named branch (`gcs:`, `cw:`); unprefixed refs are to `cloud`.

Goal (Ryan): one generic implementation on `cloud`. Deployment differences should live only in config and profiles, and only where they reflect real fleet properties. Related: [[generic-code-no-deploy-defaults]], [[no-complexity-for-missing-index]], [[branches-carry-exactly-their-code]], and `specs/index-landscape.md` §3 (per-deployment index differences, not repeated here except where things changed since).

Classes used below:
- **real**: a property of the fleet or its inputs (provider, size, cadence, audience, ownership signal, access logs).
- **accidental**: historical, rollout order, or which session built what. Converge.
- **missing**: a capability one side has that the other lacks, with no fleet reason. Port it.

## Executive summary

- **The two scan jobs share almost no vocabulary.** gcs records 15 phases and cw records 15 + 2×(buckets) (with 5 buckets, 25), and only 6 names coincide: `publish`, `index-sync`, `warm-cache`, `probe`, `index-gc`, `static-names`. Two of those six mean different things: `index-gc` is a D1 row sweep plus `-r 30` on gcs but a file GC on cw; cw's `index-sync` also contains its row GC. gcs's `webdata+stage` phase is a leftover name: it now times `dt-cloud rules`.
- **There are three path-store pipelines for one artifact.**
  - gcs: `bulk-list` fan-out → `path-index` (from listings).
  - cw: per bucket `bulk-list` → `disk-tree import -e stream` → `cw-webdata.py` → `index-write` (from layer-2).
  - r2 demo (`cloud`'s `daily-ingest.yml`): `bulk-list r2://` → `path-index -S`.

  The r2 demo proves `path-index` already consumes S3-protocol listings. So cw's "S3 needs `disk-tree import`" is **not** a real difference. It is historical.
- **Index sets diverge by builder, not by fleet.**
  - Only cw has the age pyramid and over-time groups, because only `index-write` produces them.
  - Only gcs has the interval store, by rollout order.
  - The age pyramid and over-time groups are missing on gcs for the same builder reason.
- **cw's static-names stage runs after `index-sync`, in the background.** So every new cw scan is listed before it's searchable, and `?f=` on the newest scan answers `scan-not-indexed`. That's the case `1b9256ec` just made honest (cw prod `?f=.json` at `10-11T0001`). gcs runs the same stage before `index-sync`. This ordering difference is accidental and user-visible.
- **Retention is mirrored.** gcs retires D1 rows (`-r 30`) but never deletes superseded generation files. cw deletes files (`-F`, 2-day grace) but has no D1 retention while writing ~4 scans/day toward D1's 10 GB cap. Each has half of the GC.
- **Site config:** ~45 vars across the two `wrangler.toml`s.
  - Most differ for real reasons: buckets, auth audience, executor, scheme.
  - Accidental: `FILTER_INDEXED_ONLY`, `OG_CARDS` and `CACHE_NS` set on one side only; `STATIC_GEN` / `INTERVAL_STORE_GEN` duplicated in three places each.
  - Each deployment repeats its bucket list in up to 8 places.
- **Untagged code drift is small.** cw edits no shared code. gcs carries:
  - a stale `restrictToPool` merge residue in `site/src/ledgerOverlay.ts` (dead code; `cloud` replaced it with server-side folding in `c930477d`);
  - the retired query box's Cloudflare tunnel in the *shared* `infra/cf/stack/__main__.py` (`8b2c97fc`, untagged);
  - a `deletion_runs.plan` vs `.manifest` column split between D1 lineages;
  - ~15 comment-only spec-path edits.
- **About 12 job and infra files are byte-identical or near-identical on both deployment branches.** They belong on `cloud` once: `Dockerfile`, `cloudbuild.yaml`, `job/build.sh`, `job/gen-*-arrows.py`, `job/rerg`, `job/icons/arrows/*`, most of `job/static-names.sh` and `health.yml`.

## 1. Scan job pipelines

Files:
- gcs: `job/run.sh` (565 lines), `job/batch-submit.sh`, `job/static-daily.sh`, `job/scan-runs.json`.
- cw: `job/cw-run.sh` (344), `job/cw-batch-submit.sh`, `job/cw-meta-run.sh` + `cw-meta-submit.sh`, `job/cw-overtime.sh` (+ `-submit`), `job/cw-reindex.sh` (+ `-submit`), `job/scan-runs.json`.

Phase names come from `phase NAME [NOTE]` in each script, which writes a `PHASE …` marker and calls `dt-cloud scan-run record -P`. The profile's `overlapped` and `concurrent` lists (`cloud/src/dt_cloud/scan_runs.py:272-280`) say how phases draw on /scans.

### 1.1 Stages side by side, in run order

Columns:
- **gcs phase**: the phase name exactly as gcs records it.
- **cw phase**: the phase name exactly as cw records it.
- **What runs**: the command, gcs ‖ cw.
- **Class**: real / accidental / missing (see the top of this spec).
- **Proposed name**: the name in the shared vocabulary of §1.4.

| # | gcs phase | cw phase | What runs (gcs ‖ cw) | Class | Proposed name |
|---|---|---|---|---|---|
| 0 | (start: `scan_run -S -B`) | (same) | record start + Batch facts | same | — |
| 1 | — | `bucket-check` | ‖ boto `ListBuckets` + one `ListObjectsV2` per unscanned bucket, alert if non-empty | **missing on gcs** (the fleet project's bucket list is just as listable) | `preflight` |
| 2 | `listing-fanout` | `list <bucket>` ×N | `dt-cloud job submit-listing -W` (one Batch task per bucket, regional VMs) ‖ `disk-tree bulk-list -a s3://b -E endpoint` in-VM, serially per bucket | engine **same** (`bulk-list`). Fan-out vs inline is **real** (595M objects over 6 regions vs ~100M+ on one endpoint), but should be a profile switch | `list` (one phase; per-bucket detail in the note or as child outputs) |
| 3 | `access-ingest` (overlapped with 2) | — | `dt-cloud access ingest` in the background | **real** (usage logs exist on GCS only) | `access-ingest` |
| 4 | (inside `path-index`) | `import <bucket>` ×N | `dt-cloud stage` to local SSD, then the builder reads listings ‖ `disk-tree import -e stream -m -p storage_class_id -s s3` per bucket → layer-2 | **accidental**: two builders (§1.2) | `aggregate` |
| 5 | `path-index` | `webdata` | `dt-cloud path-index -d … -c dir-cache -P … -u bysize` writes the sorts and the age/meta JSONs ‖ `python job/cw-webdata.py` writes tree/age/meta JSONs from layer-2 | **accidental** (cw's `tree.json` is no longer read by the site; `api/estate.ts` and `UserPage.tsx` both say "no tree.json") | `aggregate` |
| 6 | `path-index upload` | `index-write` | inline python, a GCS parallel upload of the sorts ‖ `dt-cloud index-write` (sorts + age pyramid) and a `cp` to the GCS mount | **accidental** | `publish` |
| 7 | `webdata+stage` | — | `dt-cloud rules` (attribution findings) | **real** (ownership only on gcs). The **name is wrong**: it's a leftover from the retired webdata step | `attribution` |
| 8 | `publish` | `publish` | snapshot JSONs + `lifecycle pull` (+ `rules.json`) → `snapshots/[<sub>/]<scan>/` | same, except layout (§4) | `publish` |
| 9 | — | `layer-2` | ‖ copy per-bucket layer-2 parquets to `cw-l2/<scan>/` | follows from cw's builder. gcs keeps `dir-cache/` instead | folded into `publish` |
| 10 | `static-names` ∥ `interval-store` (`concurrent`, before 12) | — | `dt-cloud static-names runs add -c` (120 min timeout) ∥ `dt-cloud interval-store append -c -w 1800` (60 min timeout) | static: see row 15. Interval store: **missing on cw** (no `interval_profiles/cw.json`) | `static-names`, `interval-store` |
| 11 | — | `index-write` | (row 6) | | |
| 12 | `index-sync` | `index-sync` | `dt-cloud index-sync -d … -g GEN -k …` ‖ the same, **plus `dt-cloud index-gc SCAN`** (row sweep) inside the phase | **accidental** (cw buries the row GC in `index-sync`) | `index-sync` |
| 13 | — | `publish-r2` | ‖ `dt-cloud publish-r2 -L SCAN`: the served subset to R2 | **missing on gcs**. gcs's per-scan path store is still read cross-provider from GCS (`STORE_BUCKET = oa-gcs-usage-dvx`). Only its interval and static stores are on R2. [[diff-perf]] names R2 colocation as the lever | `publish-r2` |
| 14 | — | `over-time` | ‖ `job/cw-overtime.sh` → `dt-cloud over-time-groups` + `publish-r2` per group | **missing on gcs** (needs the age/over-time builder; see §4 on over-time vs interval store) | `over-time` |
| 15 | (row 10) | `static-names` (`overlapped`, in the background from here, waited on at the end) | `STATIC_NAMES_PROFILE=cw dt-cloud static-names runs add -c` (no timeout) | **accidental ordering**: it starts *after* `index-sync`, so the scan is listed before it's indexed and the newest scan answers `scan-not-indexed`. Gating is also inverted: gcs is on unless `SKIP_STATIC_NAMES=1`, cw is off unless `STATIC_NAMES=1` (the cron sets it since `fd48ed4c`, today) | `static-names`, before `index-sync` |
| 16 | `healthcheck` | — | `dt-cloud healthcheck -d SCAN` + Slack warn | **missing on cw** (only the GHA `health.yml`, which on cw-s3's branch fires by dispatch only) | `healthcheck` |
| 17 | `warm-cache` | `warm-cache` | `dt-cloud warm-cache -d SCAN -r gs://…/snapshots[/cw]` | same | `warm-cache` |
| 18 | `probe` | `probe` | `dt-cloud probe -c -o gs://…/probes[/cw]/` (+ Slack on fail on gcs only) | the alert is **missing on cw** | `probe` |
| 19 | `slack-digest`, `discord-digest` | `digest` | `dt-cloud digest -C job/digest.yml [-P discord]` ‖ `dt-cloud digest -T cw -C job/digest.yml` | Discord: **real** (audience). Phase names: **accidental**. cw's `-T cw` duplicates `template: cw` in its YAML | `digest` (transport in the note) |
| 20 | `index-gc` | `index-gc` | `dt-cloud index-gc -r 30 SCAN` (rows + retention) ‖ `dt-cloud index-gc -R -m 2d -F gs://… [-F r2]` (files only) | **accidental, mirrored**: each side has half (§3) | `gc` |
| 21 | — | (wait on static-names) | | | — |
| — | `PHASE total` / `SNAPSHOT-JOB-DONE` | `CW-SCAN-JOB-DONE` | done markers (both profiles parse them) | **accidental** | `SCAN-JOB-DONE` |

Other per-job behavior that differs:

| Item | gcs | cw | Class | Recommendation |
|---|---|---|---|---|
| Recorded run kinds | `scan`, `reproc` (`-k`); SCRATCH and GATE runs not recorded | `scan` only; `REPROC=1` records **nothing** | accidental | Record `reproc` on cw too (the same `SR_KIND` logic) |
| Non-scan modes in the scan entrypoint | `SWEEP_BENCHMARK_PLAN`, `ACCESS_ONLY`, `SWEEP`, `GATE`, `SCRATCH`, `NOP_IF_PUBLISHED` all branch inside `run.sh` | separate scripts (`cw-reindex.sh`, `cw-overtime.sh`, `cw-recompress-submit.sh`, …) | accidental | cw's shape is better: one script per job kind. Split gcs's sweep/benchmark/access-only/gate branches out of the scan driver |
| Failure alert | `slack_post`: pure python3. Its comment says the image has no curl, which is stale: both Dockerfiles install curl | `slack_alert`: curl | accidental | One `dt-cloud alert` (or `thrds slack post`) call |
| Scan id default | `SNAP_ID` ← `SNAPSHOT_DATE` ← `date -u +%F` | `date -u +%Y-%m-%dT%H%M` | accidental (violates "no daily" in [[generic-code-no-deploy-defaults]]) | One `dt-cloud scan-id` resolver; the cron says the granularity |
| Site token env | `GCS_USAGE_TOKEN` ← secret `gcs-sheet-sync-token` (the sheet mirror's grant, reused) | `SITE_TOKEN` ← `cw-s3-job-grant` (its own grant); maps the old name | accidental (gcs never took the rename; `deploy.py:53-62` still accepts both) | gcs: its own `gcs-job-grant` secret as `SITE_TOKEN`; then drop the deprecated names from `deploy.py` |
| Secret-strip list | 10 names | 11 names (+`SITE_TOKEN`) | accidental | Strip inside the driver, over every `secretVariables` key |
| NOP retry | `NOP_IF_PUBLISHED` (scheduled retries) | none (`maxRetryCount: 0`, no retry attempts) | real-ish (gcs's cron has retry attempts) | A profile flag |
| Timeouts on appends | static 120 min, interval 60 min | none | accidental | Profile fields, enforced by the driver |
| Machine | n2-highmem-32, 250 GB, 1500 GB local SSD, 6 h max | n2-standard-8, 30 GB, 200 GB pd-ssd, 4 h max | **real** (fleet size: 595M vs ~100M objects). The max run durations are hard-coded literals | Profile fields |
| Image tag | `gcs-usage-snapshot:latest` | `gcs-usage-snapshot:cw` | real (each branch builds its own image) | Tag = deployment name (`:gcs`, `:cw`), never `latest` |
| Python extras in the image | `--extra plot` | `--extra plot --extra overtime` (+ git for the pyrmts pin) | follows the builder | One Dockerfile on `cloud`; extras from a build arg |
| Meta self-scan | — | `cw-meta-run.sh`: list → import → webdata → publish → index-write → sync → publish-r2. No `scan-run` record, no healthcheck | missing (no record) | A third profile of the same driver (`meta`), recorded under `store=meta` |

### 1.2 The builder is the root of most of the drift

| Builder | Input | Output | Used by |
|---|---|---|---|
| `dt-cloud path-index` (`cli.py:405`, `viz.write_path_index`) | listing shards (any scheme), `-a` attribution, `-x` access, `-c` dir-cache | `path` + `bysize` (+ `-by-user` copies, `-S` search sidecars), `age.json`/`meta.json` | gcs, r2 demo |
| `disk-tree import` → `cw-webdata.py` → `dt-cloud index-write` (`cli.py:695`, `index.write_index`) | per-bucket layer-2 | `path` + `bysize` + **age pyramid**, tree/age/meta JSONs (from `cw-webdata.py`, a cw-branch-only script) | cw, cw meta |

Consequences:
- The age pyramid (path-aware AgeChart) and over-time groups exist only where `index-write` runs.
- Attribution, access and dir-cache exist only where `path-index` runs.
- Neither difference is a fleet property.
- `job/cw-webdata.py` (180 lines) is generic L2 → site JSON code living on a deployment branch. Its own docstring says "for a non-GCS store".

Proposal: one builder, `path-index`:
- **Inputs:** listing shards, with optional attribution, access and dir-cache.
- **Outputs:** the sorts, plus the age pyramid as an option (move `index.write_index`'s pyramid cut into the shared cut).
- **cw:** goes `bulk-list` → `path-index`. It drops `import` and `cw-webdata.py`, and the per-bucket layer-2 parquets with them.
  - Check first whether anything reads `cw-l2/<scan>/<bucket>.parquet` besides `over-time-groups`. Its docstring says it reads "the scans' path-indices", but `cw-overtime.sh` says the group's "scan rollups (`cw-l2/<gid>/<bucket>.parquet`)" stay in GCS.
  - The ad-hoc analysis value of the per-bucket layer-2 (`cw-run.sh` "Keep the canonical layer-2 parquets") is covered by the `path` sort, which holds every row.
- **Cost risk:** cw's import is measured at ~2 GB RSS on an n2-standard-8. `path-index` on gcs needs highmem-32. Measure `path-index` on a cw scan before switching (the r2 demo shows it works on S3 listings, not at cw's 100M rows).

### 1.3 What each `scan-runs.json` says

| Field | gcs | cw | Class |
|---|---|---|---|
| `jobs` matchers | 3 kinds (`reproc`, `scan`, `listing` downstream), by image/name/env | 1 kind, by `commands: ^job/cw-run\.sh` | follows from the drivers. One driver → one matcher shape |
| `overlapped` | `access-ingest` | `static-names` | accidental (cw's should be `concurrent` before `index-sync`) |
| `concurrent` | `static-names`, `interval-store` | — | |
| static output URI | hard-coded `static-names/2026-10-08c/manifests/{scan}.json` | `static-names/{static_gen:cw}/manifests/{scan}.json` | **accidental**: gcs's goes stale at the next compaction. Use `{static_gen:gcs}` |
| outputs | `listing`, `dir-cache`, `path-index`, `snapshot`, `static` | `layer-2`, `path-index`, `snapshot`, `static` | follows from the builder |
| `nop_marker` | yes | no | real-ish (NOP retries) |
| tests | none on gcs | `cloud/tests/test_scan_runs_cw_config.py`, `test_digest_cw_config.py` (cw branch) | **missing on gcs** |

### 1.4 Proposed shared stage vocabulary

One name per logical stage, deployment-agnostic, in this order. Per-bucket, per-transport or per-store detail goes in the phase **note** (or in outputs), never in the name. A deployment omits the stages it doesn't run; it never renames them.

| Order | Phase | Mode | Does | gcs today | cw today |
|---|---|---|---|---|---|
| 1 | `preflight` | serial | the bucket inventory check (non-empty, unscanned buckets alert); credentials sanity | — | `bucket-check` |
| 2 | `list` | serial | `bulk-list` every bucket, inline or as a Batch fan-out (`listing.mode: inline|fanout`) | `listing-fanout` | `list <b>` ×N |
| 2′ | `access-ingest` | overlapped with `list` | usage-log ingest (inputs that exist) | `access-ingest` | — |
| 3 | `aggregate` | serial | the one builder: sorts, age pyramid, snapshot JSONs (stage to local SSD inside it) | `path-index` | `import <b>` ×N, `webdata` |
| 3′ | `attribution` | serial, optional | ownership rules/findings | `webdata+stage` (misnamed) | — |
| 4 | `publish` | serial | the generation dir, snapshot JSONs, lifecycle and any layer-2 to the data bucket (never over a pointed generation) | `path-index upload`, `publish` | `publish`, `layer-2`, `index-write`'s copy |
| 5 | `publish-r2` | serial | the served subset to R2 | — | `publish-r2` |
| 6 | `static-names`, `interval-store` | concurrent (each starts where `publish-r2`/`publish` ended) | the append indexes, with a timeout each, never fatal | both | `static-names` (late) |
| 7 | `index-sync` | serial | D1 footers + pointer flip: the scan becomes listed | `index-sync` | `index-sync` (+ row GC) |
| 8 | `over-time` | serial | cross-scan groups (needs synced pointers) | — | `over-time` |
| 9 | `healthcheck` | serial | serving invariant + alert | `healthcheck` | — |
| 10 | `warm-cache` | serial | | `warm-cache` | `warm-cache` |
| 11 | `probe` | serial | cold replay + alert | `probe` | `probe` |
| 12 | `digest` | serial | Slack / Discord threads (transports in the note) | `slack-digest`, `discord-digest` | `digest` |
| 13 | `gc` | serial | `index-gc` rows + `-r N` retention + `-F` files (one phase) | `index-gc` | `index-sync` (rows), `index-gc` (files) |

Done marker: `SCAN-JOB-DONE <scan>` (+ `SCAN-JOB-NOP`).

Historical /scans rows keep their old names unless renamed. Prefer a **one-time D1 `UPDATE scan_run_phases`** with an old → new map per deployment over a permanent alias table in the profile. That's no new reader complexity, but it's a prod D1 write (Ryan approves). Per-bucket cw phases (`list marin-us-east-02a`, …) collapse into `list` with summed time. Keep the per-bucket split only if /scans shows it as a note.

### 1.5 One generic scan-job driver on `cloud`

Shape: `dt-cloud scan-job -P <profile.yml> [SCAN_ID]`. A Python driver, not a shared bash script, because:
- recording, alerts, timeouts, secret stripping and concurrency are all already Python-adjacent (`scan-run record`, `digest`, `static-names runs add`);
- they become unit-testable against a fake Batch/D1, the way `scan_runs.py` already is.

Each branch keeps:
- a ~5-line `job/scan.sh` entrypoint;
- `job/scan-job.yml`, which absorbs `scan-runs.json`, the env defaults now at the top of `run.sh`/`cw-run.sh`, and the submitters' `vars()`.

Profile sketch (the cw values are real ones from `cw-run.sh` / `cw-batch-submit.sh`):

```yaml
name: cw
scan_id: {granularity: minute}            # gcs: minute too; the cron decides cadence
buckets: {scheme: s3, endpoint: https://cwobject.com, list: [marin-us-east-02a, hero-checkpoints, marin-us-east-06a, rhoarnet-us-east-08a, marin-us-west-04a]}
listing: {mode: inline, procs: 8, workers: 8}            # gcs: {mode: fanout, regions: {marin-eu-west4: europe-west4}}
layout: {data_bucket: oa-gcs-usage-dvx, snapshots: snapshots/cw/{scan}/, generation: cw-l2/{scan}/index/{gen}/}
d1: {id: 7f1e1326-…, name: oa-cw-s3-usage-db}
site: {url: https://cw-s3.oa.dev, warm_paths: [...]}
aggregate: {user_sorts: [], age_pyramid: true, search: false, attribution: null, access: null}
stages:
  preflight: {}
  publish_r2: {bucket: oa-cw-s3-usage-index}
  static_names: {profile: cw, timeout: 120m}
  interval_store: null                     # gcs: {profile: gcs, timeout: 60m, merge_wait: 1800}
  over_time: {}
  healthcheck: {}
  digest: {config: job/digest.yml, transports: [slack]}
  gc: {retain: 30, files: [gs, r2], min_age: 2d}
alerts: {slack_channel: C0BTUNT3B5Z}
runs: {project: oa-internal-450019, prices: {...}, spot_factor: 0.3}   # what scan-runs.json holds today
batch: {machine: n2-standard-8, memory_mib: 30000, disk: {type: pd-ssd, gb: 200}, max_run: 4h, image: …:cw}
```

Submitters: one `dt-cloud scan-job submit -P … [--dry]` replaces `batch-submit.sh` and `cw-batch-submit.sh`, which share `labels()`, `vars()`, `PIN`/`DRY` and the spec template almost line for line. The `infra/gcp` `BatchCron` keeps taking a submitter's `PIN=1 DRY=1` output, so the IaC contract is unchanged.

Estimate (agent-days, with verification). The ranges are judgment; nothing is prototyped.

| Step | Work | Est. |
|---|---|---|
| A. Vocabulary only | rename `phase` calls + `scan-runs.json` on both branches, `SCAN-JOB-DONE`, one-time D1 rename | 0.5 d per branch |
| B. One builder | age pyramid into `path-index`'s cut; cw switches `import`/`cw-webdata.py`/`index-write` → `path-index`; a2a check of the new vs old index on 2–3 cw scans (`cascade-a2a`-style); RSS measured on Batch | 3–5 d |
| C. Driver + profiles | `dt-cloud scan-job` (+ `submit`), profile loader/validation, tests with fakes; gcs and cw profiles | 4–6 d |
| D. Cut-over | each deployment: one `SCRATCH`-style parallel run, compare outputs/phases, then flip the cron (Pulumi `up`) | 1 d each |
| Total | | ≈ 2–2.5 weeks |

Step A is independent and immediately useful. B is the high-value refactor. C is mostly a mechanical consolidation once B lands, since without B the driver would need two aggregate paths.

## 2. Site config (`site/wrangler.toml`, `site/src/stores/*`)

### 2.1 `wrangler.toml` vars

"P" = prod `[vars]`, "D" = `[env.preview.vars]`. Wrangler does not inherit `[vars]` into an env, so a var in P only is absent on dev.

| Var | gcs | cw | `cloud` default (code) | Class | Recommendation |
|---|---|---|---|---|---|
| `FILTER_INDEXED_ONLY` | `1` P+D | unset (the walk serves regex/glob/multi-term) | off | **accidental** (Ryan 10-09 "just support what's fast and robust", applied to gcs only; `index-landscape.md` §3) | Set on cw once its per-scan static append runs before `index-sync` (§1 row 15) and anchors are built (§4). Then consider making it the code default and deleting the walk fallback for static-indexed deployments |
| `FILTER_STATIC`, `FILTER_STATIC_HEAVY`, `NAME_SUMMARY_STATIC` | `1` P+D | `1` P+D (since `852afc5e`, today) | off | converged | — |
| `FILTER_STATIC_ANCHORS` | unset (= on) | unset (= on, but cw's generation has no anchors: `static_profile_examples.CW` has no `anchors=True`) | on unless `0` | **accidental** | `anchors=True` in the CW profile; build anchors for `2026-10-10cw` |
| `STATIC_GEN` | `2026-10-08c` | `2026-10-10cw` | required | real value. But the same gen is also in `static_profile_examples.py` and (gcs) hard-coded in `scan-runs.json` | One SoT: the profile; `site/deploy` writes the var from it, `scan-runs.json` uses `{static_gen:…}` |
| `PATH_STORE`, `INTERVAL_STORE_GEN` | `intervals`, `2026-10-09b` P+D | unset | per-scan | **missing on cw** (rollout) | With `interval_profiles/cw.json`, after the cross-scan decision (§4) |
| `compatibility_flags` | `["enable_request_signal"]` | same | example has none | converged (cw sets it without an interval store; harmless) | Put it in `wrangler.example.toml` |
| `CACHE_NS` | unset (dev keys tag by host, since D has no `PROD_HOST`) | `dev.cw-s3.oa.dev` D | host tag (`edgeCache.ts:52`) | **accidental**, equivalent today | Drop it on cw (the host fallback covers it), or set it on both. Pick one |
| `PROD_HOST`, `DEV_HOST`, `REPO_URL` | P only | P only | — | real (the cache-tag fallback depends on P-only `PROD_HOST`) | Keep. Document the P-only intent in the example |
| `OG_CARDS` | `1` P+D | unset | off | **missing on cw** (the cw lineage has `0010_og_tokens.sql`) | Enable on cw (needs `SESSION_SECRET` on its Pages project) |
| `ADMIN_EMAILS` | unset | `1` | off | real (cw lineage has an `admin_emails` table; gcs lineage doesn't) | Converges with the D1 lineages (§5) |
| `VIEWER_DOMAINS` | unset | `coreweave.com` | — | **real** (audience) | Keep |
| `ROOT_LABEL` | `Marin GCS` / `(dev)` | `Marin CoreWeave` / `(dev)` | — | real | Keep |
| `ROOT_TITLE` | unset | only inside `STORES_JSON.meta` | — | optional | Optional: give primaries one naming their buckets |
| `STORE_BUCKET` (path store) | `oa-gcs-usage-dvx` (GCS, cross-provider) | `oa-cw-s3-usage-index` (R2) + `STORE_REGION=auto` | — | **missing on gcs** (§1 row 13) | gcs → R2 after `publish-r2` lands in its job (cost/approval) |
| `SNAPSHOTS_SUBDIR` / store `base` | unset → `snapshots/<scan>/`, `/data` | `cw` → `snapshots/cw/<scan>/`, `/data/cw` | `''` | accidental (gcs predates subdirs) | Low priority: migrating gcs to `snapshots/gcs/` is a data move with no user benefit. Leave it, but make it explicit (`SNAPSHOTS_SUBDIR=""` in the profile) |
| `STORE_PREFIXES` | unset (code default list) | `listing/,snapshots/,sweep/,cw-sweep/,cw-l2/` | `index.ts:265-272` | real | Keep |
| `OBJECT_BUCKETS` | the 6 buckets | unset (no `/v1/objects` proxy) | none | **missing on cw** (unless deliberate: CAIOS creds on the site) | Decide. Probably leave off |
| `EXECUTOR` + its vars (`SWEEP_PACING`, `GCP_LOG_VIEW`, `BUCKET_REGIONS`, `CF_ACCOUNT_ID`, `D1_DB_*`, `SWEEP_S3_ENDPOINT`) | `sweep` | `plan-sweep` | `plan-sweep` | historical (two executor families; §5) | Out of scope here; keep |
| `SWEEP_IMAGE` | pinned digest | mutable tag `:cw` | — | **accidental** | Pin a digest on both (the 10-10 stale-image incident went the other way, but the sweep executor should be reproducible) |
| `STATIC_BENCH`, `FILTER_DETAILS_MS` | D only | — | off | dev experiments | Delete `STATIC_BENCH` (+ `api/static-bench.ts`) if the bench is done. Decide whether `FILTER_DETAILS_MS` (300) should be prod/default |
| `STORES_EXTRA`/`STORES_JSON` | — | `meta` | — | real (cw hosts `/meta`) | Keep. gcs links there via `META_TREE_URL` |
| Bindings: D1 / KV / R2 | all three, P+D shared | all three, P+D shared | — | converged | — |

### 2.2 Store modules

`gcs:site/src/stores/gcs.tsx`, `cw:site/src/stores/cw.tsx`, `cw:site/src/stores/meta.ts`.

Different and real: `prices` (GCS classes), `owners`, `executor`, `lifecycle.rules` shape (`gcs` anonymous vs `s3` with IDs), `wall`/`about`/`contact` copy, `peer`.

Accidental:
- **Bucket lists repeated.** gcs names its six buckets in `stores/gcs.tsx`, `STORE_BUCKETS`, `OBJECT_BUCKETS`, `BUCKET_REGIONS`, `run.sh` `FLEET` (twice: lines 58 and 248), `ACCESS_BUCKETS` (7, including `marin-us-west1`, which isn't scanned), `WARM_PATHS`, `batch-submit.sh` `BUCKETS_PY`, `infra/gcp` `FLEET` and `job/lifecycle/*`. cw names its five in `stores/cw.tsx`, `STORE_BUCKETS`, `cw-run.sh` `BUCKETS`, `WARM_PATHS` and `digest.yml`.
  - Recommendation: the job profile (§1.5) is the SoT. The site's store module and `STORE_BUCKETS` are generated from it by `site/deploy`, or the store reads `STORE_BUCKETS` at runtime.
- **`lifecycle.tracked` layout:** `job/lifecycle/` (per bucket) vs `job/cw-lifecycle.json` (primary only). Converge on per-bucket files under `job/lifecycle/`.
- **About copy says "snapshotted daily".** gcs's `about` and `desc` text bake in "daily" and "six". Fine as copy, but generate the counts.

## 3. Infra (`infra/gcp`, `infra/cf` Pulumi)

| Item | gcs | cw | `cloud` | Class | Recommendation |
|---|---|---|---|---|---|
| Scan cron | `0 7 * * *` | `0 */6 * * *` | — | **real** (churn, budget) | Keep. It's config |
| Other crons | `gcs-sheet-sync` hourly (Cloud Run) | `cw-meta-snapshot` `0 9 * * *` | — | real (sheet mirror is gcs's audience; meta scans the shared bucket) | Keep. The meta scan could move to whichever stack owns the data bucket (gcs's), since it scans gcs's bucket too. Not urgent |
| Job SA | `gcs-usage-job` (+ `-dispatch`, `-browse`) | `cw-s3-job` (+ `-dispatch`) | `JobAccount` | real | Keep |
| Shared resources | **owns** `oa-gcs-usage-dvx` (lifecycle, soft delete), `oa-gcs-usage-scratch`, secret `cf-pages-token` | grants itself on gcs-owned bucket/secret | — | real coupling, accidental owner | Fine for now. If a third OA deployment appears, move shared buckets to a `shared` stack |
| `legacyJobAccess` | — | config flag, `false` | — | transition leftover | Delete the flag and its block (cut-over done) |
| Bucket labels | data/scratch `deployment=shared` | — | `cost_labels()` from `$DISKY_LABELS` | converged | — |
| Job labels | `DISKY_LABELS` passthrough in `vars()` | same | — | converged | — |
| D1 retention (`index-gc -r`) | `-r 30` (measured: ~218 MB/scan, 3 sorts) | **none** (~4 scans/day; `path` + `bysize` only, ~2/3 the rows per scan, unmeasured) | off | **accidental** | cw: `-r` sized from a measured MB/scan (likely ~30–60 scans). Verify cw generations carry `.groups.parquet` first (`index-write` → the shared cut, `index.py:221-239`; `-r` keeps a variant without one, with a warning) |
| Generation file GC (`-F`) | **none** (REPROC/recut generations stay in GCS forever) | `-R -m 2d -F gs:// -F r2` | available | **accidental** | gcs: add `-F gs://oa-gcs-usage-dvx` (dry run `-n` first, reviewed) |
| Static-names R2 creds | `gcs-static-index-r2-{key-id,secret}` + endpoint from `CLOUDFLARE_ACCOUNT_ID` | `cw-s3-r2-{endpoint,access-key-id,secret-access-key}` (shared with `publish-r2`) | `Profile.r2_secrets` | accidental | Same three-secret shape on both (`<dep>-r2-{endpoint,key-id,secret}`) |
| CF stack | `Pulumi.gcs.yaml` | `Pulumi.cw-s3.yaml` | `stack/__main__.py` | converged (same program) | — |
| CF stack program edits | **`stack/__main__.py` +35 lines**: the query-box tunnel (`8b2c97fc`, untagged), unused (no `queryBoxHost` in `Pulumi.gcs.yaml`; the box was retired in `c1b91518`) | none | — | **accidental drift on a shared file** | Delete on gcs |
| `cfn_dashboard.py` | 2 comment edits | none | — | comment drift | Revert on gcs, or fix on `cloud` |
| GHA `health.yml` | matrix over **both** hosts (runs from the default branch) | single-host copy, which never fires on schedule | — | accidental duplicate | Keep one matrix workflow (on whichever branch is the default; ideally `cloud` with hosts from a config). Delete cw's copy. Move the healthcheck into the scan job on cw (§1 row 16) |

## 4. Index and store config

| Item | gcs | cw | `cloud` default | Class | Recommendation |
|---|---|---|---|---|---|
| Path-store builder | `path-index` | `import` → `index-write` | both exist | **accidental** | One builder (§1.2) |
| By-user sorts | `-u bysize` (`bysize-user` only) | `INDEX_VARIANTS=path` (none) | `path,user` (`index_footer.py:539`) | **real** (no ownership on cw) | Profile: `user_sorts: [bysize]` / `[]`. The env default `path,user` is a gcs-ism. Make it required or derive it from the generation's files |
| Age pyramid | no | yes | — | **missing on gcs** (builder) | Falls out of §1.2 |
| Search sidecars (`-S`) | off (`PATH_INDEX_SEARCH`) | off | off | converged (r2 demo on) | — |
| dir-cache | yes | no | — | real (gcs REPROC re-attribution) | Keep, as a profile option |
| Static names | gen `2026-10-08c`, `drill=True`, **`anchors=True`**, `hex_runs="16,8"` (applies from the next compaction) | gen `2026-10-10cw`, `drill=True`, **anchors off**, `hex_runs="16,8"` | `Profile` defaults: anchors off | anchors: **accidental**. hex runs: **real** (79% vs 1.1% of suffix rows in ≥16-hex runs, `index-landscape.md` §3). Both get the rule, so it's not a divergence | CW profile `anchors=True` + build. Consider `anchors=True` as the `Profile` default |
| Static append order | before `index-sync`, 120 min timeout | after `index-sync`, background, no timeout | — | **accidental** (user-visible `scan-not-indexed` on the newest cw scan) | cw: before `index-sync`, with a timeout |
| Static profile location | `static_profile_examples.GCS` on `cloud` | `.CW` on `cloud` | examples module | fine per [[generic-code-no-deploy-defaults]] (bound examples allowed). The gens in them move with each compaction, so `cloud` commits are needed for deployment config | Move the live `gen` out of the Python examples into each branch's profile file (JSON, like `interval_profiles/`), so a compaction doesn't need a `cloud` commit |
| Interval store | `interval_profiles/gcs.json`, gen `2026-10-09b`, read by default (`PATH_STORE=intervals`) | none | — | **missing on cw**, by rollout | Decide the cross-scan structure first (next row) |
| Over-time groups | none | `over-time-groups` (K-scan multiscan groups for `/api/series`) | — | **missing on gcs** | Two cross-scan indexes, one per deployment, is the worst case. Pick one: the interval store already answers any scan from its runs; if it can serve `/api/series`, retire over-time groups (cw) and give cw the interval store. Otherwise port over-time to gcs. Root session's call, with Ryan |
| `over-time-groups` layer-2 default | — | sets `LAYER2_PREFIX` | **`cw-l2/{scan}/` baked in** (`cli.py:811`) | **accidental** (a cw default in generic code) | Required (error when unset), per [[generic-code-no-deploy-defaults]] |
| `publish.py` layer-2 default | — | sets it | **`listing/{scan}/index/`** (gcs's, `publish.py:52`) | **accidental** (a gcs default in generic code; the two defaults also disagree with each other) | Required |
| D1 retention | `-r 30` | none | — | **accidental** | §3 |
| `FILTER_INDEXED_ONLY` | on | off | off | **accidental** | §2.1 |
| Row groups | 8K | 8K | 8K | converged | — |

## 5. Code drift outside the expected deployment-only set

`git diff --name-status cloud <branch>`, files that exist on `cloud` and were *modified*:

**cw-s3** modifies only `AGENTS.md`, `README.md`, `infra/cf/README.md`, `site/index.html` and `site/public/og.jpg`. All of these are deployment identity. No shared code is edited on cw-s3. Its additions are job/infra/config, specs, two config tests and the store modules. **Clean.**

**gcs** modifies 36 files:

| File(s) | What | Class | Action (who) |
|---|---|---|---|
| `site/src/ledgerOverlay.ts` (+41), `ledgerOverlay.test.ts` | `restrictToPool`, added by `[cloud]` `139244df`/`55a981bc` and removed on `cloud` by `c930477d` (server-side pool folding). It survived on gcs through a merge. **No callers** besides its test | **dead merge residue** | Delete on gcs (gcs session) |
| `infra/cf/stack/__main__.py` (+35) | query-box tunnel, `8b2c97fc`, **untagged**, on a shared program | **untagged shared-code drift**, now dead | Delete on gcs (gcs session) |
| `site/functions/_lib/testD1.ts`, `*.test.ts` (6), `cloud/tests/test_index_{footer,stores}.py`, `site/functions/api/me.test.ts` | tests run on the `gcs` D1 lineage instead of (or beside) `cw` | **real** (gcs's D1 is its own lineage) | Keep. Goes away with one lineage |
| `site/functions/_lib/sweepReflect.test.ts` | `deletion_runs (…, plan, …)` vs `cloud`'s `manifest` | **schema drift between lineages** | Part of lineage unification |
| `site/package.json` | `devPort` 3253 vs 3263 | real (per-worktree dev port) | Keep |
| `site/index.html`, `og.jpg` | identity | real | Keep (could be generated from the store module) |
| `site/wrangler.r2.toml`, `site/migrations/cw/0004,0005,0009`, `site/src/{ChildrenTable,OwnerFactChip}.tsx`, `app.scss`, `site/functions/api/assignments.ts`, `site/e2e/guest-chip.spec.ts`, `cloud/src/dt_cloud/{access,viz}.py`, `src/disk_tree/access/__init__.py`, `specs/union-of-roots.md`, `specs/done/aggregation-extensions.md`, `infra/cf/cfn_dashboard.py` | comment-only: `gcs:specs/…` → `specs/…`, `specs/x.md` → `specs/done/x.md` (the referenced specs live on gcs). `union-of-roots.md`'s rewrite produced a broken `m/specs/cp-from-cw-s3-2026-09-16.md` | **comment drift** (spurious merge conflicts later) | Either revert on gcs, or carry the moved specs and corrected refs to `cloud` as one `[cloud]` commit (root session). Fix the `m/specs/` typo |
| `AGENTS.md`, `README.md`, `infra/cf/README.md` | identity docs | real | Keep |

Untagged gcs commits on shared code paths (non-merge, `cloud..gcs`): `8b2c97fc` (above), plus deployment-scoped test commits (`0ccd8d13`, `25dd2c38`, `d5ab2dd3`, `52688bbe`, `b44b0d53`, `6d91093d`: gcs-lineage tests, fine) and `46968900` (`metaTree` in gcs's store module, fine). No untagged *behavioral* change to shared runtime code beyond `restrictToPool`.

**Duplicated deployment files that belong on `cloud` once** (generic toolkit per [[branches-carry-exactly-their-code]]):

| File | gcs vs cw | Action |
|---|---|---|
| `job/gen-arrow-avatars.py`, `job/gen-delta-arrows.py`, `job/rerg`, `job/icons/_headers`, `job/icons/arrows/*` (34 PNGs) | byte-identical | Move to `cloud` (root session); drop from both branches at their next merge |
| `Dockerfile`, `cloudbuild.yaml`, `job/build.sh` | near-identical (cw adds `JOB` build-arg, `git`, `--extra overtime`) | One parameterized copy on `cloud` (`JOB`, `TAG`, extras) |
| `job/static-names.sh` (166 vs 178 lines) | same machinery; profile-ish differences (SA, R2 bucket, `static_roots` module, `NO_SCRATCH`) | Read SA, buckets and R2 from the static `Profile`; one copy on `cloud` |
| `.github/workflows/health.yml` | gcs's matrix covers both | One workflow (§3) |
| `job/scan-runs.json`, `job/digest.yml` | real per-deployment content | Keep (config) |
| `site/deploy.env` | 1 line (`PTR_SHORT`) | Keep (config) |

**Stale or leftover items found along the way** (gcs unless noted):
- `run.sh`: phase `webdata+stage`; the `slack_post` comment "the image has no curl" (it does).
- `batch-submit.sh`: the `CH_STORE_URL` passthrough.
- `job/serving-exp-*.sh` and `job/serving-exp-ch/*` (ClickHouse experiment; the box is retired).
- `job/bench-queries.yml`, `job/serving-box-*.sh`: check whether the serving box is still live.
- `run.sh` `ACCESS_BUCKETS` includes `marin-us-west1`, which the scan doesn't list. Real (logs for an unscanned bucket) or stale?
- cw: `cw-run.sh` stage numbering (`4b` appears twice; `4c'`); the `cw-batch-submit.sh` header still says gcs needs "highmem-16".

## 6. Prioritized convergence plan

Sessions:
- **root**: this checkout, on `cloud`;
- **gcs**: `wt/gcs`;
- **cw**: `wt/cw-s3`.

Every deployment change ships through the branch's own image rebuild and/or `pulumi up` (cron bodies come from `PIN=1 DRY=1` submitters, so a submitter change needs an `up`) and/or `site/deploy`.

| P | Step | Session | Ryan approves |
|---|---|---|---|
| **P0** | cw: move `static-names` before `index-sync` (blocking, `timeout 120m`, `concurrent` in `scan-runs.json`). This fixes the newest-scan `scan-not-indexed` | cw | image rebuild (routine) |
| P0 | cw: D1 retention. Measure MB/scan, check `.groups.parquet` presence, add `index-gc -r N` | cw | the N; prod D1 deletes |
| P0 | gcs: generation file GC, `index-gc -R -n -F gs://oa-gcs-usage-dvx` reviewed, then in the job | gcs | the dry-run's list; prod GCS deletes |
| P0 | gcs: delete `restrictToPool` and the query-box tunnel + `CH_STORE_URL` + `serving-exp-*` | gcs | — |
| P1 | Shared stage vocabulary (§1.4) on both jobs + `scan-runs.json`; `SCAN-JOB-DONE`; one-time D1 rename of historical phase names | gcs, cw (root writes the map) | prod D1 `UPDATE` |
| P1 | gcs `scan-runs.json` static URI → `{static_gen:gcs}`; live static/interval gens out of `static_profile_examples.py` into branch profile files (one SoT for `STATIC_GEN`) | root (loader), gcs, cw | — |
| P1 | `cloud` generic defaults: make `LAYER2_PREFIX` required in `over-time-groups` and `publish.py`; `INDEX_VARIANTS` from the profile | root | — |
| P1 | Move byte-identical job files + one parameterized Dockerfile/cloudbuild/build.sh to `cloud` | root, then gcs/cw drop theirs on merge | — |
| P1 | CW static profile `anchors=True`, build anchors on `2026-10-10cw`; then cw `FILTER_INDEXED_ONLY=1` | root (profile), cw (build + wrangler) | the user-visible filter change on cw; the Batch build cost |
| P1 | cw: in-job `healthcheck` + probe alert; record `reproc` runs. gcs: `preflight` bucket check (needs list permission on the fleet project) | cw, gcs | IAM grant on `hai-gcp-models` for gcs's list |
| P1 | gcs: own job grant as `SITE_TOKEN` (stop reusing the sheet-sync token); drop deprecated `GCS_USAGE_*` names from `deploy.py` afterwards | gcs, then root | minting the grant; the secret |
| P2 | One builder (§1.2): age pyramid in `path-index`; cw switches; a2a on 2–3 cw scans; RSS measured | root (builder), cw (switch) | Batch spend for the a2a runs |
| P2 | Cross-scan decision: interval store vs over-time groups; give both deployments the survivor (`interval_profiles/cw.json` or over-time on gcs) | root + Ryan | the decision; backfill cost |
| P2 | `OG_CARDS` on cw; `CACHE_NS` one convention; `SWEEP_IMAGE` pinned on cw; `STATIC_BENCH` removed | cw, gcs, root | `SESSION_SECRET` on cw's Pages |
| P3 | `dt-cloud scan-job` driver + `submit` + profiles (§1.5); cut-over with one parallel run each; `cw-meta` as a third profile with a scan-run record | root (driver), gcs + cw (profiles, cut-over) | `pulumi up` of both crons |
| P4 | gcs per-scan path store on R2 (`publish-r2` stage + backfill, `STORE_BUCKET` → R2) | gcs | R2 storage + egress-free copy cost; site switch |
| Later | Unify the D1 lineages (`migrations/gcs` vs `migrations/cw`: same tables, different numbering, `deletion_runs.plan`/`manifest`, `admin_emails`, `pyramid_multiscans`); unify executors (`sweep` vs `plan-sweep`); move shared buckets to a `shared` stack | root + Ryan | each is a design decision |

### 6.1 Done on `cloud` (branch `no-defaults`): no deployment defaults, one static-gen source

Recommendations from §4 rows "By-user sorts", "Static profile location", "`over-time-groups` layer-2 default" and "`publish.py` layer-2 default", plus the other deployment-shaped defaults a grep of `cloud/src` turned up. Each is now the deployment's to name, and a missing one is an error naming the flag or variable ([[generic-code-no-deploy-defaults]]).

| What | Was | Now |
|---|---|---|
| `publish-r2` layer 2 | `$LAYER2_PREFIX`, else gcs's `listing/{scan}/index/` (`publish.py`) | `-l`/`$LAYER2_PREFIX` required (`deploy.layer2_prefix`; must contain `{scan}`), resolved before any D1 read |
| `over-time-groups` layer 2 | `-l`/`$LAYER2_PREFIX`, else cw's `cw-l2/{scan}/` | same requirement |
| `$INDEX_VARIANTS` | read at import, default gcs's `path,user` | **not read**. The variant set is the generation's files: `index-sync`/`index-blob` already skip absent files, and `index-gc -r` retires only the sort rows D1 holds (`SORT_VARIANTS` = all four sorts). Deriving beats a required knob: nothing to set, nothing to drift. Dead `INDEX_VARIANTS` lines dropped from `daily-ingest.yml` and `wrangler.r2*.toml` (the site never read it) |
| `index-sync -k` | default `listing/<date>/index/<gen>` (gcs's layout) | required |
| `index-blob -g` | built `listing/<date>/index/<gen>` | `-g` needs `-k`; `-k` is a template (`{date}`, `{gen}`) |
| bare bucket → GCS | `index_footer._remote`: paths starting `oa-` were GCS (an OA bucket-name heuristic); any other bucket name was a *local relative path* (so `index-gc -r` on a non-`oa-` `$DATA_BUCKET` found no cold footer and kept every row) | `bucket_base`: a bare name is `gs://<name>`; URLs and `/`- or `.`-paths as given; `index-sync` without `-d` reads `gs://<bucket>/<key>` |
| digest template | `config_from_dict` defaulted `template` to `gcs`; `digest_plot -T` defaulted `gcs` | both required |
| static-names live gen | in `static_profile_examples.GCS/CW` (`2026-10-08c`, `2026-10-10cw`): every compaction needed a `cloud` commit | examples carry **no `gen`** (illustrative). A deployment's live profile is a JSON file on its branch, `STATIC_NAMES_PROFILE=<path>`. New `dt-cloud static-names profile [-f FIELD]` prints the resolved profile (start the file from it) or one field |
| `{static_gen:<profile>}` | example name only (`[\w.-]+`) | also a file path; a relative one resolves against the scan-run profile's own dir (anchored at load), so `job/scan-runs.json` names `{static_gen:static-names.json}`. A profile with no gen is an error, not `None` in the URI |
| site `STATIC_GEN` | hand-copied into `wrangler.toml` | still a wrangler var (Pages takes vars only from `wrangler.toml`), but `site/deploy` refuses when any `STATIC_GEN` differs from the file's `gen`, when `deploy.env` sets `STATIC_NAMES_FILE=job/static-names.json` |

So a compaction is one branch commit: bump `gen` in `job/static-names.json`, set `STATIC_GEN` to match (deploy checks), rebuild the image. `scan-runs.json` follows by itself.

Not changed (deliberately, follow-ups):
- `plan-sweep manifest`'s `-l` default `/gcs/<data>/cw-l2/<date>/<bucket>.parquet`: the whole `plan-sweep` executor (CLI + `site/functions/_lib/{planDispatch,cwBatch,executor}.ts`: `sweep/cw/runs/`, `cw-s3-job-grant`, `EXECUTOR` default `plan-sweep`) is cw-shaped; converge it with the executor unification (§6 "Later"), plumbing `LAYER2_PREFIX` into the Batch env then. **Bug found on the way:** `planDispatch.ts` runs `dt-cloud plan-sweep manifest --plan "$RUN/plan.json" …`, but the command takes the plan as a positional argument and has no `--plan` option (since `a02b24fd`, 9/28), so a cw plan-sweep dispatch fails: `dt-cloud plan-sweep manifest --plan x.json -d … -o …` exits 2 with "No such option '--plan'" (checked locally; not checked against a live run).
- `interval_profiles/gcs.json` (live interval gen `2026-10-09b`) is still on `cloud`. Its loader already takes a path (`-P`/`$INTERVAL_STORE_PROFILE`); same move as static: copy it to `job/interval-store.json` on gcs, point the job at it, then drop it from `cloud`.
- `UA = "gcs-usage-…"` in `healthcheck.py`/`site.py`, `scan-started --listing-prefix listing` (the generic lister's layout, not a deployment's) and `GCS_USAGE_ATTR_MAX_DEPTH`: naming, not behavior.

#### What each deployment must do before merging this

Checked against `gcs` @ `60b6e1b5` and `cw-s3` @ `9511238a` (job scripts, Batch submitters, `scan-runs.json`, workflows, wrangler).

Already fine (no action):
- `-k` on every `index-sync` call: gcs `run.sh`; cw `cw-run.sh`, `cw-meta-run.sh`, `cw-reindex.sh`; both copies of `daily-ingest.yml`.
- `LAYER2_PREFIX` wherever cw runs `publish-r2` or `over-time-groups`: `cw-run.sh`, `cw-meta-run.sh`, `cw-overtime.sh`, `cw-publish-submit.sh`. gcs runs neither.
- `INDEX_VARIANTS`: gcs never set it; cw's `export INDEX_VARIANTS=path` (`cw-run.sh`, `cw-meta-run.sh`, `cw-reindex.sh`) and the `INDEX_VARIANTS` lines in its wrangler tomls are now no-ops. Drop them at leisure. cw's `index-sync` now also probes `path-index{,-bysize}-by-user.parquet` and lists them as skipped (cw's `index-write` writes no by-user sort: no `usr` column).
- `index-gc -r` on gcs: `$DATA_BUCKET=oa-gcs-usage-dvx` resolves to `gs://oa-gcs-usage-dvx` as before.
- `job/digest.yml` names `template:` on both.

**Must add before merging** (otherwise every static-names append fails with `static names: no gen in the deployment profile: set STATIC_NAMES_GEN (or STATIC_NAMES_PROFILE)`, and `scan-run record`'s `static` output errors):

gcs:
1. `job/static-names.json`: `STATIC_NAMES_PROFILE=gcs STATIC_NAMES_GEN=2026-10-08c dt-cloud static-names profile > job/static-names.json` (on the merged code; check `anchors: true`, `hex_runs: "16,8"`, `compact_level: 5` came through; `image` stays `null`, the caller passes `STATIC_NAMES_IMAGE`).
2. `job/run.sh` (the `static-names runs add` call ~l.408 and the resume hint in the Slack message ~l.414) and `job/static-daily.sh`: `STATIC_NAMES_PROFILE=gcs` → `STATIC_NAMES_PROFILE=/app/job/static-names.json` in the image (or `"$(dirname "$0")/static-names.json"`, which works both in the image and in a checkout).
3. `job/scan-runs.json` `static` output: `{static_gen:gcs}` → `{static_gen:static-names.json}`.
4. `site/deploy.env`: `STATIC_NAMES_FILE=job/static-names.json` (then `site/deploy` checks both `STATIC_GEN = "2026-10-08c"` lines).

cw:
1. `job/static-names.json`: `STATIC_NAMES_PROFILE=cw STATIC_NAMES_GEN=2026-10-10cw dt-cloud static-names profile > job/static-names.json` (`anchors` per the P1 row when that build lands).
2. `job/cw-run.sh` (~l.240): `STATIC_NAMES_PROFILE=cw` → the file (as gcs 2).
3. `job/static-names.sh`: both Batch envs (`ENV_JSON` ~l.99 and the default `ENVJ` ~l.125) set `"STATIC_NAMES_PROFILE": "cw"` → `"/app/job/static-names.json"` (an absolute in-image path: the tasks `cd /stage`). This needs the tasks to run the branch's own image, which has `/app/job/`; with a staged `SRC` the image's `/app/job` is still there.
4. `job/scan-runs.json` `static` output: `{static_gen:cw}` → `{static_gen:static-names.json}`.
5. `site/deploy.env`: `STATIC_NAMES_FILE=job/static-names.json`.

Any operator `.envrc` or ad-hoc command with `STATIC_NAMES_PROFILE=gcs|cw` keeps working only with `STATIC_NAMES_GEN` beside it.

## Open questions and unverified claims

- Whether cw generations already carry `.groups.parquet` (needed by `-r`). Inferred from the shared cut in `index.py`, not checked in GCS.
- cw's D1 bytes per scan. Inferred to be ~2/3 of gcs's per-row footprint (2 sorts vs 3); not measured.
- Whether the cw cron body with `STATIC_NAMES=1` has been applied with `pulumi up` (`fd48ed4c` is today). The text assumes the submitter is what runs.
- Whether anything reads `cw-l2/<scan>/<bucket>.parquet` besides ad-hoc analysis (decides whether §1.2 can drop them).
- Whether the interval store can serve `/api/series` well enough to retire over-time groups. That's a design call, not checked here.
- `ACCESS_BUCKETS` including `marin-us-west1`: intentional?
