-- The path index now has three sort orders (specs/path-agnostic-serving.md §2.3):
--   'path'  sorted (depth, path)   — the drill index (row groups prune by depth/path)
--   'user'  sorted (usr, depth, path) — a user lens's groups prune by usr
--   'team'  sorted (team, depth, path) — a team lens's groups prune by team
-- Add a `variant` discriminator (default 'path' = the existing index) and the
-- primary-sort-key stats each lens prunes on (`u_*` usr range, `t_*` team range).
-- Recreate the 0013 tables (their data is re-synced by index-sync anyway).
DROP TABLE IF EXISTS index_groups;
DROP TABLE IF EXISTS index_schema;

CREATE TABLE index_schema (
  date        TEXT NOT NULL,
  variant     TEXT NOT NULL DEFAULT 'path',
  version     INTEGER NOT NULL,
  schema_json TEXT NOT NULL,
  PRIMARY KEY (date, variant)
);

CREATE TABLE index_groups (
  date      TEXT NOT NULL,
  variant   TEXT NOT NULL DEFAULT 'path',
  rg        INTEGER NOT NULL,
  d_min     INTEGER NOT NULL,   -- depth stats (secondary within a single key)
  d_max     INTEGER NOT NULL,
  p_min     TEXT NOT NULL,      -- path stats
  p_max     TEXT NOT NULL,
  b_max     INTEGER NOT NULL,   -- max bytes (threshold prune; = lens bytes in by-user/team)
  u_min     TEXT,               -- usr range (by-user primary key; NULL elsewhere)
  u_max     TEXT,
  t_min     TEXT,               -- team range (by-team primary key)
  t_max     TEXT,
  row_start INTEGER NOT NULL,
  row_end   INTEGER NOT NULL,
  rg_json   TEXT NOT NULL,
  PRIMARY KEY (date, variant, rg)
);
CREATE INDEX idx_index_groups_depth ON index_groups (date, variant, d_min, d_max);
CREATE INDEX idx_index_groups_user  ON index_groups (date, variant, u_min, u_max);
CREATE INDEX idx_index_groups_team  ON index_groups (date, variant, t_min, t_max);
