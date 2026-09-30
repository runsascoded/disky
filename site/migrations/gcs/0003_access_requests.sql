-- Request access: the wall's second affordance, for everyone who isn't staff.
-- Approval mints a grant (`grant_id`) and delivers it to `email`, which is why
-- no pre-verification round-trip is needed — typing someone else's address just
-- mails the real owner.
CREATE TABLE access_requests (
  id         TEXT PRIMARY KEY,
  email      TEXT NOT NULL,
  name       TEXT,
  note       TEXT,
  created_at INTEGER NOT NULL,
  status     TEXT NOT NULL,   -- pending | approved | denied | auto
  decided_at INTEGER,
  decided_by TEXT,
  grant_id   TEXT,
  ip_hash    TEXT             -- HMAC, for rate-limiting; never a raw address
);

CREATE INDEX access_requests_created_at ON access_requests (created_at DESC);
CREATE INDEX access_requests_status ON access_requests (status, created_at DESC);
CREATE INDEX access_requests_email ON access_requests (email, created_at DESC);

-- At most one open request per address: a re-visit updates the wall's "pending"
-- state rather than queueing a second row for an admin to wade through.
CREATE UNIQUE INDEX access_requests_one_pending ON access_requests (email) WHERE status = 'pending';
