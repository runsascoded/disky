# Retiring the ch-store VM

Status: **mostly executed** (2026-10-10). What ran, by runbook step:

| Step | State |
|---|---|
| 1 Site stops referencing the box | done: gcs prod + dev drop every `QUERY_BOX_*` var (gcs `b667b554`); `/names` answers from the static index |
| 2–3 Timer, containers | implied by step 5 |
| 4 Keep what is not derived | **skipped, data lost**: the VM was deleted from gcs's own inventory before this runbook was read, with no snapshot and no `vm-final/` copy. The ~5 GB of non-derived `/data` artifacts (bench-*, hot-l1-catalog, dated-l1-*, hot-frequency-sweeps, prof, ch-native builds) are gone; what remains is `gs://oa-gcs-usage-dvx/scratch/bench/ch-store/` (1.6 GB: inputs, native-l2-stage, queries, scripts, src, static, test-checkout) and the conclusions in `done/ch-store.md` |
| 5 Delete the VM and disk | done ~15:40 UTC (`job/ch-store.sh delete`); no instance or disk remains |
| 6 Tunnel + DNS | done: `pulumi up -s gcs`, 3 deleted; `gcs-query.oa.dev` no longer resolves; the `query_host` block stays as an unset opt-in |
| 7 Account-wide R2 token | **open**: the hash compare is impossible (`/data/r2-index.env` went with the disk); find it in the dashboard by elimination (not `gcs-static-index-r2-*`, m3's or the r2 demo's; likely created ~10-08/09 for the VM's static-names setup), then roll or delete it (Ryan) |
| 8 GCS staging | **open**: now the only surviving copy, so the suggestion is to keep `scratch/bench/ch-store/` (1.6 GB) — Ryan's call |
| 9 Code | done: `cloud` `c41a5778` (tag `ch-store-final` = `fc9aeec7`), gcs `b667b554` / `c1b91518` |
| 10 Local and memory | done (2026-10-10): worktrees `wt/ch-daily`, `wt/ch-retire`, `wt/ch-retire-gcs`, `wt/ch-tunnel`, `wt/ch-remove` and `wt/ch-store` removed, their ignored notes and logs moved to `tmp/archive-<name>/` first (`wt/ch-store`'s 2.1 GB — `tmp/` incl. the local ClickHouse binary and servers' data, `.dvc/cache`, `dvx.db`, the ignored DVX outputs under `static-names/2026-10-08{,c}/` — to `tmp/archive-ch-store/`, after stopping its two orphaned local ClickHouse servers, started 10-07); branches archived as annotated tags `archive/ch-daily` (`5a7039b6`), `archive/ch-remove` (`db5e97c8`), `archive/ch-retire` (`70a2b477`), `archive/ch-retire-gcs` (`8f47b8d3`, superseded on `gcs` by `f00b6210`), `archive/ch-tunnel` (`2edea467`), `archive/ch-store` (`33557ba6`) and deleted; `tmp/ch-store-ip` deleted. Kept: branch `catalog-append` (2 static-catalog commits not on `gcs`; its worktree removed, its `tmp/` in `tmp/archive-catalog-append/`), remote `o/ch-store` (`73f8a266`), and the agent worktree `.claude/worktrees/agent-ac91f1f5…` (`exp/ch-suspend`) |

Move this spec to `done/` once 7, 8 and 10 are settled. Prepared 2026-10-09: Ryan agreed to retire the VM once the static name search has run on gcs prod for a few days. This spec is the inventory, the preconditions, and the ordered runbook.

The VM was the experiment platform for an append-only historical query service ([`ch-store.md`], [`filter-query-service.md`], [`serving-options.md`]). It ended up serving one production-adjacent thing, the name search, which now comes from the static name index on R2 (`specs/static-append.md` on `gcs`, `specs/architecture/static-name-search.md`). Nothing in gcs prod reads from the VM any more; its last jobs are the hourly `ch-daily` catch-up and, until the code change below, the R2 copy in `job/static-daily.sh`.

## Inventory

Checked on 2026-10-09 around 12:00 UTC (read-only ssh, `gcloud … describe/list`, anonymous `curl`).

### GCP

| Item | Detail | Created by | Owner | Depends on it |
|---|---|---|---|---|
| VM `ch-store` | us-east1-c, n2-highmem-8 (8 vCPU, 64 GB), on-demand (not spot), RUNNING since 2026-10-04, label `purpose=ch-store`, ephemeral premium external IP (in `tmp/ch-store-ip`, untracked) | `job/ch-store.sh create` (gcloud, **not Pulumi**; nothing in `infra/gcp` references it) | gcs session | see below |
| Disk `ch-store` | the boot disk, **2,000 GB pd-ssd**, `autoDelete: true`, no snapshot schedule, no snapshots. 505 GB used: ClickHouse 475 GB (`default` 174 GiB, `narrow_fleet_stream_oct05*` 202 GiB, `narrow_central2_*` 61 GiB, `daily_scalar_oct06_*` 28 GiB, smaller experiment DBs), `/data` 20 GB (of which `/data/in` 15 GB is ingest staging) | same | gcs session | the VM |
| Service account | the VM runs as `gcs-usage-job` (the shared job account; also Batch's). No VM-only account, no key files | `infra/gcp` | gcs | **keep**: Batch uses it |
| GCS staging | `gs://oa-gcs-usage-dvx/scratch/bench/ch-store/` (1.53 GiB: staged `dt_cloud` / `disk_tree` sources, node scripts, test checkout) | `job/ch-store.sh push`/`test` | gcs session | the VM only |
| Firewall, static IP | none specific (default `default-allow-ssh` only; the tunnel needs no inbound port) | | | |

### On the VM

| Item | State | What it does | Depends on it |
|---|---|---|---|
| ClickHouse server | `active` | the consolidated store, name postings (`m`), catalog, narrow/daily-scalar experiment DBs | `serve-query`, `ch-daily`, `ch-answers` |
| `serve-query` container | up (restarted 08:51 UTC by `ch-daily`) | `dt-cloud serve-query -e ch` on :8080, bearer `/data/token`: hot L1/L2, name summary, dated names, narrow history, coarse | the named tunnel; gcs **preview** (dev.gcs.oa.dev) via `QUERY_BOX_HOT_L1_URL`. In the 3 h before the check its log had **no requests** besides one `/healthz` |
| `ch-named-tunnel` container | up 16 h | `cloudflared` for the named tunnel `gcs-query.oa.dev` (token in `/data/tunnel.env`) | `gcs-query.oa.dev` answers 200 on `/healthz` (unauthenticated: scan list) |
| `ch-daily.timer` / `.service` | enabled, hourly; last run 12:00 UTC exit 0, "store through 2026-10-09; to do: nothing" | `/data/daily.sh` (`job/ch-store/daily.sh`): ingest each new gcs scan, `ch-mega-names-append`, `ch-mega-catalog-build`, restart `serve-query` | nothing else; superseded by `job/static-daily.sh` |
| `/data/r2-index.env` | 138 bytes, `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | an **account-wide** R2 key (every bucket in the account) used by `job/static-names.sh r2` | that subcommand only. Superseded by the bucket-scoped Secret Manager key `gcs-static-index-r2-{key-id,secret}` (version 1 of each enabled 2026-10-09 11:18 UTC) |
| `/data/sn/src`, `/data/sn/{ch-answers.py,terms.txt}` | 2.6 MB | the staged source tree for `static-names.sh r2`; the `ch-answers` reference script | those two subcommands |
| `/data/token`, `/data/tunnel.env`, `/data/daily.env`, `/data/image` | small | box bearer, tunnel token, daily config, image ref | the containers |
| Other `/data` | ~5 GB: bench / measurement JSONL (`bench-*`), `hot-l1-catalog`, `dated-l1-*`, `hot-frequency-sweeps`, `prof`, `ch-native` builds, `static`, `static-proto` | measurement artifacts cited by `ch-store.md` | none live |

### Cloudflare

| Item | Where | Owner | Depends on it |
|---|---|---|---|
| Named tunnel `gcs-query-box` + its config (ingress `gcs-query.oa.dev` → `http://localhost:8080`) | `infra/cf` gcs stack, `queryBoxHost` in `Pulumi.gcs.yaml` (gcs branch); `ZeroTrustTunnelCloudflared` / `…Config` in `stack/__main__.py` | gcs | `ch-named-tunnel` |
| DNS CNAME `gcs-query.oa.dev` → `<tunnel>.cfargotunnel.com` (proxied) | same stack (`query-box-cname`) | gcs | the preview's `QUERY_BOX_HOT_L1_URL` |
| Stack outputs `query_box_host`, `query_box_tunnel_token` (secret) | same | gcs | `job/ch-store.sh tunnel` |
| Account-wide R2 API token (the one in `/data/r2-index.env`) | made by hand in the dashboard, not in any stack | Ryan | `static-names.sh r2` only |
| Pages **preview** vars (`site/wrangler.toml` `[env.preview.vars]` on gcs-lineage branches) | `QUERY_BOX_URL` (a trycloudflare quick tunnel, **already dead**: DNS fails), `QUERY_BOX_TIMEOUT_MS`, `QUERY_BOX_COARSE`, `QUERY_BOX_HOT_L1`, `QUERY_BOX_HOT_L2`, `QUERY_BOX_NAME_SUMMARY`, `QUERY_BOX_DATED_NAMES`, `QUERY_BOX_HOT_L1_URL = https://gcs-query.oa.dev` | gcs | dev.gcs.oa.dev's `/hot`, `/coarse`, box hand-off on `/api/{subtree,diff,series}` (falls back to the Worker on the dead quick tunnel) |
| Pages **preview** secret `QUERY_BOX_TOKEN` | `wrangler pages secret list --env preview` | gcs | same |
| Pages **production** vars | `QUERY_BOX_NAME_SUMMARY = "1"`, `QUERY_BOX_DATED_NAMES = "1"`: route gates only. No `QUERY_BOX_URL` / `QUERY_BOX_HOT_L1_URL` var and no `QUERY_BOX_TOKEN` secret in production (checked: 10 secrets, none box), so prod cannot reach the box | gcs | `/api/name-summary(-registry)` were gated on them (cut below) |

### Code

On `cloud` (shared base; every deployment merges it):

| Path | Size | Live caller |
|---|---|---|
| `cloud/src/dt_cloud/chstore/` | 76 modules | `cli.py` only (lazy imports) |
| `cloud/src/dt_cloud/box/` (`server.py`, `view.py`: the `mem` / `ch` serving box) | 3 modules (chstore + box: 19.4K lines) | `cli.py serve-query` |
| `dt-cloud` CLI: `serve-query` + 73 `ch-*` commands (`ch-ingest`, `ch-narrow-*`, `ch-hot-*`, `ch-mega-*`, `ch-dated-*`, `ch-daily-*`, `ch-order-key-*`, …) | in `cli.py` (5,355 lines total) | the VM's scripts |
| Tests: `cloud/tests/test_ch*.py` (83, incl. `test_chstore.py`, `test_chserve.py`, `test_chnarrow.py`), `test_box{,_admission,_stream}.py`, `test_name_summary.py`, `chserver.py` | 22.2K lines | — |
| `deploy/serve-query/Dockerfile` | | — |
| Site box hand-off: `site/functions/_lib/queryBox.ts` (`QUERY_BOX_URL` → `/api/{subtree,diff,series}`), `api/coarse.ts` (`QUERY_BOX_COARSE`), `_lib/hotL1.ts` / `_lib/hotL2.ts` + `api/hot-l1.ts` / `api/hot-l2.ts` (`QUERY_BOX_HOT_L1/L2`, `QUERY_BOX_HOT_L1_URL`), `_lib/nameSummary.ts` `askNameBackend`; their tests; `wrangler.example.toml` box lines | | preview only |
| Site pages `/coarse` (`CoarsePage`, `coarseModel`) and `/hot` (`HotPage`, `HotMaps`, `HotDrill`, `hot*Model`) | | box only (their APIs 404 when the flags are off) |

`view.ts` has no box path: the map's filter (`q=`) reads the static index (`staticFilter.ts`) or the path store. `/names` (`NamePage`) stays: it reads `/api/name-summary`, which the static index answers.

On gcs-lineage branches (`gcs`, `gcs-static`):

| Path | Live caller |
|---|---|
| `job/ch-store.sh`, `job/ch-store/*` (37 files: `daily.sh`, `ingest-days.sh`, SQL, benches) | the VM, the `ch-daily` timer |
| `job/static-names.sh r2` (VM copy, `/data/r2-index.env`; carries the R2 endpoint) | manual fallback only, after `ch-retire-gcs` (below) |
| `job/static-names.sh ch-answers` + `job/static-names/ch-answers.py`, `catalog-ch-terms.txt` | manual reference answers; DVX stages `static-names/2026-10-08/ch-answers.jsonl.dvc`, `2026-10-08c/ch-catalog-answers.jsonl.dvc` (done, cached) |
| `job/static-daily.sh` `R2_VIA=vm` | **removed** on `ch-retire-gcs` |
| `job/run.sh` opt-in `ch-ingest` (`CH_STORE_URL`; passed through by `batch-submit.sh`) | none: the Cloud Scheduler body of `gcs-usage-snapshot-daily` carries no `CH_STORE_URL` |
| `job/serving-exp-ch.sh`, `job/serving-box-*.sh` (earlier experiments' VMs, none running) | none |
| `infra/cf` `queryBoxHost` block | the tunnel and DNS |
| `CLAUDE.md` "ClickHouse development" section | sessions |

Local only: worktrees `wt/ch-store`, `wt/ch-store/tmp/wt-append`, `wt/ch-daily`, `wt/ch-tunnel`; `tmp/ch-store-ip`.

## Dependency cuts made now (local commits, no deploys)

1. **`ch-retire-gcs`** (off `gcs-static`, `wt/ch-retire-gcs`): `job/static-daily.sh` always copies to R2 with the Batch job (`static-names.sh r2-batch`, the bucket-scoped Secret Manager key); `R2_VIA` and the VM path are gone. A run without `R2_ENDPOINT` / `CLOUDFLARE_ACCOUNT_ID` exits 2 before any stage. `static-append.md`'s R2 bullet updated. `static-names.sh r2` stays as a manual fallback until `r2-batch` has a live run, then goes with the VM.
2. **`ch-retire`** (off `cloud`, this branch): `/api/name-summary(-registry)` are on with `NAME_SUMMARY_STATIC=1` + `INDEX_R2` alone, and the static index makes requests dated (`namesEnabled`, `datedNames` in `_lib/nameSummary.ts`), so gcs prod can drop `QUERY_BOX_NAME_SUMMARY` / `QUERY_BOX_DATED_NAMES`. With gcs prod's current vars the behaviour is unchanged. `site/functions/api/no-box.test.ts` pins it: with prod's vars, and with the preview's box URL + token still set, a failing R2 answers 503 and `fetch` is never called; without the static index or the box flag the routes 404; `hot-l1` / `hot-l2` / `coarse` with their flags off 404 without a fetch. Test-fail-fix-pass: the prod-vars case failed (404) before the change. `/api/{subtree,diff,series}` with `QUERY_BOX_URL` unset were already covered (`queryBox.test.ts`, "never asks a box").

## Preconditions (all true before step 1 of the runbook)

1. **Prod static search live N days with no box fallback.** It went live on gcs.oa.dev on 2026-10-09 (`617ee2b5`, the `gcs-prod` pointer). Suggested N = 3, so not before 2026-10-12. Check:
   - `/api/name-summary` answers carry `x-query-engine: name-summary-static` (never `name-summary`), and its 503 rate is near zero (Workers logs: `static name summary failed`).
   - The box saw no prod traffic: `job/ch-store.sh sh 'sudo docker logs --since 72h serve-query 2>&1 | grep -v healthz | wc -l'` is 0 (prod has no box endpoint or token, so this should hold by construction).
2. **The R2 copy runs on Batch.** `R2_VIA=batch` is the only path (cut 1, merged into `gcs`). As of 2026-10-09 no `sn-r2-*` Batch job has ever run (that day's copy went through the VM), so: at least one live `job/static-daily.sh D` whose `r2-*` stages are Batch jobs that succeeded, ideally the N days of precondition 1. `static-daily` should also be on a schedule that is not the VM (today it is run by hand).
3. **No prod var references the box.** Cut 2 merged into `gcs` and deployed, then `QUERY_BOX_NAME_SUMMARY` / `QUERY_BOX_DATED_NAMES` removed from `[vars]` and deployed; `/names` still answers. Production secrets already have no `QUERY_BOX_TOKEN`.
4. **No pipeline writes to the store.** `CH_STORE_URL` unset in the snapshot job (true today).
5. **`ch-answers` is not needed.** The static pipeline's acceptance is brute force from the scan files (`daily verify`, `verify-*.json`); ClickHouse answers were a second reference for 2026-10-08 / 2026-10-08c only, and those DVX outputs are cached. Agree they need no rerun.
6. **No session is mid-experiment on the VM** (the `ch-store` / `ch-daily` worktree sessions): `job/ch-store.sh py-status` for any running tag, `docker ps` shows only the two containers.

## Runbook

Run from `wt/gcs` (its direnv carries the OA CF / GCP credentials) unless noted. Steps 1–2 are reversible; from 5 on they are not.

1. **Site: stop referencing the box.** (needs Ryan's go: deploys)
   - Merge `ch-retire` (the `[cloud]` commit) into `cloud`, then `cloud` into `gcs`.
   - Merge `ch-retire-gcs` into `gcs-static` / `gcs`.
   - `site/wrangler.toml` on `gcs`: delete `QUERY_BOX_NAME_SUMMARY`, `QUERY_BOX_DATED_NAMES` (and their comment) from `[vars]`; from `[env.preview.vars]` delete `QUERY_BOX_URL`, `QUERY_BOX_TIMEOUT_MS`, `QUERY_BOX_COARSE`, `QUERY_BOX_HOT_L1`, `QUERY_BOX_HOT_L2`, `QUERY_BOX_NAME_SUMMARY`, `QUERY_BOX_DATED_NAMES`, `QUERY_BOX_HOT_L1_URL`, and fix the `NAME_SUMMARY_STATIC` comment ("unset = the query box").
   - `site/deploy --dev`, check dev.gcs.oa.dev `/names` and a filtered map; then `site/deploy`, same checks on gcs.oa.dev.
   - `wrangler pages secret delete QUERY_BOX_TOKEN --project-name oa-gcs-usage --env preview`.
2. **Stop the timer.** `job/ch-store.sh daily-timer off`. (needs Ryan's go)
3. **Stop the containers.** `job/ch-store.sh sh 'sudo docker update --restart=no serve-query ch-named-tunnel && sudo docker stop serve-query ch-named-tunnel'`. `gcs-query.oa.dev` then returns Cloudflare's tunnel error. Optionally `sudo systemctl disable --now clickhouse-server`. Leave it a day if anything might still call it; restarting is `docker start` (or `job/ch-store.sh serve` / `tunnel`). (needs Ryan's go)
4. **Keep what is not derived; snapshot or not.** (needs Ryan's go)
   - Recommended: **no disk snapshot.** Every ClickHouse table is derived from the scans in `gs://oa-gcs-usage-dvx/listing/` and can be rebuilt (`job/ch-store/ingest-days.sh`, the `ch-*` builds), and the experiments' conclusions are in `ch-store.md`. A standard snapshot of the 505 GB used would cost ≲ $25/month (≲ $11 archive, 90-day minimum).
   - Copy the small, non-derived artifacts instead (~5 GB: everything in `/data` except `in/`, `src*/`, `test-checkout/`, `tmp/` and the secret files): `job/ch-store.sh sh 'cd /data && sudo gcloud storage rsync -r -x "^(in|src|src-a|sn|sn-pyrmts|test-checkout|tmp)/.*|.*\.env$|^token$" . gs://oa-gcs-usage-dvx/scratch/bench/ch-store/vm-final/'` (≈ $0.10/month).
5. **Delete the VM and its disk.** `job/ch-store.sh delete` (`gcloud compute instances delete ch-store --zone us-east1-c --delete-disks=all`, then lists any leftover `purpose=ch-store` instance or `ch-store*` disk; both must be empty). This also destroys `/data/r2-index.env`, `/data/token` and `/data/tunnel.env`. (needs Ryan's go)
6. **Delete the tunnel and DNS** via `infra/cf` (on `gcs`): remove `gcs-usage-cf:queryBoxHost` from `Pulumi.gcs.yaml` (and the `query_host` block from `stack/__main__.py`, or keep the block as an opt-in that is now unset), commit, `pulumi preview -s gcs` must show exactly 3 deletes (`query-box-tunnel`, `query-box-tunnel-config`, `query-box-cname`) and the two outputs gone, then `pulumi up -s gcs`. Check `dig gcs-query.oa.dev` is NXDOMAIN. (needs Ryan's go)
7. **Revoke the account-wide R2 token.** In the dashboard (R2 → Manage API tokens) or the account-tokens API, find the token whose Access Key ID is the one that was in `/data/r2-index.env` (before step 5, compare by hash, not by printing: `job/ch-store.sh sh "sudo sed -n 's/^AWS_ACCESS_KEY_ID=//p' /data/r2-index.env | tr -d '\n' | sha256sum"`), confirm nothing else uses it (the Batch copy uses `gcs-static-index-r2-*`; m3 and the r2 demo have their own tokens), and roll or delete it. (needs Ryan's go)
8. **GCS staging.** `gcloud storage rm -r gs://oa-gcs-usage-dvx/scratch/bench/ch-store/` except `vm-final/` from step 4. (needs Ryan's go)
9. **Code** (see the next section): delete on `cloud` and on `gcs`, one commit each side, after a tag. (needs Ryan's go to merge into deployments)
10. **Local and memory.** Remove worktrees `wt/ch-store` (and its `tmp/wt-append`), `wt/ch-daily`, `wt/ch-tunnel`; archive their branches as tags (`archive/ch-store`, …) and delete the branches; delete `tmp/ch-store-ip`; update the auto-memory (`serving-box-owner`, `path-store-status`) and `CLAUDE.md` on `gcs`. (needs Ryan's go: branch deletion)
11. **Move this spec to `specs/done/`** with the dates each step ran.

## Deleted vs kept in git

Recommendation: delete the code from the trees, keep it reachable by tag. Nothing live calls it after the cuts above; it is ~19K lines of source and ~22K lines of tests that every `pytest` collection and every reader still pays for.

- **Tag first:** `git tag ch-store-final <cloud commit before the deletion>` (and `ch-store-final-gcs` on `gcs`), so `git show ch-store-final:cloud/src/dt_cloud/chstore/narrow.py` recovers any of it.
- **Delete on `cloud`:** `cloud/src/dt_cloud/chstore/`, `cloud/src/dt_cloud/box/`, `serve-query` and the 73 `ch-*` commands in `cli.py`, `cloud/tests/test_ch*.py`, `test_box*.py`, `test_name_summary.py`, `chserver.py`, `deploy/serve-query/`; on the site, `_lib/queryBox.ts` and the hand-offs in `api/{subtree,diff,series}.ts`, `api/coarse.ts`, `_lib/hotL1.ts` / `hotL2.ts` and `api/hot-l1.ts` / `hot-l2.ts` (keep `hotParams` / `privateHeaders` / `HOT_SCOPE`, which the static path imports: move them), `askNameBackend` in `_lib/nameSummary.ts` (the static index becomes the only backend; `QUERY_BOX_*` disappear from `NameSummaryEnv`), the `/coarse` and `/hot` pages and their models and tests (`/names` imports from `hotModel.ts`: keep what it uses), the box lines of `wrangler.example.toml`, and `no-box.test.ts` itself once there is no box code left to call.
- **Delete on `gcs`:** `job/ch-store.sh`, `job/ch-store/`, `static-names.sh r2` and `ch-answers`, `job/static-names/ch-answers.py`, `catalog-ch-terms.txt`, the `CH_STORE_URL` block in `job/run.sh` and its `batch-submit.sh` passthrough, `job/serving-exp-ch.sh` (and `serving-box-*.sh` if no other experiment needs them), the `infra/cf` query-box block (step 6), the `CLAUDE.md` ClickHouse section.
- **Keep:** `specs/ch-store.md` (the measurement journal), `filter-query-service*.md`, `serving-options.md`, `specs/architecture/*` (update their status lines to "retired 2026-10-xx"), and the DVX stages under `static-names/2026-10-08{,c}/` whose `cmd`s name `static-names.sh r2` / `ch-answers`: they are provenance for cached outputs and are not rerun.

## Cost

List prices from the Cloud Billing catalog (2026-10-09; this project has no BigQuery billing export, so these are not billed amounts), 730 h/month.

| Item | Rate | Monthly |
|---|---|---:|
| n2-highmem-8, us-east1, on-demand | 8 × $0.031611 + 64 GiB × $0.004237 = $0.5241/h | $382.56 |
| (with the full-month N2 sustained-use discount, up to 20%) | | (≈ $306) |
| 2,000 GB pd-ssd | $0.17/GB-month | $340.00 |
| External IPv4 on a standard VM | $0.005/h | $3.65 |
| Steady egress (box, tunnel, ssh: 0.1–0.7 GB/day per Cloud Monitoring `sent_bytes_count`) | ~$0.12/GB, premium tier, first TiB (published list) | ~$2 |
| GCS staging (1.53 GiB) | | ~$0.03 |
| **Total** | | **≈ $728 list (≈ $650 with sustained use), ≈ $24/day** |

Not saved: the GCS → R2 copies. The VM sent 617.7 GB in the 24 h to 12:00 UTC on 2026-10-09 (the 2026-10-08c base and drilldown, ≈ $74 of egress); the same bytes leave us-east1 from the Batch copy now. Steady state that is ~1.1 GB/day (`static-append.md`). The replacement pipeline (`static-daily`) costs ≈ $0.53/day (≈ $16/month), so the net saving is **≈ $710/month at list (≈ $635 with sustained use)**.

[`ch-store.md`]: done/ch-store.md
[`filter-query-service.md`]: done/filter-query-service.md
[`serving-options.md`]: serving-options.md
