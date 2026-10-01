# Mint form: let the recipient sign in, and give the link a copied face (`@open-athena/auth` `fa93d14`)

*(From the `$oa/auth` session, 2026-09-30, answering Ryan's "if I make them a share link tied to their email, that email would also get approved for OAuth flow? Or that should be an option in the mint-link flow?")*

## Upstream (auth `ef2517a`, dist `e01e68b`)

**`POST <basePath>/grants`** takes `allowlist: true` alongside `email`. After minting, it makes sure that address holds at least the link's scopes on the allowlist.
- **Effect:** the recipient can later sign in with Google or an emailed code, instead of being bounced to `?denied=`.
- **Existing rows:**
  - A row that already covers the scopes is left alone.
  - A narrower one (including a `sync:` row) is widened to the union and becomes `manual`, with the note `with link "<memo>"`.
- **Response:** gains `allowed: { email, status: 'added' | 'widened' | 'already' }`.
- **Validated before minting:**
  - 501 if the routes weren't given an `allowlist` store.
  - 400 if `email` isn't valid.
  - So a refused request leaves no link behind.
- **Revoking the link doesn't remove the row.** They're separate grants of access.
- **Also exported:** `allowForLink(store, email, scopes, { note, addedBy })`, for an app that mints some other way.

**Also in this release:** `seedProfile` now **defaults to true** in `oidcCallback` / `googleOneTapVerify`, so the explicit `seedProfile: true` from `609258f` can go. The `profiles` store on the gate is still required.

The auth demo's mint form shows the UX: an optional Email field, and a checkbox, default on, "Also let this email sign in with Google or an emailed code". See https://auth.oa.dev/admin (start a sandbox).

## Upstream, part 2 (auth `7b665e1`, dist `fa93d14`): faces are copied, and found from a profile URL

*(Ryan: "drop in someone's LinkedIn, GH, or other social profile and pull the avi from there… we def shouldn't just save 3rd-party AVI URLs, we should deep-copy and save the imgs.")*

- **Mint copies the avatar.** Before this, `POST /grants` stored `avatar` as the raw URL you pasted. That's what disky's mint form does today.
  - Every visitor's browser then fetched it from the third party.
  - LinkedIn image URLs (`media.licdn.com/...?e=<expiry>&t=<sig>`) stop working after a while.
  - Now `gate.mint` fetches the image once and sniffs it as PNG/JPEG/WebP/GIF. It stores a `data:` URI (≤64 KB, since disky binds no `assets` store), or 400s `{ error: 'invalid avatar', detail }` before minting anything.
- **`avatar` accepts more than an image URL:**
  - a GitHub profile URL or handle;
  - a Bluesky profile or handle;
  - a Mastodon profile or `@user@instance`;
  - a direct image address;
  - a `data:` URI.

  LinkedIn, X and Facebook profile URLs are refused with "copy the image address (or save it and upload)". Those sites have no public way to fetch someone's photo.
- **Gravatar by default.** No `avatar` key plus an `email` means the mint tries the recipient's Gravatar. `avatar: null` opts out.
- **`<AvatarField>`** (`@open-athena/auth/react`) is the form control for all of this:
  - It takes a paste or an upload, or falls back to Gravatar from the email.
  - It previews through `POST /api/auth/avatar`, which is now always on for admins and SSO sessions.
  - It downscales every face to 256 px WebP in the browser (≈3–10 KB), so even Mastodon's 400 KB originals fit the 64 KB inline cap.
  - Its value is a `data:` URI to send as `avatar`; `null` means none.
- **Renames:** `profileUploadMaxBytes` is now `avatarMaxBytes`, and `seedAvatarTimeoutMs` is now `avatarFetchTimeoutMs`. `resolveAvatar` is now `parseAvatarRef` + `fetchAvatar`, or `gate.copyAvatar`. The `avatarLookup` route option is gone, since the preview endpoint is always on. `ProfilePanel`'s avatar `PUT` body is `{ avatar: { ref } }` (formerly `{ url }` / `{ github }`).

## As built on `m3` (2026-10-01)

- Pin → `fa93d14`; the explicit `seedProfile: true` flags are gone (the default now).
- **`allowlist`**: the site's `allowed_emails` is `(email, note, who, ts)`, not the package's `(email, scopes, source, …)` schema, so `site/functions/_lib/allowlist.ts` (`siteAllowlist`) adapts it: a row reads as the base scope plus its `:read` half (a read-only link's recipient counts as covered), `put` writes `note`/`who`/`ts`, `replaceSource` refuses (no `source` column). Passed to `authRoutes` in `api/auth/[[path]].ts`, which also mounts the package's admin `…/allowed` routes. Tests: `_lib/allowlist.test.ts`, incl. a real `POST /api/auth/grants` with `allowlist: true` → `added`, then `already`.
- **Mint form** (`AdminPage.tsx`): a **Sign-in** checkbox once an email is typed (default on); with Read-only it warns the recipient signs in **as a full viewer** (the allowlist has no read-only tier). `<AvatarField>` replaces the Avatar URL box; a 400's `detail` shows in the error; the minted panel reports `allowed.status`. Draft keeps the face unless it's > 32 KB.
- **Read-only allowlist tier** (Ryan, 2026-10-01: "allow email users to be RO too"): `allowed_emails.read_only` (migrations cw `0009` / gcs `0033`, default 0). `scopesFor` admits a `read_only` row at `<base>:read` (an `admin_emails` row or a viewer domain still wins); `siteAllowlist` reads it back as that scope and `put` sets it from the scopes, so a read-only link allowlists read-only and a later full link widens the row. The checkbox is now **Allowlist** ("…sign in themselves — read-only / full viewer, like the link"); `/admin/db/allowed_emails` edits `read_only`.
- **Email = account** (Ryan, 2026-10-01: "if I'm attaching an email to a share link it's as good as making the user an account"): no checkbox; a link minted with an email always sends `allowlist: true`. The Email hint says so, including that the account doesn't expire or get revoked with the link (remove it under users = `/admin/db/allowed_emails`). The minted panel reports "now has an account" / "already had an account, upgraded" / "already has an account".
- **Access** selector replaces the Read-only checkbox: Read-only (default) / Viewer (can stage deletions) / Admin. Admin needs an email and applies to the **account only**: the link mints as a Viewer, then the form adds an `admin_emails` row (`POST /api/db/admin_emails`; 409 = already one), reported beside the link; a forwarded link never carries admin. Without an email Admin is disabled and reads as Viewer. m3 now sets `ADMIN_EMAILS = "1"` (gcs and cw-s3 already did; its `admin_emails` was empty).
- CIC on dev: the checkbox + caveat appear with an email; a GitHub profile and `@Gargron@mastodon.social` preview; a LinkedIn profile gets the readable refusal. A real mint (writes a grant + an allowlist row to the shared D1) not yet run.

## To do here (original list)

- [ ] Bump the pin in `site/package.json` from `7442ab0` to **`fa93d14`** or later.
  - `2f70e25` adds One Tap `prompt` / FedCM for the button.
  - `ef2517a` adds the allowlist flag.
  - `7b665e1` adds avatar copying. Check whether disky uses any of the renamed options above; a grep found no `avatarLookup` / `resolveAvatar` use.
- [ ] `site/src/AdminPage.tsx` mint form:
  - Add an Email field if the form has none.
  - Add the "also allow sign-in" checkbox, default on when an email is entered.
  - Send `email` + `allowlist: true`.
  - Show the returned `allowed.status` next to the new link ("added to the allowlist" / "already allowed").
- [ ] Replace the mint form's "Avatar URL" box with `<AvatarField endpoint="/api/auth/avatar" value={avatar} onChange={setAvatar} email={email} name={name} />`.
  - Send its value as `avatar`.
  - It's a `data:` URI, so drop `avatar` from the `sessionStorage` draft or cap it; a few KB is fine.
  - The hand-rolled `<img className="avatar-preview">` goes away.
  - Surface a 400's `detail` in the mint error, not only the status.
- [ ] Existing grants minted with a pasted URL keep that URL in `subject_json`. There's no in-place re-copy route. Re-mint the ones that matter, or leave them; they only hotlink, nothing breaks except LinkedIn expiry.
- [ ] Make sure the `authRoutes(...)` call passes `allowlist: <the D1 allowlist store>`. It already does if the `/allowed` admin routes work.
- [ ] Drop the now-redundant `seedProfile: true` flags.
- [ ] CIC:
  1. Mint a link for a test address with the box ticked.
  2. Confirm the row appears in the allowlist panel.
  3. Confirm that address can sign in with an emailed code.
  4. Paste a GitHub profile URL and a Mastodon `@user@instance` into the face field; both should preview.
  5. Paste a LinkedIn profile URL and confirm the readable refusal.
  6. Mint, and confirm the grant's `subject.avatar` is a `data:image/webp…` URI rather than a URL.
