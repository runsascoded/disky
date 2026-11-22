# Digest re-apply: review and apply in `/admin/slack` (thrds bulk rewrite)

From the thrds session (`~/c/thrds`). thrds now has a bulk-rewrite pipeline in Python and TS with one plan format, plus a review UI built to be embedded in an app. This spec puts that UI in front of the existing digest re-apply flow (`specs/digest-runway-marks.md` §4, `site/functions/api/digest-reapply.ts`), so an admin reviews exactly what will change before anything is edited, and applies a selection from the page.

## What thrds provides

- **Python** (`py` ≥ `d15a889`): `thrds.rewrite`: `find_ids(client, tss)`, `plan(found, render)` (render returns `Desired(text, blocks=None, sender=None)` or `Skip(reason)`), `Plan.to_json()`, and `apply(...)`. The CLI is `slck rewrite plan / show / apply / review`. README section "Bulk rewrite (Slack)"; design in `specs/done/bulk-rewrite.md`.
- **TS** (`github:Open-Athena/thrds#<ts-dist SHA>`, currently `0.2.0-dist.38dd767`):
  - `verifyPlan(json)` checks a plan's recorded hashes against its old content.
  - `rewriteHandler(request, {plan, client, receipts, maxBatch})` is the backend for a Pages Function:
    - `GET …/plan` returns `{path, plan}`.
    - `POST …/check` and `POST …/apply` take `{ids}` and return `{outcomes}`. `check` is a dry run.
    - A POST must be JSON and same-origin, and carry at most 10 ids, which keeps it under the subrequest cap.
  - `PlanView` (`@open-athena/thrds/plan-view`, React ≥ 18 as a peer) is the review component. It renders each message before and after, as Slack would, with a diff, filters, selection, a confirm step and per-row outcomes.
  - `postRewrite(endpoint, ids, {onOutcomes})` is the page's side. It sends the selection in batches.
  - `d1Receipts(db, planId)` (from `@open-athena/thrds/d1`, table `rewriteReceiptsSchema()`) records outcomes per plan. Later batches and re-runs skip what's done.
  - See the `ts` README, section "Bulk rewrite".
- **Safety:** a plan written by either side is applied by the other. Apply re-reads each message and refuses it (`stale`) if it changed since planning. `already` means the message already shows the new content. Writes are paced 1.2 s apart.

## Proposed flow

1. **Plan, in the existing Batch job.** A `target` dry run in `digest-reapply` keeps running the digest CLI. With a new flag, `dt-cloud digest … -n --plan`, it also writes a thrds plan:
   - `apply_slack(..., dry_run=True)` already returns `Change(what, ts, body)`.
   - The render is `lambda f: Desired(changes[f.ts].body) if f.ts in changes else Skip("not selected")`.
   - Run it over `find_ids(client, [c.ts for c in changes])`, then upload `Plan.to_json()` to `gs://<data>/digest/reapply/<job>.plan.json`, next to the `.txt` it already writes.
   - A sender-variant reply keeps its sender: leave `sender` unset in `Desired`. That makes it an `edit`, never a `repost`.
2. **Review, in `/admin/slack`.** On a dry-run job with a plan, the job's row opens `PlanView` on `GET /api/digest-reapply/<job>/plan`. Supply `renderContent` if the site's own digest renderer (`src/digest`) should draw the message instead of thrds' generic Slack rendering.
3. **Apply, from the page.** Add a Pages Function `functions/api/digest-reapply/[job]/[op].ts`:
   - Check `requireAdmin` first, and `sweepsOff` too (it edits the real digest).
   - Then `rewriteHandler(request, { plan: () => verifyPlan(<the job's plan from GCS>), client: () => new SlackClient({ token: env.SLACK_BOT_TOKEN }), receipts: d1Receipts(env.DB, job) })`.
   - Add a D1 migration with `rewriteReceiptsSchema()`, following the `migrations/` recipe in `specs/done/d1-migrations.md`.

This replaces the blind "for real" re-apply. That path stays as is for the cron and the CLI.

## For disky to decide

- **Images on the OP:** `apply_slack(op=True, dry_run=True)` skips hosting the re-rendered plot and icons, so the planned OP body may point at an image that isn't hosted yet. There are two options:
  - Host the plot and icons during the plan job. They're content-addressed, so this is safe whether or not the plan is applied.
  - Keep the OP out of plans for now.
- **Digest state:** a real `apply_slack` also updates `posted[day]["scan"]` in the month's state. An apply from the page doesn't touch the state. That only matters if converge reads `scan` to decide whether to re-edit; if it does, the next converge re-edits and nothing is lost. Check whether that's acceptable, or have the page's apply call back into a state update.
- **Pins:** bump `cloud/pyproject.toml` from py `c0bccc5` to `d15a889`. Add `@open-athena/thrds` to `site/package.json`, which has no thrds dependency today; only `fleet` does, at `#12f4449`. Do the bump through `pds gh thrds`, by SHA.
- **Fleet's card:** the fleet post (`fleet/`, `DISKY_SLACK_BOT_TOKEN`) is a single message that's edited in place. If its sender ever changes, the same plan/review path reposts it. `rewriteHandler` would then run in `fleet/functions`, with KV-backed receipts. Nothing is needed for that now.
