-- The claims slice of each cached manifest, stored beside the body so a user
-- lens (one read per scan for the series) fetches ~250 KB, not the ~3 MB body.
-- Rows from before this column are read through the body until the next
-- ledger head recomputes them.
ALTER TABLE mark_totals ADD COLUMN claims TEXT;
