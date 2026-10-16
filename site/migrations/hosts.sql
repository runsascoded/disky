-- The capture hosts a deployment serves, each one a store (specs/hosts-registry.md): the ingest
-- resolves a capture's host to its store here, and the site serves every live host's store
-- without a deploy. `store` = 'primary' for the deployment's own host.
CREATE TABLE hosts (
  id TEXT PRIMARY KEY,
  store TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  home TEXT,
  root TEXT,
  mark TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  created_by TEXT,
  revoked_at TEXT
);
