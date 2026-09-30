-- App-owned sign-in allowlist + a generic audit trail for admin table edits.
--
-- `allowed_emails` replaces the per-email include list on the CF Access
-- policy: Access becomes a pure IdP (anyone can complete OTP/Google at
-- /auth/sso), and the app's email policy consults this table — non-staff
-- emails get scopes only while listed here. Sessions re-derive scopes per
-- request, so removing a row de-authorizes existing sessions immediately.

CREATE TABLE allowed_emails (
  email TEXT PRIMARY KEY,               -- lowercased
  note  TEXT,                           -- who this is / where they're from
  who   TEXT NOT NULL,                  -- admin who added the row
  ts    INTEGER NOT NULL                -- epoch seconds
);

-- Append-only history of every write made through /api/db (any table).
CREATE TABLE admin_edits (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  tbl      TEXT NOT NULL,
  pk       TEXT NOT NULL,
  action   TEXT NOT NULL,               -- 'insert' | 'update' | 'delete'
  who      TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  old_json TEXT,
  new_json TEXT
);
CREATE INDEX admin_edits_tbl ON admin_edits (tbl, ts);
