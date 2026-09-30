-- The access log. One store for auth-lifecycle events and (optionally) views,
-- so "who viewed what" joins to `grants` natively instead of across systems.
--
-- Raw IPs are never stored: `ip_hash` is an HMAC under the app's session
-- secret, which correlates sessions without retaining addresses.
CREATE TABLE access_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  event       TEXT NOT NULL,   -- redeem | deny | revoke | request | view | signin | signout
  grant_id    TEXT,
  session_sub TEXT,            -- `e:<email>` | `g:<id>`
  path        TEXT,
  status      INTEGER,
  ip_hash     TEXT,
  ua          TEXT,
  country     TEXT,
  referer     TEXT,
  reason      TEXT,            -- deny detail: bad-token | revoked | expired | exhausted | not-allowed
  bucket      INTEGER          -- floor(ts/3600) for `view` rows; NULL otherwise
);

CREATE INDEX access_log_ts ON access_log (ts DESC);
CREATE INDEX access_log_grant ON access_log (grant_id, ts DESC);

-- Dedupe `view` events per (session, path, hour) in the DB, so a chatty SPA
-- writes one row an hour per view target instead of thousands. Lifecycle events
-- (bucket IS NULL) are excluded and always land.
CREATE UNIQUE INDEX access_log_view_dedupe ON access_log (session_sub, path, bucket) WHERE bucket IS NOT NULL;
