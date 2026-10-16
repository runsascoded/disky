-- Device enrollment (specs/device-api.md): each enrolled host's device token (an auth grant, scoped
-- `host:<id>`), the app's reported OS and version, and when its token was last used.
ALTER TABLE hosts ADD COLUMN grant_id TEXT;
ALTER TABLE hosts ADD COLUMN os TEXT;
ALTER TABLE hosts ADD COLUMN version TEXT;
ALTER TABLE hosts ADD COLUMN seen_at TEXT;
