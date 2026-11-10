# Scan runs in the site UI

**Per-scan cost diagnostics (2026-10-13):** `dt-cloud scan-run cost -c job/scan-runs.json -s EXACT_SCAN [-l] [-j -o FILE] ROOT_JOB_NAME_OR_UID` is a read-only parent/child resource report, documented in [cost-labels.md](cost-labels.md). It joins root identity to propagated labels, includes detached compactions/retries and distinguishes unpriced/ongoing work. Its observed compute estimate excludes disks, startup/shutdown, operations/transfers and actual billing discounts; it does not overwrite the UI's existing per-job estimate or claim the billing export is enabled. The first rollout is CLI-only; no cost widget or D1 migration is included.

Show each scan job's run the way `/staged` shows deletion runs: when it ran, how long each phase took, what it wrote (with links to the `/meta` tree and the map at that scan), what changed in each structure against the previous scan, and the job's Batch and log links.

## Status

Done. Every rollout step landed on both deployments: the migrations (gcs `0040_scan_runs.sql`, cw `0016_scan_runs.sql`), `job/scan-runs.json` and the `scan_run` glue in gcs `job/run.sh` and cw-s3 `job/cw-run.sh`, and `META_TREE_URL` in both `wrangler.toml`s. Later work extends it: concurrent phases (`f4bc4885`), cost labels (`53df1e93`), per-store runs (`bcbe370c`), jobs outside Batch (`a6f56737`). As built, before the rollout:

- Built on `scan-runs` (off `cloud`): migration, `dt-cloud scan-run record | backfill`, `/api/scan-runs`, `/scans` and `/scans/<run>`, tests.
- Gated on Ryan's go (§Rollout), since given: applying the migration to a deployment's D1 (gcs's prod D1 `oa-gcs-usage-auth` is also dev.gcs.oa.dev's, so a dev deploy needs it too) and the backfill's writes. Until then the backfill has run read-only into local SQLite, and the UI was checked on a local stack (`gcs` + `scan-runs`, `./dev --local-db`, the local D1 seeded from the backfill's `-n` SQL).
- The job glue (§Job glue) is for the deployment sessions to apply on `gcs` / `cw-s3`.

## Decisions

### Active phase timing (2026-10-13)

`record -A -P NAME` starts a phase with `status=running` and no end timestamp. It measures no outputs. Completion with `-P NAME` preserves the explicit start, and repeated starts do not reopen a completed phase or run. A failed job exit closes remaining active phases as failed. `job/run.sh` emits `PHASE-START NAME` and records each stage before it runs, including independent static/interval/row uploads before their concurrent launch. The backfill fetches and parses these start markers; old end-only markers retain their reconstruction contract.

The API and shared model extend only explicitly running phases of a running job to the current request timestamp. No absent stage is guessed from a pipeline order. The detail chart shows their true overlap and `running`/elapsed labels, and compact charts/tips retain active-stage indicators. Polling remains 30 seconds for active detail pages. Intervals with no recorded stage remain visibly unknown, rather than being described as idle or assigned to a guessed stage. No schema migration is needed.

### Failed phases and subsequent recoveries (2026-10-13)

A succeeded Batch job is not necessarily a fully successful scan pipeline: optional search/interval append failures do not fail the parent. Live recording and backfill now derive the phase's `failed` status from a failure note, including legacy `wall, failed …` markers. Readers apply the same rule to existing rows recorded as `done`, so historical charts do not require destructive rewriting. Exit 124 is labeled `timed out`. Failed phases are red in the duration strip, row bars and detail timeline, with an explicit outcome alongside the duration. That duration is the original attempt's actual elapsed wait until failure, not a successful build duration.

The displayed run outcome is `partial` for a succeeded Batch job with unresolved failed phases, and `recovered` once every failed phase has a linked successful recovery. The recorded Batch status remains unchanged. `/health` uses the same display rule, and `/scans` can filter by partial/recovered as well as ordinary outcomes.

Recovery attempts use the existing schema: a separate `scan_runs` row of kind `phase-recovery`, the original run's `parent`, identical scan/store, and its own named `scan_run_phases` row. Neither the original phase nor the original job's duration is overwritten. Recoveries fold under their parent in the run list and appear separately on the detail page with their own UTC times, durations, notes and details/Batch/log links. They are not stacked onto the original duration or silently described as a complete fresh rebuild. A later successful same-phase attempt resolves an issue; an unrelated scan/store/parent, an older attempt or an unsuccessful retry does not.

