-- The Slack digest's live format settings and re-apply runs (specs/digest-runway-marks.md §4).
-- One settings row per digest template, edited on /admin/slack (each change logged in `admin_edits`) and read by the job
-- (`GET /api/digest-settings`) over its config file's `params` / `quota_marks`.
CREATE TABLE digest_settings (
  template    TEXT PRIMARY KEY,
  params      TEXT NOT NULL,
  quota_marks TEXT NOT NULL,
  updated_by  TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);
-- Each re-apply from /admin/slack: a Batch job running the digest CLI on exactly the chosen messages of a month's thread
-- (`target`: `thread` | `op` | `YYYY-MM-DD` | `YYYY-MM-DD..YYYY-MM-DD`), a dry run (`-n`) or for real; its output at `out`.
CREATE TABLE digest_reapply (
  job_id      TEXT PRIMARY KEY,
  month       TEXT NOT NULL,
  channel     TEXT NOT NULL,
  target      TEXT NOT NULL,
  dry_run     INTEGER NOT NULL,
  params      TEXT,
  quota_marks TEXT,
  actor       TEXT NOT NULL,
  ts          INTEGER NOT NULL,
  out         TEXT NOT NULL
);
CREATE INDEX digest_reapply_ts ON digest_reapply (ts);
