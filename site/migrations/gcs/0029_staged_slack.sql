-- Staged-deletion review in Slack (specs/staged-slack.md) — the same columns
-- as cw's `migrations/cw/0005_staged_slack.sql`. Each staged plan gets one
-- thread in the deployment's admin channel: the parent message (kept current:
-- counts, the latest dry-run, the action buttons) and a reply per event
-- (stage, unstage, reject, dispatch, run result). `slack_channel` / `slack_ts`
-- locate the parent; NULL = not posted yet.
ALTER TABLE plans ADD COLUMN slack_channel TEXT;
ALTER TABLE plans ADD COLUMN slack_ts TEXT;

-- The plan's exact item set a run acted on (sha-256 of the sorted canonical
-- prefixes, 16 hex: `planDigest` in functions/_lib/plans.ts). A real run
-- requires a finished dry-run whose digest equals the plan's current one —
-- "you reviewed exactly this set" — so a batch staged after the dry-run forces
-- a fresh one. On gcs the executor writes `deletion_runs` from inside Batch;
-- the site fills this column in from the job's `PLAN_DIGEST` env once the run
-- has finished (functions/_lib/sweepReflect.ts). NULL (not yet reflected, or
-- from before digests) and '' (the run ended without a result) never open
-- the gate.
ALTER TABLE deletion_runs ADD COLUMN plan_digest TEXT;
