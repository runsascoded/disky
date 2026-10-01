-- A read-only allowlist tier (specs/mint-link-allowlist.md): `read_only = 1`
-- admits the email at the base read scope (`<base>:read`, what a read-only
-- share link carries — every read endpoint, no write) instead of the base
-- scope. A share link minted read-only with "Allowlist" ticked writes such a
-- row; a full link to the same address widens it back to 0.
ALTER TABLE allowed_emails ADD COLUMN read_only INTEGER NOT NULL DEFAULT 0;
