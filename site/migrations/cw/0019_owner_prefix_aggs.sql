-- Each scan's priced assignment prefixes (`_lib/ownerTotals.ts`): a prefix's bytes/objects on a scan never
-- change, so a new ledger head re-prices only its new prefixes instead of every live one.
CREATE TABLE owner_prefix_aggs (
  scan       TEXT NOT NULL,     -- scan id the prefixes are priced on
  idx        TEXT NOT NULL,     -- the index read (`<mode>:<gen>`): a reindex prices again
  body       TEXT NOT NULL,     -- JSON: { v, buckets, asked, aggs }
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (scan, idx)
);
