-- A digest thread switch's reviewed edits and reorders (specs/digest-thread-variant.md, Ryan's 2026-11-27 asks): the old
-- replies the reviewed plan edits in place (JSON [ts…]; a switch may touch only those and `deletes`), and the people's
-- replies the admin allowed to end up above the new ones (JSON [ts…]; NULL: none). Needs `digest-thread-variant`.
ALTER TABLE digest_threads ADD COLUMN edits TEXT;
ALTER TABLE digest_threads ADD COLUMN reorder TEXT;
