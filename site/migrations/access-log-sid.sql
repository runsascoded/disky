-- `@open-athena/auth`'s migration 0008 (`access_log_sid`): which browser an access-log row came from (`sid`: a keyed
-- hash of its session cookie, or of its network and user agent when it presented a key), so the hourly dedupe of
-- views and repeat denies is per browser, not per grant (everyone opening one share link shares `session_sub`). ''
-- on earlier rows. Apply before deploying the auth version that writes it (every insert names `sid`); the version
-- before inserts with a target-less `ON CONFLICT DO NOTHING`, so it runs unchanged against the new index.
ALTER TABLE access_log ADD COLUMN sid TEXT NOT NULL DEFAULT '';
DROP INDEX access_log_dedupe;
CREATE UNIQUE INDEX access_log_dedupe ON access_log (event, session_sub, sid, path, bucket) WHERE bucket IS NOT NULL;
