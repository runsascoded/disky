-- Agents that check in with this deployment. The laptop drainer (`disk-tree
-- dispatch -s`) writes its heartbeat every poll, and the `laptop` executor
-- refuses a dispatch while that's stale — the items stay staged and the
-- console says why (specs/m3-site.md Phase 3). One row per agent name.
-- `IF NOT EXISTS`: the laptop's D1 applied this as `0006_agents.sql` before the
-- renumber (0006 was taken by `0006_store_scoped_index.sql`); re-applying
-- under the new name is a no-op there.
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY,      -- `drainer`
  seen_ts INTEGER NOT NULL,   -- epoch seconds of the last check-in
  host TEXT                   -- the machine it runs on (informational)
);
