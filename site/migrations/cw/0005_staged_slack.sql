-- Staged-deletion review in Slack (specs/staged-slack.md). Each staged plan
-- gets one thread in the deployment's admin channel: the parent message
-- (kept current: counts, the latest dry-run, the action buttons) and a reply
-- per event (stage, unstage, reject, dispatch, run result). `slack_channel` /
-- `slack_ts` locate the parent; NULL = not posted yet.
ALTER TABLE plans ADD COLUMN slack_channel TEXT;
ALTER TABLE plans ADD COLUMN slack_ts TEXT;

-- The plan's exact item set when a run was dispatched (sha-256 of the sorted
-- canonical prefixes, 16 hex). A real run from Slack requires a finished
-- dry-run whose digest equals the plan's current one — "you reviewed exactly
-- this set" — so a batch staged after the dry-run forces a fresh one.
ALTER TABLE deletion_runs ADD COLUMN plan_digest TEXT;
