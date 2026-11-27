-- A digest thread's reply style per month (specs/digest-thread-variant.md): an admin's switch on /admin/slack, which
-- the job reads (`GET /api/digest-threads`) over the thread's live variant. `deletes` are the reviewed dry run's
-- replies to delete (JSON [ts…], from `plan_job`'s switch plan): the only ones a switch may delete.
CREATE TABLE digest_threads (
  channel    TEXT NOT NULL,
  month      TEXT NOT NULL,
  variant    TEXT NOT NULL,
  deletes    TEXT NOT NULL,
  plan_job   TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (channel, month)
);
-- The deployment default for a new month's thread (NULL: the config file's).
ALTER TABLE digest_settings ADD COLUMN variant TEXT;
-- A re-apply run's candidate variant (`-V`): a whole-thread dry run naming another than the live one plans a switch.
ALTER TABLE digest_reapply ADD COLUMN variant TEXT;