`dt-cloud scan-run recovery -c job/scan-runs.json -d DB_ID -p ORIGINAL_RUN_ID -P PHASE -J RECOVERY_JOB_NAME -R REGION [-q SCOPE_NOTE] [-n]` imports a completed Batch attempt without launching any work. It verifies the parent has that failed phase, imports actual Batch UID/status/start/end/machine/image, and rejects missing/nonterminal/invalid or conflicting attempts. `-n` validates against the configured sink before printing SQL; `-s` selects an existing local SQLite database. Repeating the same import is idempotent. Include a scope note when completed append/catalog/drill work was reused. This is an operator CLI, not an unauthenticated UI write endpoint. Future detached recovery workflows must explicitly record/link their attempt; merely completing a job does not create the relationship automatically.

The successful recovery controller for the `2026-10-12T2006` scan is linked to its original `static-names` timeout: 00:47:47–01:28:14 UTC (40m27s), waiting for the fixed anchors and then completing publication/R2 verification, while reusing prior stages. The original two-hour timeout stays visible. The reader/CLI changes require no D1 migration; the recorder change is now in the approved daily production image.

The `2026-10-13T0702` controller is also linked: 10:12:17–10:34:43 UTC (22m27s including its wait for the long-only drill recovery). Its scope note explicitly records reused short drill, anchors, catalog, shards and append, plus gated publication/R2 verification. The dev page is browser-verified as `recovered`, with the original failed search interval intact and this attempt separately linked. After recovery, the user separately approved image promotion: `e439a01c…` is now selected by the daily production tag. Pages promotion remains separate and has not occurred.

### D1, not a `run.json` in the bucket

The record lives in D1 beside `deletion_runs`, not as a per-scan manifest object:

- **Listing is the main read.** `/scans` lists every run with its totals and deltas; from D1 that is three indexed `SELECT`s, from the bucket it is a LIST plus one GET per run (hundreds, growing) or a maintained index file with read-modify-write races between the job and the backfill.
- **It matches deletion runs**: the same store, the same `requireViewer` read path, the same "the job writes its own record" model (`sweep_exec.record_run`).
- **Additive only**: three new tables, no existing table altered or rebuilt, no FK into an existing table (memory: D1 enforces FKs; gcs 0028). The job already holds D1 write credentials for `index-sync`.
- No `run.json` mirror: it is not free (a second writer path, a second shape to keep in step), and nothing reads it.

### The schema (`site/migrations/cw/0016_scan_runs.sql`; gcs: the same file as `0040_scan_runs.sql`)

