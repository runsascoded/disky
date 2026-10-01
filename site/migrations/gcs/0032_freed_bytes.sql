-- A dry run's measured reclaim (specs/apfs-sharing.md): what deleting the
-- staged set as a whole would actually free, where the executor can tell —
-- the laptop drainer's extent intersection, which doesn't count bytes the set
-- shares with a clone or hardlink outside it (uv / pnpm caches). NULL = not
-- measured (a real run, another executor, or the measurement failed);
-- `deleted_bytes` stays the per-path would-delete size.
ALTER TABLE deletion_runs ADD COLUMN freed_bytes INTEGER;
