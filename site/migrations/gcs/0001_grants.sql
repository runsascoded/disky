-- Grants: named, capped, expiring share links (a grant with `email` set is a
-- magic link). Tokens are shown once at mint; only SHA-256 hashes are stored.
--
-- Every knob is optional and defaults sane: zero-config is an unlimited-use,
-- never-expiring, unnamed link.
--
-- Timestamps are epoch seconds (INTEGER), not ISO text: they compare and bucket
-- cheaply, and the access log joins against them.
CREATE TABLE grants (
  id            TEXT PRIMARY KEY,       -- random, not autoincrement: doesn't leak counts
  token_hash    TEXT NOT NULL UNIQUE,   -- SHA-256, base64url
  name          TEXT,                   -- admin-side label: "Bob Smith (donor)"
  note          TEXT,                   -- freeform: why this exists
  subject_json  TEXT,                   -- optional pre-loaded identity: {first,last,email,avatar}
  email         TEXT,                   -- if set: magic-link semantics (bind on redeem)
  scopes        TEXT NOT NULL,          -- space-separated
  max_redeems   INTEGER,                -- NULL = unlimited; counts sessions minted, not requests
  redeems       INTEGER NOT NULL DEFAULT 0,
  expires_at    INTEGER,                -- NULL = never
  session_ttl   INTEGER,                -- seconds; NULL = inherit app default
  created_at    INTEGER NOT NULL,
  created_by    TEXT NOT NULL,
  revoked_at    INTEGER,
  first_used_at INTEGER,
  last_used_at  INTEGER
);

CREATE INDEX grants_created_at ON grants (created_at DESC);
