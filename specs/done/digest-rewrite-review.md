# Digest re-apply: review and apply in `/admin/slack` (thrds bulk rewrite)

From the thrds session (`~/c/thrds`). thrds now has a bulk-rewrite pipeline in Python and TS with one plan format, plus a review UI built to be embedded in an app. This spec puts that UI in front of the existing digest re-apply flow (`specs/digest-runway-marks.md` §4, `site/functions/api/digest-reapply.ts`), so an admin reviews exactly what will change before anything is edited, and applies a selection from the page.

**Status: implemented** (scope: the digest re-apply, "Proposed flow" 1–3). What landed is under "Implemented"; the fleet card and the emoji-change apply come later on the same path.

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

## Decisions (Ryan, 2026-11-22)

- **Images on the OP:** host the re-rendered plot and icons during the plan job; a planned OP edit must point at hosted images.
- **Digest state:** check whether converge reads `posted[day]["scan"]`; if the page's apply leaving it alone costs only a redundant re-edit at the next converge, accept that and document it, else have the page's apply record the state too.
- **Pins:** `cloud/pyproject.toml`'s thrds → `py` `d15a889`; `site/package.json` gains `@open-athena/thrds` by `ts-dist` SHA (the build of `38dd767`).
- **Scope:** the digest re-apply only (flow 1–3); edits to `digest.py` kept to the `--plan` path.

## Implemented

### 1. Plan (`dt-cloud digest -n -j PATH`)

- `-j/--plan PATH` (a local path or a `blobfs` URL) with `-n` and a selector (`-o`, `-d`, `-y`): after printing the dry run's edits, `find_ids` reads each changed message and `dg.rewrite_plan(found, changes)` plans it; `dg.write_plan` writes `Plan.to_json()` as `Plan.save` would. Without `-n` and a selector it's a usage error. The last stdout line is `plan: <summary> -> PATH`.
- **The wire form.** The digest's bodies are markdown that `SlackClient.edit` converts, but a rewrite plan carries Slack's wire form. `dg.edit_desired(body)` mirrors `edit`: trailing `![alt](url)` lines become image blocks under a `section` of the converted text (`thrds.mrkdwn.to_slack`), otherwise the text alone (an edit then sends `blocks: []`, as `edit` does). The sender is left unset, so a sender-variant reply is an `edit`. A found message without a change plans as `skip` ("not selected").
- **OP images.** `apply_slack(..., dry_run=True, host=True)` (what `-j` passes) still renders and hosts the OP's plot, so its planned edit names a served image: the deployment-specific URL of the plan job's own Pages deployment (`pages_deploy`). A plan that's never applied leaves that deployment unused. Two caveats on "content-addressed": the plot's file name is the month's (`state["plot_name"]`), not a content hash, so it is the *deployment* URL that's immutable; and the config's `plot_branch` alias does move to the plan job's deployment, as a converge's would. A live OP names its own deployment URL, so it's unaffected unless wrangler's output named none (then the OP points at `plot_base`, which now serves the plan's render). The reply icons are the static `arrows/` avatars, which every deploy of the icons dir re-uploads unchanged.
- **The Batch job.** `reapplyArgs(req, plan)` adds `-j /gcs/<data>/digest/reapply/<job>.plan.json` (the gcsfuse mount, beside the `.txt`) to a dry run of the OP or of days (`plans(req)`). A whole-thread run (`target: thread`) writes no plan: its for-real form is a converge, which also posts.

### 2. Review (`/admin/slack`)

- A dry run of the OP or of days, once its job `SUCCEEDED`, shows `Review` in place of the raw output (still under "The job's output"): `PlanView` over `GET /api/digest-reapply/<job>/plan`, with `postRewrite` to `…/check` and `…/apply`, the outcomes recorded per ts, and `messageHref` built from the bundle's permalink workspace. No `renderContent`: thrds' Slack rendering (mrkdwn, the OP's section + image blocks, sender names and avatars) shows the digest faithfully enough; the site's own renderer draws the OP from rows, not from a message's content.
- Custom emoji (`:arrow_deg80:`, `:runway-3:`, `:pool-chip-…:`) draw as their images: `Review` passes `PlanView` its `emoji` map from `GET /api/slack-emoji` (`functions/api/slack-emoji.ts`, admin): `emoji.list` through thrds' `SlackClient` with the site's `SLACK_BOT_TOKEN`, each `alias:` resolved to its target's URL (`customEmojiUrls`, `@disk-tree/pages-kit/emoji`, which fleet's `workspaceCustom` shares), cached per isolate for 5 minutes. No token, or no `emoji:read`: `{emoji: {}}`, logged once, and each shows as its `:name:`. Tests: `functions/api/slackEmoji.test.ts`.
- The page's blind "apply" (a for-real Batch job) is now offered only for the whole thread ("converge"); the OP and days are applied from the review. `POST /api/digest-reapply` still accepts `dry_run: false` for any target, and the CLI is unchanged.

