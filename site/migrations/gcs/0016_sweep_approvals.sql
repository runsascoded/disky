-- Sweep band approvals — the human owner-verification the empty owner axis
-- couldn't provide (specs/sweep-executor.md § Phase 2). One row per approved
-- band prefix; `sweep manifest --approved-from-site` treats these as the
-- eligibility gate, and the /sweep page writes them (admin scope) with the
-- attribution evidence in view.

CREATE TABLE sweep_approvals (
  prefix TEXT PRIMARY KEY,   -- gs://marin-<bucket>/<dir>/ band
  scan TEXT NOT NULL,        -- scan the evidence was reviewed against
  head INTEGER NOT NULL,     -- ledger head at approval
  note TEXT,
  who TEXT NOT NULL,         -- approver (server-set)
  ts INTEGER NOT NULL        -- approval time (server-set)
);
