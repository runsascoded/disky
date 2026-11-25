-- Each run's end as posted (`announceFinished`, site/functions/_lib/stagedSlack.ts): one row per run and target (where
-- the deployment posts it: `slack`), `post_id` the message (`<channel>/<ts>`), NULL while a post is claimed. A finished
-- run with no row for its target is announced again (`missedRuns`). `*`: a run finished before this table, whose
-- announcement isn't known; it covers every target, so the backfill posts nothing old.
CREATE TABLE IF NOT EXISTS deletion_run_posts (
  run_id TEXT NOT NULL REFERENCES deletion_runs(run_id),
  target TEXT NOT NULL,
  post_id TEXT,
  posted_ts INTEGER NOT NULL,
  PRIMARY KEY (run_id, target)
);
INSERT OR IGNORE INTO deletion_run_posts (run_id, target, post_id, posted_ts)
  SELECT run_id, '*', NULL, finished_ts FROM deletion_runs WHERE finished_ts IS NOT NULL;
