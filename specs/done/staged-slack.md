# Staged-deletion review in Slack

Staged deletions (the table's trash gesture, `/staged`) announce themselves in an admin Slack channel, where admins can review and act without opening www. Ryan, 2026-09-29: cw → `#cw-s3-admin`, gcs → `#gcs-admin`; "try both [notify + act from Slack], I'm curious whether acting from Slack can be made good enough / preferable".

Built on cw-s3 (`c3d592a`), brought onto the base (`cloud`) generic over both executors: cw's plan-first Batch bridge (`plan-sweep`) and gcs's (`sweep`).

**Status (2026-09-28): done on the base** (branch `staged-slack`): cw's generic pieces verbatim, the executor seam, the digest gate on both executors and both dispatch surfaces, both lineages' migrations. Verified by `pnpm -C site build` and the site's vitest suite (the gate over the real cw schema, gcs reflection over the gcs schema, both lineages applied with FKs on). Nothing has called Slack, GCP or production; each deployment turns it on per [Per-deployment setup](#per-deployment-setup).

## Shape

- **One thread per staged plan.** The parent message is re-rendered on every event: item and batch counts, who staged, the latest dry-run's result and whether it still matches the plan (or that it ended without a result), the last real run. Every event is a reply: a stage batch (who, count, memo, the first prefixes), an unstage, a batch rejected, a run dispatched (www or Slack), a run finished (totals; for a real run, the undo deadline) or ended without a result.
- **Buttons.** Parent: *Open in www*, *Dry-run*, *Delete for real…* (the dispatch buttons only where the deployment can dispatch: `GCP_SA_KEY` set). Each batch reply: *Reject batch*, *View in www*.
- **Authority is the site's.** `/slack/actions` verifies Slack's signature (the app's signing secret, ±300 s replay window), maps the clicker to their email (`users.info`, `users:read.email`), and applies the www rules: dispatch = admin (staff domain or `admin_emails`); reject = admin or the batch's stager.
- **Best-effort.** Notifications run after the response (`waitUntil`); a Slack failure never fails a gesture.
- **Inert until configured.** No `SLACK_BOT_TOKEN` + `SLACK_ADMIN_CHANNEL` = no posts; no `SLACK_SIGNING_SECRET` (or no D1) = `/slack/actions` answers 503.

## The executor seam

`functions/_lib/executor.ts` `dispatchPlan(env, { planId, mode, date?, actor, siteUrl, buckets? }, kind?)` is the one dispatch path. Both `/staged` routes are thin wrappers that name their own executor (`api/plan-sweep/dispatch` → `plan-sweep`, `api/sweep/dispatch` → `sweep`); `/slack/actions` uses the deployment's `[vars]` `EXECUTOR` (`plan-sweep` | `sweep`, default `plan-sweep`; any other value throws). It must equal the deployment's `Store.executor` (`site/src/stores.ts`).

The contract (`_lib/dispatch.ts`) is per executor:

- `prepare(env, db, req)` → the canonical prefixes the run acts on (the digest's input) plus a `launch(date, digest)`, or a refusal (no plan, empty plan, cw's one-bucket rule, gcs's bucket cut).
- `refresh(env, db)` → reflect finished runs into D1; returns the runs this call closed (`{ run_id, ok }`), which the caller announces.

| | `plan-sweep` (cw, r2) | `sweep` (gcs) |
|---|---|---|
| implementation | `_lib/planDispatch.ts` `planSweep` | `_lib/sweepDispatch.ts` `sweep` |
| scan ids | `YYYY-MM-DD[THHMM]` | `YYYY-MM-DD` |
| digest's item set | every plan item (one bucket per run) | the items in the run's `-b` cut (all, by default) |
| run row | inserted at dispatch, with `plan_digest` | inserted by the executor inside Batch; the dispatch puts the digest in the job env (`PLAN_DIGEST`) |
| reflection | `_lib/runReflect.ts`: the run's `<mode>-summary.json` → totals + bands | `_lib/sweepReflect.ts`: once the executor recorded the end, `PLAN_DIGEST` → `plan_digest` (matched on `log_dir` = the job's run dir) |
| exit-trap ping | `/api/plan-sweep/jobs`, grant `cw-s3-job-grant` | `/api/sweep/jobs`, grant `gcs-sheet-sync-token` (the job's existing `GCS_USAGE_TOKEN`) |
| result post | `api/plan-sweep/jobs.ts` announces what `reflectRuns` closed | `api/sweep/jobs.ts` announces what `reflectSweepRuns` closed |

Results arrive on their own: each Batch job's exit trap calls its jobs route with the job's read grant, and the reflector runs there (cw: as soon as the summary exists; gcs: as soon as the executor recorded the end), so the result posts to the thread as the run ends. A Batch job that reaches a terminal state without a result (no summary; no recorded end) is closed with the **empty digest** on both executors: it reviewed nothing, so it never opens the gate, and the thread says it ended without a result instead of "would delete 0 B". Only gcs jobs carrying `PLAN_DIGEST` (dispatched through the seam) are reflected; older runs keep `plan_digest` NULL.

## The real-deletion gate

`deletion_runs.plan_digest` = `plans.planDigest(prefixes)`: sha-256 of the run's canonical prefixes, sorted, joined by `\n`, first 16 hex (order-independent). Every run either executor starts records it.

A real dispatch — from Slack **or** `/staged` — goes through `dispatchPlan`, which first refreshes runs, then applies `plans.realGate`: it is refused (409) unless a *finished* dry-run of the plan has the same digest as the item set the real run would act on, and no run of the plan is in flight. The real run uses that dry-run's scan (a `date` naming another scan is refused; Slack omits it). So a batch staged after the dry-run forces a fresh one, and a failed dry-run never counts. On top of that, a Slack *Delete for real…* button carries the digest it was drawn for, and a click on a button drawn for an older item set is refused; the button only appears once the gate is open, and its confirm dialog carries the item count and the dry-run's bytes/objects.

## Pieces

- `functions/_lib/slack.ts` — Web API, `users.info` email, signature verification.
- `functions/_lib/stagedSlack.ts` — rendering (`renderParent`, `stageEvent`, `runEvent`), the notifier (`notifyPlan`, `announceFinished`), `planGate`.
- `functions/_lib/plans.ts` — `planDigest`, `realGate`, `planRuns`.
- `functions/_lib/executor.ts` + `_lib/dispatch.ts` — the seam; `_lib/planDispatch.ts` + `_lib/runReflect.ts` (plan-sweep), `_lib/sweepDispatch.ts` + `_lib/sweepReflect.ts` (sweep).
- `functions/slack/actions.ts` — interactivity.
- The notify calls in `functions/api/plans/[[path]].ts` (stage, unstage/add), both dispatch routes, both jobs routes.
- `functions/_lib/testD1.ts` — test-only in-memory D1 (`node:sqlite`, a lineage applied, FKs on).
- Migrations, both lineages, plain `ALTER TABLE … ADD COLUMN` (FK-safe on D1): `migrations/cw/0005_staged_slack.sql`, `migrations/gcs/0029_staged_slack.sql` — `plans.slack_channel`, `plans.slack_ts`, `deletion_runs.plan_digest`.

## Per-deployment setup

1. Apply the lineage's migration (`wrangler d1 migrations apply <db> --remote`): cw `0005_staged_slack`, gcs `0029_staged_slack`.
2. `[vars]` in the deployment's `wrangler.toml` (the base's documents them commented out):
   - `SLACK_ADMIN_CHANNEL = "<channel id>"` (cw: `#cw-s3-admin`; gcs: `#gcs-admin`).
   - `EXECUTOR`: cw may leave the default (`plan-sweep`) or set it; **gcs must set `EXECUTOR = "sweep"`**, or Slack's Dry-run would go to cw's bridge.
3. Pages secrets (`wrangler pages secret put <NAME> --project-name <project>`): `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` — the deployment's own Slack app.
4. The Slack app's manifest (kept per deployment, not in the base):
   - `oauth_config.scopes.bot`: `chat:write`, `users:read`, `users:read.email` (plus whatever the app already has).
   - `settings.interactivity`: `is_enabled: true`, `request_url: https://<deployment host>/slack/actions`.
   - Invite the bot to the admin channel.
5. Executor-side: the plan-sweep job reads `cw-s3-job-grant` from Secret Manager (a site read grant for the exit-trap ping; must exist wherever `plan-sweep` dispatches). The gcs job reuses its `GCS_USAGE_TOKEN`; the ping needs `curl` in the `gcs-usage-snapshot` image (a missing `curl` is harmless: `|| true`, and `/staged` polling or any Slack click reflects instead).

## Open

- Does acting from Slack beat www? Watch: whether admins use the buttons, whether the dry-run numbers in the thread are enough context to confirm a real deletion.
- A Batch job that dies without its exit trap firing is reflected (and announced) only when something next reads its jobs route — `/staged` or any Slack click (a real click refreshes before gating).
- Reflection reads the recent Batch job listings (cw: the newest 20 sweep jobs; gcs: 100 per region); a run whose job has aged out of them before anything read the route is never reflected by the site.
- gcs's executor could record `plan_digest` itself (from a `plan_digest` field in plan.json) instead of the site copying it from the job env; not needed while the job env carries it.
- gcs's in-flight check sees a run only once the executor has inserted its row (after the manifest step, up to an hour of listing), so a real dispatch while a gcs dry-run is still listing is not refused as "in flight" — it is refused only if no *finished* matching dry-run exists.
