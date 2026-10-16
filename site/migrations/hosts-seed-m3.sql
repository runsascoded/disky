-- m3's capture hosts (specs/hosts-registry.md): this Mac is the primary store; the NAS publishes under `snapshots/nas/`.
INSERT INTO hosts (id, store, label, home, root, mark) VALUES
  ('m3', 'primary', 'm3', 'Users/ryan', NULL, NULL),
  ('nas', 'nas', 'NAS', NULL, 'mnt/user', 'nas');
