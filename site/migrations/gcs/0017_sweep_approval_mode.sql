-- Approval mode: 'slice' (default — the executor's attribution gate deletes
-- only directories majority-attributed to the band's sweeper) vs 'full'
-- (whole band regardless of attribution; for bands verified out-of-band).
ALTER TABLE sweep_approvals ADD COLUMN mode TEXT NOT NULL DEFAULT 'slice';
