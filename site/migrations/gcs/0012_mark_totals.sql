-- Exact keep / sweep totals per (scan, ledger head): the folded ledger priced
-- against the floor-free path index by /api/marks/totals, cached here so the
-- ~200 MB of index reads happen once per ledger change, not per request
-- (specs/path-agnostic-serving.md §2.3). Rows for old heads are dead weight;
-- prune anything but the newest head per scan whenever convenient.
CREATE TABLE mark_totals (
  scan        TEXT NOT NULL,             -- snapshot date the totals are priced on
  head        INTEGER NOT NULL,          -- max(actions.id) the fold included
  body        TEXT NOT NULL,             -- JSON response body
  computed_ts INTEGER NOT NULL,
  ms          INTEGER,                   -- compute wall time
  PRIMARY KEY (scan, head)
);
