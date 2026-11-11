-- "Run a scan" (`_lib/scanDispatch.ts`): one row per dispatch, claimed (`ts`, `who`), then `dispatched` (the Cloud
-- Run execution and operation) or `failed` (`error`, which releases the claim). A claim is one conditional insert.
CREATE TABLE scan_dispatch (
  id         TEXT PRIMARY KEY,
  ts         INTEGER NOT NULL,
  who        TEXT NOT NULL,
  job        TEXT NOT NULL,
  state      TEXT NOT NULL CHECK (state IN ('claimed', 'dispatched', 'failed')),
  execution  TEXT,
  operation  TEXT,
  error      TEXT,
  updated_ts INTEGER NOT NULL
);
CREATE INDEX scan_dispatch_ts ON scan_dispatch (ts);

-- The dispatches logged before this table, where a deployment's `admin_edits` allowed them (`tbl = 'scan_dispatch'`:
-- `claim`, then `dispatched` or `failed`); none elsewhere. Their `admin_edits` rows stay as they are.
INSERT INTO scan_dispatch (id, ts, who, job, state, execution, operation, error, updated_ts)
SELECT c.pk, c.ts, c.who, COALESCE(json_extract(c.new_json, '$.job'), ''),
  COALESCE(f.action, d.action, 'claimed'),
  json_extract(d.new_json, '$.execution'), json_extract(d.new_json, '$.operation'),
  CASE WHEN f.id IS NULL THEN NULL
    WHEN json_extract(f.new_json, '$.status') IS NULL THEN json_extract(f.new_json, '$.error')
    ELSE 'Cloud Run ' || json_extract(f.new_json, '$.status') || ': ' || json_extract(f.new_json, '$.error') END,
  COALESCE(f.ts, d.ts, c.ts)
FROM admin_edits c
LEFT JOIN admin_edits d ON d.tbl = c.tbl AND d.pk = c.pk AND d.action = 'dispatched'
LEFT JOIN admin_edits f ON f.tbl = c.tbl AND f.pk = c.pk AND f.action = 'failed'
WHERE c.tbl = 'scan_dispatch' AND c.action = 'claim';
