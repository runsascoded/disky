-- `@open-athena/auth`'s migration 0006: a link's key kept for staff to copy again (`gate.mint({ keepToken })`),
-- AES-GCM-sealed. NULL: not kept. Every grant read selects `token_enc IS NOT NULL AS kept` from the bump to auth
-- 5b64199 on, so apply this BEFORE deploying that bump: without it, every grant read (share links, a job's token) fails.
ALTER TABLE grants ADD COLUMN token_enc TEXT;
