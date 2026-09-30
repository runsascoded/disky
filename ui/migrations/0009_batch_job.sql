-- Staged delete (spec `specs/staged-delete.md`, CP8): the large-scope executor.
-- When a run's scope exceeds the CFN/inline threshold the drainer submits it to
-- AWS/GCP Batch instead of deleting inline. `batch_job` records the submitted
-- job id: the run stays `finished_ts IS NULL` (still running) but the drainer
-- skips it (the Batch job finishes it), so it isn't resubmitted every poll.
ALTER TABLE deletion_runs ADD COLUMN batch_job TEXT;
