-- Faces an activity post's sender shows in Slack / Discord (`_lib/publicAvatar.ts`): each image a post used, keyed by
-- the SHA-256 of its bytes and served publicly at `/a/<sha256>.<ext>` (`functions/a/[[path]].ts`). Nothing else is
-- stored: a profile's or grant's own face is copied in only when a post uses it, a generated identicon when one did.
CREATE TABLE public_avatars (
  sha256      TEXT PRIMARY KEY,
  type        TEXT NOT NULL,
  data        TEXT NOT NULL,
  created_at  INTEGER NOT NULL
);
