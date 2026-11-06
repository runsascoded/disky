-- The admin list (`ADMIN_LIST`; `/admin`, `/api/admins`): each listed address's GitHub handle, where known, so the list can be
-- compared with a GitHub org's owners (`dt-cloud admins check -o <org>`).
ALTER TABLE admin_emails ADD COLUMN github TEXT;
