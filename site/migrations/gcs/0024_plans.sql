-- Plans (specs/gcs-toward-union.md §5, seam 1) — a deletion plan as a
-- first-class object, so more than one can be drafted at once (plan A = old
-- checkpoints, plan B = tmp/, each with its own dry → real lifecycle and a
-- detail page). Adopted verbatim from cw-s3's plan shape (cw migration 0002)
-- so the two deployments converge on one schema; the merged lineage keeps
-- these here (seam 3). The plan is the curation layer; gcs's exercised
-- `sweep manifest`/`sweep execute` executor is what runs it, fed by a plan's
-- items instead of building an implicit plan from marks at dispatch.
--
-- `IF NOT EXISTS`: at the branch collapse this migration is shared, and
-- cw's live DB already carries these tables — a no-op there, a create here.

CREATE TABLE IF NOT EXISTS plans (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  note       TEXT,
  state      TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
  created_by TEXT NOT NULL,            -- the identity that drafted it
  created_ts INTEGER NOT NULL,         -- epoch seconds
  closed_ts  INTEGER                   -- set when state → closed
);

-- The prefixes a plan will sweep. Curated (added / removed) while the plan is
-- open; typically drawn from the current `sweep`-marked set (or the viewer's
-- eligible slice) but decoupled from it once added — removing the underlying
-- mark surfaces as drift, not an auto-removal. One prefix per (plan, prefix).
CREATE TABLE IF NOT EXISTS plan_items (
  plan_id  INTEGER NOT NULL REFERENCES plans(id),
  prefix   TEXT NOT NULL,             -- gs://<bucket>/<path>/ (trailing slash)
  note     TEXT,
  added_by TEXT NOT NULL,             -- the identity that added it
  added_ts INTEGER NOT NULL,
  PRIMARY KEY (plan_id, prefix)
);
