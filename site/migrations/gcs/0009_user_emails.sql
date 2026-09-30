-- Sign-in email → canonical attribution user id (identities.yaml spelling).
-- Powers the /mark "My files" tab: the viewer arrives as an email, the tree
-- attributes bytes to user ids. App-owned (edit at /admin/db/user_emails)
-- rather than baked into the snapshot, so mismatches are fixable instantly.
CREATE TABLE user_emails (
  email TEXT PRIMARY KEY,               -- lowercased
  user  TEXT NOT NULL,                  -- canonical user id, e.g. 'calvin-xu'
  who   TEXT NOT NULL,
  ts    INTEGER NOT NULL
);
CREATE INDEX user_emails_user ON user_emails (user);
