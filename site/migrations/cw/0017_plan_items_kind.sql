-- A plan item names either a folder prefix (`kind` NULL — every item staged
-- before this column, and every prefix since) or one exact object
-- (`kind = 'object'`: `prefix` then holds the key URI, no trailing slash, and
-- the executor deletes that key only — never `key.bak`, never `key/…`).
-- Readers take the kind from this column, never from the string's shape.
-- Additive: the prod D1 takes it in place (specs/file-assign.md). The gcs
-- lineage's twin also adds `actions.kind` / `owner_prefixes.kind`.
ALTER TABLE plan_items ADD COLUMN kind TEXT CHECK (kind IS NULL OR kind = 'object');
