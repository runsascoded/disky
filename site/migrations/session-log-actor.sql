-- @open-athena/auth migrations/session-log/0002_actor.sql: trusted recipient identity, nullable for older rows.
ALTER TABLE session_log ADD COLUMN grant_id TEXT;
ALTER TABLE session_log ADD COLUMN subject_name TEXT;
