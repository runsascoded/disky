-- The admin list (`ADMIN_LIST`; `/admin`, `/api/admins`): the addresses that are admins, each with who added it and when,
-- and the person's GitHub handle where known (compared with a GitHub org's owners by `dt-cloud admins check -o <org>`).
-- With `ADMIN_LIST` on, a row here (or the break-glass `ADMIN_BOOTSTRAP`) is the only way an email is an admin.
CREATE TABLE IF NOT EXISTS admin_emails (
  email  TEXT PRIMARY KEY,
  note   TEXT,
  who    TEXT NOT NULL,
  ts     INTEGER NOT NULL,
  github TEXT
);
