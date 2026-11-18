-- The digest templates' rename (`cw` → `quota`, `gcs` → `cost`): each `digest_settings` row re-keyed to its template's
-- name. Data only (`schema.sql` unchanged). Until a database has it, `/api/digest-settings` reads an old-named row in
-- its name's place and a write moves it (`getSettings` / `putSettings`). Where a template has a row under both names
-- (a site still on the old code writing to a shared D1), the newer `updated_at` is kept, as `getSettings` reads.
DELETE FROM digest_settings WHERE template IN ('cw', 'gcs') AND EXISTS (
  SELECT 1 FROM digest_settings n
  WHERE n.template = CASE digest_settings.template WHEN 'cw' THEN 'quota' ELSE 'cost' END AND n.updated_at >= digest_settings.updated_at
);
DELETE FROM digest_settings WHERE template IN ('quota', 'cost') AND EXISTS (
  SELECT 1 FROM digest_settings o WHERE o.template = CASE digest_settings.template WHEN 'quota' THEN 'cw' ELSE 'gcs' END
);
UPDATE digest_settings SET template = CASE template WHEN 'cw' THEN 'quota' WHEN 'gcs' THEN 'cost' END WHERE template IN ('cw', 'gcs');
