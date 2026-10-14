-- `@open-athena/auth` 0002: where a client is, from Cloudflare's `request.cf`,
-- in place of its address (which stays an HMAC in `ip_hash`). Additive, so the
-- prod D1 takes it in place. Apply before deploying an auth build ≥ `e14287a`
-- (its access-log INSERT writes these columns).
ALTER TABLE access_log ADD COLUMN city TEXT;
ALTER TABLE access_log ADD COLUMN region TEXT;
ALTER TABLE access_log ADD COLUMN as_org TEXT;  -- network operator (`cf.asOrganization`)
