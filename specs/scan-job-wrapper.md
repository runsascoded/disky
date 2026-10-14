# Shared scan-job wrapper: `dt-cloud scan-run phase` + `dt-cloud notify`

Status: **draft for review** (2026-10-14). Sources: the drift audit (`tmp/drift-audit-2026-10-14.md` #3–#4) and each deployment's must-keeps (`tmp/job-wrapper-reqs.md`, from gcs and cw-s3; m3 from its ingest.sh). Nothing is built yet. Circulate to gcs, cw-s3 and m3 before building.

## Problem

Each deployment's job script records its phases and alerts on failure its own way:
- gcs `job/run.sh`: `begin_phase`/`phase` shell functions, ERR + EXIT traps, Slack through `chat.postMessage`;
- cw `job/cw-run.sh` / `cw-meta-run.sh`: `phase` only (phase starts go unrecorded), an EXIT trap, `slack_alert` through a webhook fallback;
- m3 `infra/aws/ingest.sh`: `scan-run record -K/-t` calls and no failure alert at all.

That makes three copies of the phase shim and four Slack paths. Every fix (timeouts, secret stripping, scratch suppression) lands in one copy and drifts from the others.

## Design

All the logic moves to two `dt-cloud` subcommands. What stays in each deployment's shell script is the list of steps and two traps.

### `dt-cloud scan-run phase NAME [opts] -- CMD…`

This command runs CMD as one phase of the current run (the run id comes from `-r` or the profile's `run_id_env`, as `record` does today):
1. Records the phase start (today's `record -A -P NAME`), so phase starts are recorded everywhere, cw included.
2. Runs CMD, with stdout and stderr passed through, under `-T SECONDS` if given (a timeout reports as exit 124).
3. On exit 0, records the phase end and measures its outputs (`-o KEY=URI`, `-M` to skip measuring), as `record -P` does today.
4. On a non-zero exit:
   - **fatal (default):** records the phase as failed and posts a `fail` alert: phase, exit code or timeout, the job's log link (from the profile, e.g. the Batch task-logs URL built from the job UID). It then exits with CMD's code. The EXIT trap (below) finishes the run.
   - **`-W` / `--warn` (non-fatal):** records the phase with a note, posts a `warn` alert including a copy-paste resume command (`-R 'CMD'`, defaulting to CMD itself). It then exits 0, so the script continues. This replaces cw's WARN-and-continue steps and gcs's `timeout … || post ⚠️` blocks.

Other options:
- `-b EPOCH` / `-q NOTE`: overlapped phases (cw's background static stage) keep working: `phase -b "$BEGAN" -q overlapped`.
- `-g GEN`: stamps the generation, as `record -g` does.
- `-K STORE`, plus `-t` and the job-fact flags from `record`: pass through unchanged, for m3/AWS.

### `dt-cloud scan-run start` / `finish`

- `start`: today's `record -S`, plus the job facts (`-B`/`-J` on GCP, flags on AWS).
- `finish -x RC [-e ERR]`: today's `record -x`. On a non-zero code with no phase failure already alerted (the failure came from the script, not a `phase`), it posts the `fail` alert too.

The script's only trap becomes:

```bash
trap 'dt-cloud scan-run finish -x $? ${SR_ERR:+-e "$SR_ERR"}' EXIT
```

The gcs ERR trap's line and command detail come from `phase` instead: a failure outside any phase reports "exit N after phase X".

### `dt-cloud notify LEVEL MESSAGE` (`LEVEL` = `fail` | `warn` | `info`)

This is the one alert path. `phase`/`finish` call it, and scripts can too (cw's "unscanned non-empty bucket").
- Slack through `chat.postMessage` (icon overrides work; webhooks are dropped). The channel is `$SLACK_ALERT_CHANNEL`, else `$SLACK_CHANNEL`. The token is `$SLACK_BOT_TOKEN`. Secrets are stripped of trailing whitespace on every read.
- **Suppression:** `$DT_ALERTS=stderr` (set by gcs SCRATCH/GATE runs) prints "alert (Slack suppressed): …" to stderr and posts nothing. The same setting skips `scan_runs` writes (`record` already honors the scratch flag; unify it on this one variable).
- No token configured: print to stderr, exit 0. An alert never fails a job.
- m3 gets failure alerts from this for free, once its profile names a channel.

### Profile additions (`job/*.json`, per deployment, declarative)

```json
{"alerts": {"label": "cw-s3", "logs_url": "https://console.cloud.google.com/batch/jobsDetail/regions/{region}/jobs/{job}/logs?project={project}"}}
```

- `label`: the prefix on each alert.
- `logs_url`: a template over the job facts. If absent, alerts carry no link.

There are no deployment defaults in code.

## What stays deployment-specific

- The step list itself: gcs's concurrent group (static-names ∥ interval-store ∥ index-sync) and pointer flip, the SPLIT_COMPUTE preflight, `REPROC=1` skips.
- `${VAR:+set}` xtrace guards in the scripts.
- The post-publish health probe (gcs). Its ⚠️ "data fine, serving degraded" becomes a `notify warn` call.

## Migration

1. Build `phase`/`start`/`finish`/`notify` on cloud with tests: exact recorded rows, and the alert payloads captured through a fake Slack client.
2. Each deployment swaps its shim for these in its own branch, one job run verified by its owner (gcs, cw, m3). The old shell functions are then deleted.
3. Delete the forked `static-names.sh` / alert helpers that become unused.

## Open questions

- gcs: does anything read the ERR trap's `$BASH_COMMAND` line beyond the alert text? If yes, `phase` can append CMD's argv to the alert.
- cw: is the webhook fallback still needed anywhere (a channel the bot isn't in)?
- Should `notify` thread warns under the day's digest message rather than post top-level?
