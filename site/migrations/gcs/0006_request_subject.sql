-- A request that becomes a grant should arrive knowing who the person is, not
-- just their address: `grants.subject_json` already models `{first,last,avatar}`
-- for the greeting and the watermark, and until now approval had nothing to put
-- there. Same column name and same JSON shape as `grants`, so the two tables
-- stay readable side by side.
--
-- Deliberately *not* a first/last pair of columns: the shape is already decided
-- in `Subject`, and a request may carry only one of them (or neither).
ALTER TABLE access_requests ADD COLUMN subject_json TEXT;
