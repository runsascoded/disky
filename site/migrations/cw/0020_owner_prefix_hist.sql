-- Assignment prefixes priced over an interval store's whole history (`_lib/ownerTotals.ts`,
-- specs/owner-prefix-history.md): one row per prefix holding its aggregate's versions, so an owner change
-- writes nothing and a new prefix writes one row, however many scans the store holds.
CREATE TABLE owner_prefix_hist (
  idx        TEXT NOT NULL,     -- the interval store's index tag (`<mode>:<gen>`): a new generation prices again
  depth      INTEGER NOT NULL,  -- the prefix's depth; 0 = the roots marker (every depth-1 path priced through `hz`)
  path       TEXT NOT NULL,
  hz         INTEGER NOT NULL,  -- the newest scan ordinal (`vf`/`vt` units) the versions are complete through
  versions   TEXT NOT NULL,     -- JSON: [[vf, vt, agg], …], agg = { b, o, us, uo, cb }; absent between = not in the scan
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (idx, depth, path)
);