- `scan_runs`: one row per **job run** (not per scan: a scan can have a failed attempt, the run that published it, REPROCs, and its listing fan-out jobs). Key: the Batch job uid (`$BATCH_JOB_UID`, also the task logs' `labels.job_uid`). Columns: `store`, `scan`, `kind` (`scan | reproc | listing | …`, the profile's), `parent` (a downstream job's orchestrating run; no FK, either may land first), `status` (`running | succeeded | failed | nop`), `failed_phase`, `error`, `started_ts` / `finished_ts` (the task's RUNNING → terminal events), `job_name`, `region`, `machine`, `spot`, `tasks`, `image`, `cost_usd`, `gen` (the index generation it wrote), `source` (`job | backfill`; the first writer's), `updated_ts`.
- `scan_run_phases (run_id, phase)`: `seq`, `started_ts`, `finished_ts`, `status`, `note`. Explicit phase starts keep their own clock and a NULL end while running. Legacy end-only records reconstruct a start where the previous phase ended; an *overlapped* phase (gcs's `access-ingest`, which runs beside the listing) starts at the run's start.
- `scan_run_outputs (run_id, key)`: `uri`, `bytes`, `objects`, `rows`, `gen`. Keys are structures: `listing` and `listing/<bucket>` (rows = the bucket's `_SUCCESS.json` objects), `dir-cache`, `path-index` and `path-index/<tier>` (rows = the tier's last `row_end` in its `.groups.parquet`), `snapshot`, `static` and `static/deltas/<scan>` (the static name index's manifest: its appended runs' rows and bytes). The interval store gets an entry when its runs land (`specs/interval-store.md`).
- **Deltas are not stored**: the reader diffs each output against the previous scan's (the newest earlier scan with that structure; a key's scan id is normalized, so `static/deltas/<scan>` compares run to run). Stored deltas would go stale on every REPROC or late backfill.
- Writers upsert the parent row first and never `INSERT OR REPLACE` it (a REPLACE deletes the row, which the children's FKs refuse); a NULL in a later partial write keeps the stored value.

### `dt-cloud scan-run` (generic; deployment config in a profile)

A deployment's facts live in its profile JSON (`-c` / `$SCAN_RUNS_PROFILE`, checked in on the deployment branch as `job/scan-runs.json`): its GCP project and Batch regions; its **job kinds** (regex matches on job name, image, entrypoint, commands, env present / absent; where each finds its scan id: an env var, a commands regex, a log marker, the task's start or the job's creation formatted as a scan id; `downstream` for fan-out jobs); its phase / NOP / error markers; its **outputs** (URI templates over `{scan}` / `{gen}`, the phase whose end measures each, the kinds that write it, `split: dir | stem`, how rows are read); machine **prices** (list $/h) and a SPOT factor. `cloud` carries no deployment's profile.

- `record SCAN` — the job's incremental write, idempotent, and never fatal (an error prints and exits 0): `-S` start, `-A -P NAME` phase started (no output measurement), `-P NAME` a phase just ended (measures the outputs tied to it), `-x RC` finish (0 = succeeded, else failed with `-F` phase and `-e` reason), `-N` a NOP, `-g GEN`, `-J JOB -R REGION` fill the Batch facts and cost, `-o KEY=URI` ad-hoc outputs. A job outside GCP Batch states its own facts instead (`-j` name, `-R` region, `-m` machine, `--spot/--no-spot`, `-T` tasks, `-i` image; a `-m` the profile prices gets the run a cost at `-x`, over its time less the profile's `unbilled` phases), its step times (`-t EPOCH`: the start with `-S`, a phase's end with `-P`, the finish with `-x`) and its store (`-K`, over the profile's); the run id defaults to the first set of the profile's `run_id_env` vars (`["BATCH_JOB_UID"]`). Sinks: D1 over the HTTP API (`$D1_DB_ID` + the `index-sync` credentials), `-s FILE` a local SQLite file (tables from the migration file), `-n` the SQL on stdout (`wrangler d1 execute --file` takes it).
- `backfill` — reconstruct past runs (§Backfill), same sinks; `-w DIR` saves the fetched jobs and log lines, `-j` / `-l` replay them; `-M` skips the store reads; `-S` from a scan id on.

Scan ids go through `dt_cloud.scan_id` (no private regexes, no "daily"): a job's scan source must yield `is_scan_id`.

### The UI

- `/scans`: one compact row per orchestrating run (downstream jobs fold into their parent), newest first: linked start time, a separate job-type badge and downstream count, status (a failed one's tip says where and why), duration, a bar split by phase, recorded output-file size and change, compute estimate, map / Batch / log links. Scans, access-log ingests (AL), and maintenance have distinct badge colors and row markers. Type checkboxes support arbitrary combinations, each has an “only” action, and All resets them. `use-kbd` registers Only / Include / Exclude actions for every discovered type, plus All. The shareable `?k=` preserves existing single-kind links, accepts comma-separated includes / `!kind` excludes, and `!` for none; status / time-window filters remain independent. Above the table, a DIY-SVG strip of duration per run over time is stacked by phase, failures in the danger ink; type filters affect both chart and table. Under 900 px the phase bar and links fold away; the table scrolls horizontally within its container.
- **Output files / output Δ:** sum of the run's recorded top-level file sizes (never double-counting child rows). Each structure's delta compares its size with the newest earlier scan recording that structure. Units always include B. This measures recorded scan artifacts, not scanned bucket capacity, transfer volume, or every retained shared historical index. The size links to the run's per-structure breakdown; missing recordings remain “—”.
- `/scans/<run id>`: the phase timeline (a Gantt on the run's clock, overlapped phases on their own lane), the outputs table (bytes / objects / rows, Δ vs previous scan, links to the `/meta` tree), the downstream jobs, the scan's other runs, and the Batch / Cloud Logging links.
- `/meta` is the `meta` secondary store: the scan of the deployments' own data bucket (the treemap at `/meta/<bucket>/<key>`, staff-only, mounted on cw-s3.oa.dev). It is not on gcs.oa.dev, so its base is config: `META_TREE_URL` (e.g. `https://cw-s3.oa.dev/meta`); unset = no meta links. The meta store is itself scanned periodically, so a just-written output appears there after its next meta scan.
- Gate: `requireViewer`, as `/staged`'s run reads.

## Backfill

`dt-cloud scan-run backfill -c job/scan-runs.json` reads, read-only:

- **Batch API** (every job the profile's kinds match, paginated): run id, kind, scan id, status, start / end, machine, SPOT, tasks, image, cost estimate, the failure reason (`Batch: task failed (exit 137)`, `over the maximum runtime`, `cancelled`).
- **Cloud Logging** (30 days): phase markers (each phase's end is its log line's time), the done / NOP markers (scan id, NOP status), the ERR trap's `+ fail_alert <rc> <line> <cmd>` (failed command). A server-side prefilter on each marker's literal head; the full regexes run locally.
- **The data bucket**: each scan's outputs. An index generation (`{gen}`) goes to the run whose window holds its stamp (a REPROC's generation is the REPROC's); every other output to the scan's latest succeeded run among the kinds that write it.

gcs, measured 2026-10-09 (read-only, into a local SQLite file; ~35 s): **242 runs over 73 scans** (2026-07-30 → 2026-10-09T1236) from 517 Batch jobs: 76 scan runs (65 succeeded, 10 failed, 1 running), 48 REPROCs (46 succeeded, 2 cancelled), 118 listing fan-out jobs linked to their parents. cw-s3 (dry, the same way): 145 runs over 145 scans (130 succeeded, 15 failed).

| field | recovered for |
|---|---|
| status, start / end, machine, tasks, cost, Batch failure reason | every run (Batch keeps its jobs) |
| scan id | every run (env, the listing command, else the creation date) |
| phases | runs since 2026-09-10 (Logging's 30-day retention): 36 runs |
| failed command | none in the window (the ERR trap's line is xtrace; recent failures were OOM / max-runtime kills, which never reach the trap) |
| listing (per bucket, rows), dir-cache, snapshot | every scan whose prefixes remain (listing + dir-cache: 62 runs, snapshot: 69) |
| path-index tiers (+ rows) | 36 runs, scans since 2026-09-07 (earlier generations lived outside `listing/<scan>/index/`) |
| static name index runs | 2026-10-09 (the first appended run) |
| drill / interval store | not per scan yet: no output entry |

## Job glue

Written as untracked specs in the deployment worktrees, for their sessions to apply (nothing committed to `gcs` / `cw-s3`):

- `wt/gcs/specs/scan-runs-glue.md`: `0040_scan_runs.sql` for the gcs lineage, `job/scan-runs.json`, and a `job/run.sh` patch.
- `wt/cw-s3/specs/scan-runs-glue.md`: `job/scan-runs.json` and a `job/cw-run.sh` patch (which also adds phase markers; cw-run.sh had none).

The shape of both patches:

```bash
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-job/scan-runs.json}
scan_run() { { set +x; } 2>/dev/null; [ -n "${BATCH_JOB_UID:-}" ] && (cd /app && dt-cloud scan-run record -k "$SR_KIND" -g "$GEN" "$@" "$SNAP_ID"); set -x; }
phase() { echo "PHASE $1: ${SECONDS}s (wall${2:+, $2})" >&2; scan_run -P "$1" ${2:+-q "$2"}; }
scan_run_exit() { local rc=$?; scan_run -x "$rc" ${SR_ERR:+-e "$SR_ERR"}; exit "$rc"; }
trap scan_run_exit EXIT     # after the branches that exit as other jobs (sweep, access-only, benchmarks)
scan_run -S -B              # -B: the Batch facts (machine, SPOT, cost) from the job whose uid is $BATCH_JOB_UID
# … `phase <name>` replaces each `echo "PHASE <name>: …"`; `fail_alert` sets SR_ERR; the NOP branch calls `scan_run -N`
```

The marker lines are unchanged, so the backfill parses old and new logs alike. A stubbed-CLI run of the helpers checked the call sequence and that the exit code survives the trap.

## Rollout (needs Ryan's go)

1. Merge `scan-runs` into `cloud`; the deployments merge `cloud`.
2. Apply the migration: gcs `wrangler d1 migrations apply oa-gcs-usage-auth --remote` (prod D1, which dev.gcs.oa.dev shares); cw `… oa-cw-s3-usage-db --remote`.
3. Backfill each: `dt-cloud scan-run backfill -c job/scan-runs.json -o`.
4. Apply the job glue, rebuild the images.
5. `META_TREE_URL` in each `site/wrangler.toml`, deploy.