### 3. Apply (`functions/api/digest-reapply/[job]/[op].ts`)

- `GET plan`, `POST check`, `POST apply`, all admin (`gates.test.ts`). Order: `requireAdmin`; the job id and op from the path (404 otherwise); `apply` refused where `sweepsOff` (403; the plan and a check only read, so they stay open); the run's `digest_reapply` row (404 none; 404 for a run that writes no plan); `SLACK_BOT_TOKEN` for a check or apply (503); the plan from `gs://<data>/digest/reapply/<job>.plan.json` (404: not written; 502: the read failed); `verifyPlan` (500 on a bad hash); then `rewriteHandler` with `SlackClient({token, channel: <the run's channel>})` and `d1Receipts(env.DB, <job>)`. Responses are `no-store`.
- **D1:** recipe `site/migrations/rewrite-receipts.sql` (`thrds_rewrite_receipts` + its index, exactly `rewriteReceiptsSchema()`), `schema.sql` regenerated. Every outcome of a real apply is a row (`done`, `already`, `stale`, …); `done` / `already` make a later request skip the message (`receipt`).
- Tests: `functions/api/digestRewrite.test.ts` (plan served with counts; why there's no plan; check writes nothing; apply once, then `receipt`; `stale` on a changed message; ids that aren't edits refused; the token needed only for writes), `_lib/sweepsOff.test.ts` (apply refused before any fetch; unset, it goes on to read the plan), `api/digest-reapply.test.ts` (`-j` on the dry runs that plan). Python: `cloud/tests/test_digest_quota.py` (`host` routes the OP render to hosting only for a plan; `edit_desired`; `rewrite_plan`'s edit / noop / skip; the CLI's `-j`).

### Digest state

Converge reads `posted[day]["scan"]` only on an edited variant (`quota`'s `body`): it re-edits a day's reply when the day's unit names a later scan than the state records (`edited_variants`; the `sender` variant and the `cost` template never re-edit). `apply_slack` for real also writes the unit's current scan into the state; the page's apply doesn't touch the state. That differs only for a day whose newer scan has landed but whose converge hasn't run yet, which in steady state is the current day between a scan and its own converge (the same job). There, the next converge re-edits the reply to the current render: redundant when the plan used the live settings, and a revert to the live settings when the plan used candidate settings (`-p`/`-q`), where the CLI's for-real path would have kept the candidate render until that day's next scan. Past days' recorded scans already equal their units', so nothing is lost there; the OP is re-edited by every converge on either path. Accepted: the page's apply records no state.

## Deployment, beyond merging

- Apply `site/migrations/rewrite-receipts.sql` to each deployment's D1, then `git rm` it (`specs/done/d1-migrations.md`). Without the table an apply fails when it records its first outcome.
- The site needs `SLACK_BOT_TOKEN` (a secret, already set where staged-plan threads post), and it must be the digest bot's token, the one the job posts with: `chat.update` only edits the token's own messages. A deployment whose site holds another bot's token gets `failed` (`cant_update_message`) on every apply.
- No new vars: the plan route reads `DATA_BUCKET` and `GCP_SA_KEY` as `/api/digest-reapply` does. The job image needs this commit's `dt-cloud` (the `-j` flag and the thrds pin), so the scan image is rebuilt before a dry run writes plans; until then the job fails on the unknown `-j` (the page's dry runs of the OP or of days, not the cron).

## Later (same path)

- **Fleet's card:** the fleet post (`fleet/`, `DISKY_SLACK_BOT_TOKEN`) is a single message that's edited in place. If its sender ever changes, the same plan/review path reposts it. `rewriteHandler` would then run in `fleet/functions`, with KV-backed receipts.
- **An emoji-change apply** over the digest's history (`find_history` + `substitute`), reviewed and applied the same way.
