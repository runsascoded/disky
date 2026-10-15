# Shared scan-job wrapper: `dt-cloud scan-run phase-*` + `disk-tree notify`

Status: **migration step 1 built on cloud** (2026-10-14). Reviewed by gcs (9 gaps) and cw-s3 (5 gaps); both are folded in below. m3's `disk-tree notify` is on cloud (`65f0a64e`) and is the one notifier; it now has the `slack` channel. `dt-cloud scan-run start / phase-begin / phase-end / phase -- CMD / finish` are in `cloud/src/dt_cloud/scan_runs.py` (over `record`'s internals; `record` is unchanged), tested in `cloud/tests/test_scan_run_wrapper.py`. Step 2 (each deployment swapping its shim): cw switched (8d343511), pending its first verified job run; gcs built (b55f942a), pending its image and first verified run, with its old shell functions deleted. Since: `scan-run nop`, a list-valued `error_marker`, and the `scan-run supervise` entrypoint plus TERM/INT traps (below; m3's platform-terminated job recorded `succeeded`).

As built, beyond the design below:
- **Run state file.** `start [SCAN]` (default `$SCAN_ID`) keeps the run's scan, kind, parent, gen, store and job facts in `${TMPDIR:-/tmp}/dt-scan-run/<run id>.json`, beside the `.alerted` marker. The phases (background subshells included) and `finish` read it, so `phase-begin NAME` needs no scan or kind flags. A fatal `phase-end` writes its phase there, and `finish` records it as `failed_phase`. `start` clears a stale state file and marker; `finish` removes both.
- **Recording switch.** `record` has no scratch flag (gcs's SCRATCH/GATE check lives in its shell `scan_run`). So the wrapper records nothing when there's no run id, or when `DT_RECORD=off`. The PHASE lines and alerts are unaffected. gcs SCRATCH/GATE would set `DT_RECORD=off DT_ALERTS=stderr`.
- **What marks the run alerted.** Only a fatal `phase-end` (a `fail` alert) creates the marker. A `-W` warning doesn't, so a later fatal failure in the same run still alerts. The engine's `disk-tree notify` CLI has no notion of a run and doesn't touch the marker.
- **Alert text.** The title is `❌|⚠️ <alerts.label> <scan>`. A phase's message is `<name> failed (exit N[, timed out after T])[: MSG][\nResume: `R`]`. `finish`'s is `run failed (exit N)[ in <phase>][: ERR]`. The `logs_url` link is labelled "task logs", and is omitted when the run lacks a fact the template names. The facts are `job_uid`, `project`, `region`, `job`, `scan` and `kind`. With no channel configured, the alert prints as `alert (no channel configured): …`.
- **Failed phases.** A failed phase's outputs aren't measured (what it left is partial). The PHASE line and the note are `failed (exit N)[, NOTE]`, as gcs writes them today.
- **`phase` timeouts.** `-T` takes `N[s|m|h|d]`, and `-G` sets the TERM→KILL grace (default 30s). A missing CMD exits 127. If `$DT_JOB_START` is unset, CMD doesn't run (exit 2).
- **`phase` forwards signals.** A SIGTERM / SIGINT that `phase` itself receives goes on to CMD at once. Under `-T`, CMD has its own session that nothing else can reach, so the signal goes to its process group, and the group is KILLed after `min(-G, $DT_TERM_GRACE)`. Without `-T`, the signal goes to CMD only, and whoever signalled the caller's group owns the escalation. The phase ends failed with CMD's status (a signal death as 128+N), and its alert says `(exit 143, terminated by SIGTERM)`.
- **Redaction** uses each secret's value with trailing whitespace stripped, so an injected newline doesn't swallow the text's own.

Inputs:
- the drift audit (`tmp/drift-audit-2026-10-14.md` #3–#4);
- each deployment's must-keeps (`tmp/job-wrapper-reqs.md`);
- gcs `job/run.sh`, cw `job/cw-run.sh` / `cw-meta-run.sh`, m3 `infra/aws/ingest.sh`.

## Problem

Each deployment's job script records its phases and alerts on failure its own way. gcs has `begin_phase`/`phase` shell functions, ERR + EXIT traps and `chat.postMessage`. cw has `phase` only, so phase starts go unrecorded, plus `slack_alert`. m3 has `scan-run record -K/-t` calls and, until `disk-tree notify`, no failure alert. That makes three copies of the phase shim and four alert paths, so fixes keep landing in one copy and drifting from the others.

## Design

The recording, timing, redaction and alerting move into `dt-cloud`. What stays in each shell script is the step list, two one-line traps, and per-step messages.

### Bracket primitives (the core)

Most phases aren't a single executable. They're shell functions (`static_names_step`, `r2pub`), compound blocks (`r2pub … && r2pub …`, the per-bucket list/import loop, `publish`'s mkdir/cp/heredoc), env-prefixed commands, or backgrounded `( … ) &` subshells. So the core is a begin/end pair, as gcs's `begin_phase`/`phase` already is.

- **`dt-cloud scan-run phase-begin NAME [-b EPOCH] [-g GEN]`** records the phase as running (today's `record -A -P NAME`). It prints `PHASE-START <name>: …` to stderr.
- **`dt-cloud scan-run phase-end NAME [-x RC] [-W] [-m MSG] [-R RESUME] [-q NOTE] [-o KEY=URI]… [-M]`** records the phase's end and measures its outputs, as `record -P` does today. It prints `PHASE <name>: <N>s (wall[, note])` to stderr. With a non-zero `-x`:
  - **fatal (default):** records the phase as failed and posts a `fail` alert. It marks the run as alerted (see below) and exits with RC, so the script's `set -e` or its own `exit` ends the run.
  - **`-W` (non-fatal):** records the phase with a note, posts a `warn` alert, and exits 0, so the script continues.
  - **`-m MSG`:** the step's own text, saying what the failure means for users ("the scan publishes; search says not available", "D1 pointers not advanced; the previous scan stays visible", "data fine, serving degraded"). It works with or without `-W`. A fatal phase with `-m` (gcs publish-r2) posts that text, styled by severity, and `finish` posts no second generic alert.
  - **`-R RESUME`:** an explicit, hand-written resume command, quoted in the alert. **Opt-in only.** It never defaults to the command's argv, which can carry env-prefixed secrets or container-only paths.

**PHASE marker lines are a contract.** The scan-runs backfill parses them from Cloud Logging, so they must stay byte-identical to today's. `<N>s` is measured from `$DT_JOB_START`, which the script exports once at the top (`export DT_JOB_START=$(date +%s)`); this replaces the shell's `$SECONDS`. If it's unset, the commands refuse to run (exit 2) rather than print a different clock.

### `dt-cloud scan-run phase NAME [phase-end opts] [-T SECONDS] -- CMD…` (sugar)

For the steps that are one executable: `phase-begin`, then run CMD, then `phase-end -x $rc` with the same options.
- **`-T`:** runs CMD in its own session (`setsid`). On timeout it sends TERM to the whole process group, waits a grace period, sends KILL, and reports exit 124, as coreutils `timeout` does. bulk-list's multiprocessing spawn workers and Batch submitters otherwise survive as orphans (memory `pkill-spawn-orphans`).

### Run start and finish

- **`dt-cloud scan-run start [-k scan|reproc|compute|access] [-p PARENT_UID] [job facts]`:** today's `record -S`, plus kind and parent. gcs's ACCESS_ONLY path starts with `-k access`. The facts come from `-B`/`-J` on GCP and from flags on AWS.
- **`dt-cloud scan-run nop`:** marks the run a no-op (its scan was already published; `record -N`). The EXIT trap's `finish -x 0` keeps it a NOP; a non-zero `finish` still fails it.
- **`dt-cloud scan-run finish -x RC [-e ERR]`:** today's `record -x`. On a non-zero RC it posts the `fail` alert, unless the run is already marked as alerted. `ERR` is recorded as `scan_runs.err` (shown on /scans) and included in the alert.

The traps become:

```bash
scan_err() { SR_ERR="exit $1: $3 (${BASH_SOURCE[1]##*/}:$2)"; }       # record only; no alert
trap 'scan_err $? $LINENO "$BASH_COMMAND"' ERR
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'dt-cloud scan-run finish -x $? ${SR_ERR:+-e "$SR_ERR"}' EXIT
```

The TERM / INT traps make `$?` in the EXIT trap the termination's status when bash does get to run its traps. Without them, `$?` is whatever the last command left, which is 0 often enough that a terminated run is recorded `succeeded`.

Use the function, not an inline `SR_ERR=…` assignment. Under `set -x` the call traces as `++ scan_err RC LINE CMD`, which the scan-runs backfill can parse (`error_marker` `^\+\+ scan_err (?P<rc>\d+) (?P<line>\d+) (?P<cmd>.*)`). An inline assignment traces as `++ SR_ERR='exit …'`, which it can't. `error_marker` may be a list, so a deployment whose trap changed shape keeps its old marker (gcs: `fail_alert`) for older logs. macOS `/bin/bash` 3.2 reports `$LINENO` one lower than bash 5 under `bash -c`, so tests run the PATH's `bash`.

The ERR trap stays because gcs runs many unphased commands (cp, mkdir, heredoc python, the lifecycle pull), and "which one failed" has to survive. Its text passes through redaction (below) before it's recorded or posted.

### The entrypoint: `dt-cloud scan-run supervise [-G GRACE] -- CMD…`

Traps alone can't record a platform termination. On m3 (AWS Batch on Fargate, the script as PID 1), a terminated job was recorded `succeeded` with rc 0 while the job ended FAILED with exit 143:
1. The platform sends SIGTERM to PID 1 only, not to its children. Bash defers a trapped signal until its foreground child exits, so a long phase keeps running.
2. With no TERM trap, the EXIT trap records whatever `$?` happens to be.
3. After the stop timeout (30 s on Batch / ECS) everything is SIGKILLed together, and no trap runs at all.

So each deployment puts `supervise` in front of its script:

```dockerfile
ENTRYPOINT ["dt-cloud", "scan-run", "supervise", "--", "bash", "job.sh"]
```

- It runs CMD in its own session and process group, and exits with CMD's status (a signal death as 128+N).
- On SIGTERM or SIGINT, it forwards the signal to CMD's whole group at once. Python handles the signal immediately, so nothing is deferred. After `GRACE` (default 20 s, under the common 30 s platform stop timeout) it SIGKILLs whatever is left of the group.
- It exports `$DT_SUPERVISE`, a path where the script's `start` writes which run it started and how it's recorded (run id, profile, sink, state dir, `DT_ALERTS`). So `supervise` needs no flags of its own, and sees the run even when the script sets its run id or profile itself. One run per supervised job: a second `start` repoints it. Alert channels and `secrets` must be in the container env, where `supervise` can see them.
- Under `supervise`, `finish` leaves the state file and the alerted marker in place, and adds `finished: RC` to the state. `supervise` clears both after CMD exits.
- It exports `$DT_TERM_GRACE` (half its grace), so a `phase -T` group, which is in its own session and out of `supervise`'s reach, is KILLed before `supervise` gives up.

After CMD exits, `supervise` decides from CMD's exit status:

| Signal received | CMD's exit | `finish` ran | `supervise` does |
|---|---|---|---|
| TERM / INT | non-zero | either | records the run failed: exit 143 (130 for INT), error `terminated (SIGTERM)` (plus `; killed after the Ns grace` if it had to KILL) |
| TERM / INT | 0 | either | nothing: the job finished first, and its record (`succeeded`, or `nop`) stands |
| none | non-zero | no | records the run failed: CMD's status, error `killed (SIGKILL)` (e.g. an OOM kill) or `exit N, without finish` |
| none | 0, or non-zero after `finish` | — | nothing |

The failed record goes through the same code as `finish` (`JobRun.finish`, then `record(exit_code=…)`). `record` moves a stored `succeeded` or `nop` to `failed` on any non-zero exit code, and its `error` replaces the trap's, so a termination wins over whatever the EXIT trap recorded, including the observed `succeeded`. Across the job it alerts at most once. `finish` now creates the `.alerted` marker before it posts, as a fatal `phase-end` does, so if the phase, the trap or an earlier step already alerted, `supervise` only re-records.

**The NOP race.** The signal can arrive while a NOP run is ending. The rule is the job's exit status, the same one `finish` uses (a non-zero `finish` fails a NOP). A job that recorded `nop` and exited 0 stays `nop`, even if the signal arrived first. A job that was still running when the signal took it down is recorded failed, including one inside its EXIT trap with its `finish -x 0` not yet written. In that case the NOP run did no work, but it also didn't complete, and a false failure alert is a safer error than a silently swallowed termination.

### "Already alerted" is shared state

Phases run in background subshells (cw's static stage; gcs's static-names ∥ interval-store ∥ index-sync group), and `finish` runs in the EXIT trap, so the flag can't live in one process. It's a marker file: `${TMPDIR:-/tmp}/dt-scan-run/<run id>.alerted`. A posting `phase-end` or `finish` creates it before posting; `finish` (and `supervise`) skip their generic alert when it exists. A background phase's failure therefore alerts once, and is still alerted when the foreground ends cleanly but the waited-on stage failed.

### Redaction

Nothing that echoes text (an alert, `ERR`, `-R`, a note) may carry a secret:
- Every value of the env vars named in the profile's `secrets` list (cw-run's strip list today: `CLOUDFLARE_API_TOKEN`, `SITE_TOKEN`, `SLACK_BOT_TOKEN`, …) is replaced by `<NAME>` before any text is posted or written to D1.
- Raw argv is never written to a `scan_runs` row.
- Tokens are read from env only, never from argv, so `set -x` scripts can't leak them through a notify call.

### Notifications: `disk-tree notify` plus a Slack channel

There is one notifier: m3's `disk-tree notify` (engine, standard library only), which ingest.sh and the app spec (`scan-failure-notifications.md`) already depend on. Its contract is kept unchanged:
- env `PUSHOVER_TOKEN` + `PUSHOVER_USER`, `DISCORD_NOTIFY_WEBHOOK`;
- `DISK_TREE_NOTIFY` (a comma list, in order) picks the channels; unset means every channel with credentials;
- CLI `-c CHANNELS -f -l LABEL -p PRIORITY -t TITLE -u URL MESSAGE`; `-f` is a fallback chain;
- exit 2 when nothing is configured, 1 when every attempt failed.

This adds a **`slack` channel**: `SLACK_BOT_TOKEN` + `SLACK_ALERT_CHANNEL`, posting `chat.postMessage` over urllib so it stays standard library only. Secrets are stripped of trailing whitespace on read. The webhook fallback is dropped; neither gcs nor cw uses it. Alerts post top-level in the alert channel, labelled `alerts.label`, not threaded under the digest. `dt-cloud`'s `phase-end`/`finish` call the same Python function, with the level mapped to priority and title.

**Two separate switches:**
- `DT_ALERTS=stderr` mutes alerts. They print as "alert (suppressed): …" and nothing posts.
- Recording is controlled by `record`'s existing scratch flag and its "not a Batch job (no `BATCH_JOB_UID`): no record" check.

gcs SCRATCH/GATE sets both; cw REPROC or one-shot runs may record with alerts muted.

### Profile additions (`job/*.json`, per deployment, declarative)

```json
{"alerts": {"label": "cw-s3", "logs_url": "https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22{job_uid}%22?project={project}"},
 "secrets": ["CLOUDFLARE_API_TOKEN", "SITE_TOKEN", "SLACK_BOT_TOKEN"]}
```

- `logs_url` is a template over the run's open-ended job facts (`job_uid`, `project`, `region`, `job`, …). If absent, alerts carry no link.
- There are no deployment defaults in code.

## What stays deployment-specific

- The step list itself: gcs's concurrent group and pointer flip, the SPLIT_COMPUTE preflight, `REPROC=1` skips, cw's per-bucket loops.
- The per-step `-m` texts and `-R` resume commands.
- `${VAR:+set}` xtrace guards.
- The post-publish health probe (gcs), whose result becomes a `phase-end -W -m …`.

## Migration

1. Build on cloud: `phase-begin`/`phase-end`/`phase`/`start`/`finish`, the marker file, redaction, `-T` group kill, and the `slack` channel in `disk-tree notify`. Tests:
   - exact recorded rows;
   - byte-exact PHASE lines;
   - alert payloads through a fake transport;
   - one alert across a background failure plus `finish`;
   - redaction of a seeded secret;
   - `-T` killing a spawned grandchild;
   - `supervise`: a TERM mid-phase (one alert, run failed 143, empty process tree), a TERM-ignoring child KILLed after `-G`, a recorded success overridden, a clean run or NOP untouched; `phase` forwarding a TERM to its `-T` session.
2. Each deployment swaps its shim on its own branch: gcs, cw, then m3, which already calls `disk-tree notify`. On the first migrated run, a `disk-tree notify -c slack info` smoke test confirms the alert channel receives it (cw hasn't checked recently). Old shell functions are deleted after one verified job run each.
3. Delete the forked alert helpers and `static-names.sh` copies that become unused.
