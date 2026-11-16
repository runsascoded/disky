-- A person's custom Slack emoji (`:name:`), as `@open-athena/auth`'s migration 0007 adds it: copied from the broker's
-- `oa_slack_emoji` claim at each sign-in, or its lookup. Never self-set. `/api/scan-runs` joins it to a manual run's
-- starter (`trigger_emoji`), which the scan-health thread shows.
ALTER TABLE profiles ADD COLUMN slack_emoji TEXT;
