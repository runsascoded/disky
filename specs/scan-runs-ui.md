# Scan runs in the site UI

Show each scan job's run the way `/staged` shows deletion runs: when it ran, how long each phase took, what it wrote (with links to the `/meta` tree and the map at that scan), what changed in each structure against the previous scan, and the job's Batch and log links.

## Status

- Built on `scan-runs` (off `cloud`): migration, `dt-cloud scan-run record | backfill`, `/api/scan-runs`, `/scans` and `/scans/<run>`, tests.
- **Gated on Ryan's go** (§Rollout): applying the migration to a deployment's D1 (gcs's prod D1 `oa-gcs-usage-auth` is also dev.gcs.oa.dev's, so a dev deploy needs it too) and the backfill's writes. Until then the backfill has run read-only into local SQLite, and the UI was checked on a local stack (`gcs` + `scan-runs`, `./dev --local-db`, the local D1 seeded from the backfill's `-n` SQL).
- The job glue (§Job glue) is for the deployment sessions to apply on `gcs` / `cw-s3`.

## Decisions

### D1, not a `run.json` in the bucket

The record lives in D1 beside `deletion_runs`, not as a per-scan manifest object:

- **Listing is the main read.** `/scans` lists every run with its totals and deltas; from D1 that is three indexed `SELECT`s, from the bucket it is a LIST plus one GET per run (hundreds, growing) or a maintained index file with read-modify-write races between the job and the backfill.
- **It matches deletion runs**: the same store, the same `requireViewer` read path, the same "the job writes its own record" model (`sweep_exec.record_run`).
- **Additive only**: three new tables, no existing table altered or rebuilt, no FK into an existing table (memory: D1 enforces FKs; gcs 0028). The job already holds D1 write credentials for `index-sync`.
- No `run.json` mirror: it is not free (a second writer path, a second shape to keep in step), and nothing reads it.

### The schema (`site/migrations/cw/0016_scan_runs.sql`; gcs: the same file as `0040_scan_runs.sql`)

- `scan_runs`: one row per **job run** (not per scan: a scan can have a failed attempt, the run that published it, REPROCs, and its listing fan-out jobs). Key: the Batch job uid (`$BATCH_JOB_UID`, also the task logs' `labels.job_uid`). Columns: `store`, `scan`, `kind` (`scan | reproc | listing | …`, the profile's), `parent` (a downstream job's orchestrating run; no FK, either may land first), `status` (`running | succeeded | failed | nop`), `failed_phase`, `error`, `started_ts` / `finished_ts` (the task's RUNNING → terminal events), `job_name`, `region`, `machine`, `spot`, `tasks`, `image`, `cost_usd`, `gen` (the index generation it wrote), `source` (`job | backfill`; the first writer's), `updated_ts`.
- `scan_run_phases (run_id, phase)`: `seq`, `started_ts`, `finished_ts`, `status`, `note`. A phase starts where the previous one ended; an *overlapped* phase (gcs's `access-ingest`, which runs beside the listing) starts at the run's start.
- `scan_run_outputs (run_id, key)`: `uri`, `bytes`, `objects`, `rows`, `gen`. Keys are structures: `listing` and `listing/<bucket>` (rows = the bucket's `_SUCCESS.json` objects), `dir-cache`, `path-index` and `path-index/<tier>` (rows = the tier's last `row_end` in its `.groups.parquet`), `snapshot`, `static` and `static/deltas/<scan>` (the static name index's manifest: its appended runs' rows and bytes). The interval store gets an entry when its runs land (`specs/interval-store.md`).
- **Deltas are not stored**: the reader diffs each output against the previous scan's (the newest earlier scan with that structure; a key's scan id is normalized, so `static/deltas/<scan>` compares run to run). Stored deltas would go stale on every REPROC or late backfill.
- Writers upsert the parent row first and never `INSERT OR REPLACE` it (a REPLACE deletes the row, which the children's FKs refuse); a NULL in a later partial write keeps the stored value.

### `dt-cloud scan-run` (generic; deployment config in a profile)

A deployment's facts live in its profile JSON (`-c` / `$SCAN_RUNS_PROFILE`, checked in on the deployment branch as `job/scan-runs.json`): its GCP project and Batch regions; its **job kinds** (regex matches on job name, image, entrypoint, commands, env present / absent; where each finds its scan id: an env var, a commands regex, a log marker, the task's start or the job's creation formatted as a scan id; `downstream` for fan-out jobs); its phase / NOP / error markers; its **outputs** (URI templates over `{scan}` / `{gen}`, the phase whose end measures each, the kinds that write it, `split: dir | stem`, how rows are read); machine **prices** (list $/h) and a SPOT factor. `cloud` carries no deployment's profile.

- `record SCAN` — the job's incremental write, idempotent, and never fatal (an error prints and exits 0): `-S` start, `-P NAME` a phase just ended (measures the outputs tied to it), `-x RC` finish (0 = succeeded, else failed with `-F` phase and `-e` reason), `-N` a NOP, `-g GEN`, `-J JOB -R REGION` fill the Batch facts and cost, `-o KEY=URI` ad-hoc outputs. Sinks: D1 over the HTTP API (`$D1_DB_ID` + the `index-sync` credentials), `-s FILE` a local SQLite file (tables from the migration file), `-n` the SQL on stdout (`wrangler d1 execute --file` takes it).
- `backfill` — reconstruct past runs (§Backfill), same sinks; `-w DIR` saves the fetched jobs and log lines, `-j` / `-l` replay them; `-M` skips the store reads; `-S` from a scan id on.

Scan ids go through `dt_cloud.scan_id` (no private regexes, no "daily"): a job's scan source must yield `is_scan_id`.

### The UI

- `/scans`: one row per orchestrating run (downstream jobs fold into their parent), newest first: scan (→ the run; its kind and downstream count under it), status (a failed one's tip says where and why), started, duration, a bar split by phase, output bytes and Δ vs the previous scan, cost, links (details, map at `?d=<slug>`, Batch, logs). Above it, a DIY-SVG strip of duration per run over time, stacked by phase, failures in the danger ink; the charts draw at their measured width. Under 900 px the phase bar and links fold away, under 640 px the start time.
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
