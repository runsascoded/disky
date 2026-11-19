-- Where a scan run was reported (`scan_runs.thread_url`): the Slack permalink of its reply in the deployment's run
-- thread in `#<deploy>-admin`, written by the thread's reporter (`nominal.link_threads`). NULL: not reported in a
-- thread (no `nominal` profile, a child run, or reported before this column).
ALTER TABLE scan_runs ADD COLUMN thread_url TEXT;
