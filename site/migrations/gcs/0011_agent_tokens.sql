-- Personal agent tokens (specs/actions-ledger.md § API).
--
-- One row per user: the id of the grant currently serving as their `gcs`-scoped
-- personal token, so rotation can revoke the predecessor and the UI can show
-- "active since <created>" without ever seeing the token (only its hash lives in
-- the grants table). The raw token is shown exactly once, at mint.
CREATE TABLE agent_tokens (
  email    TEXT PRIMARY KEY,          -- lower-cased owner email
  grant_id TEXT NOT NULL,             -- grants.id of the live token grant
  created  INTEGER NOT NULL           -- unix seconds, server-assigned
);
