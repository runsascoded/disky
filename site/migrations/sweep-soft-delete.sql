-- The sweep's minimum soft-delete retention (`/admin`, `/api/sweep/settings`), one row, none = 7 days; and each run's record
-- of the minimum it ran under and the narrowest retention measured on its buckets (0: no undo; NULL: not recorded, a run from
-- before this, whose image required 7 days). Safe with a run in flight: its INSERT names its own columns.
CREATE TABLE IF NOT EXISTS sweep_settings (
  id                   INTEGER PRIMARY KEY CHECK (id = 1),
  min_soft_delete_days INTEGER NOT NULL CHECK (min_soft_delete_days BETWEEN 0 AND 90),
  updated_by           TEXT NOT NULL,
  updated_at           INTEGER NOT NULL
);

ALTER TABLE deletion_runs ADD COLUMN min_soft_delete_days INTEGER;
ALTER TABLE deletion_runs ADD COLUMN soft_delete_days INTEGER;
