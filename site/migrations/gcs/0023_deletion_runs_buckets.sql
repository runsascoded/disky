-- The bucket cut a run was dispatched with (`sweep execute -b …`), as a
-- comma-separated list; NULL = every bucket in the plan. Written at run start.
ALTER TABLE deletion_runs ADD COLUMN buckets TEXT;
