-- Full-info share cards (specs/done/dogi.md): the cw twin of gcs `0034`. Each
-- mint of a per-view token (10 random base62 chars) is one row, and the row is
-- the whole truth: a token is honoured only for its exact view, unexpired and
-- unrevoked. New table, no references in or out.
CREATE TABLE og_tokens (
  id         INTEGER PRIMARY KEY,
  token      TEXT NOT NULL,
  kind       TEXT NOT NULL,     -- the card kind (`map`, `staged`, …)
  view       TEXT NOT NULL,     -- the canonical view params it covers
  page       TEXT NOT NULL,     -- the page URL path + view, without `og=`
  minted_by  TEXT NOT NULL,     -- the minter's email (or `slack:` for server posts)
  minted_ts  INTEGER NOT NULL,
  exp_day    INTEGER NOT NULL,  -- days since 2026-01-01; valid through that day (UTC)
  revoked_by TEXT,
  revoked_ts INTEGER
);
CREATE INDEX idx_og_tokens_token ON og_tokens (token);
CREATE INDEX idx_og_tokens_minted ON og_tokens (minted_ts DESC);
