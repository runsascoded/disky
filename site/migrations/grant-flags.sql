-- Per-link flags from `@open-athena/auth` (its `migrations/0005_grant_flags.sql`), both editable by staff after minting
-- (`PATCH /api/auth/grants/:id`). The package's D1 store reads both columns, so apply this before deploying a site on
-- that dist.
-- service: held by a job, not a person (never bound to an email); its activity posts only notable events
-- (`_lib/activity.ts`). `dt-cloud job-token` mints with it set; `/admin` toggles it.
-- anon: the holder never sees the link's subject or email; staff still do.
ALTER TABLE grants ADD COLUMN service INTEGER NOT NULL DEFAULT 0;
ALTER TABLE grants ADD COLUMN anon INTEGER NOT NULL DEFAULT 0;
