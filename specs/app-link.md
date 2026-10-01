# App sign-in hand-off (`/api/app-link`) — as built

The site half of `tauri-native-app.md` § "Sign-in inside the window". Google refuses OAuth in the app's WKWebView, so the person signs in to the site in the system browser, and the browser hands the app a one-time credential over the `disky://` scheme. This file is the contract the app session builds against.

## Flow

1. Signed-in browser (desktop Mac): user menu → **Open in disky**.
2. The page calls `POST /api/app-link` with `{ "next": "<current path+query>" }` and gets back `{ url }`.
3. The page navigates to `disky://open?link=<encodeURIComponent(url)>`.
4. **The app** (not built here): on a `disky://open` deep link, take the `link` query param, check it is an `https://` URL on the site's own origin, and load it in the main webview. Nothing else.
5. The webview's `GET` of that URL sets the session cookie (`oa_auth`, HttpOnly) and `303`s to `next` (default `/`). The window is now signed in as the same person.

If no app is registered for `disky://`, the navigation silently does nothing; the page shows "Nothing happened? Install the disky app." if it still has focus 1.5 s later.

## `POST /api/app-link`

- **Caller**: an email session (`Identity.via === 'session'`, cookie-authenticated). Refused:
  - no identity → `401 {"error":"unauthenticated"}`
  - a grant identity (share link, agent token, `?key=`/Bearer) → `403 {"error":"only a signed-in session can open the app"}` — a grant can't launder itself into a fresh grant.
- **Same-origin only**: `Origin` must be present and equal the request's origin, and `Sec-Fetch-Site`, when sent, must be `same-origin`; otherwise `403 {"error":"cross-site request refused"}` (checked before identity). Any method but `POST` → `405`. So neither a link, an `<img>`, nor another site's form can mint one.
- **Request body** (optional, `application/json`): `{ "next": "/path?query" }`. A same-origin path only (`/…`, not `//…`, no `\`, ≤ 2048 chars); anything else becomes `/`.
- **Response** `200`, `cache-control: no-store`:
  ```json
  { "url": "https://<site>/auth/app-link?token=<token>[&next=<path>]", "expires_at": 1790000000 }
  ```
- **The grant minted** (`@open-athena/auth`, D1 `grants`): `name: "disky app sign-in"`, `email` = the caller's (lower-cased), `created_by` = the same email, `scopes` = exactly the caller's scopes as the gate resolved them for this request (never more — `admin` only if the caller has it), `max_redeems: 1`, `expires_at: now + 60`.
- **Audit**: the gate's own `mint` row, plus an `access_log` row with `event = 'app-link'`, `reason = 'mint'`, `session_sub = e:<email>`, `grant_id`. The token is never logged or stored (only its SHA-256 hash, in `grants.token_hash`).

## `GET /auth/app-link?token=<token>[&next=<path>]`

- Looks the token up first; only a grant with the self-mint shape (name above, `email` set, `created_by == email`, `max_redeems == 1`, lifetime ≤ 60 s) qualifies. Anything else — e.g. an admin's share link pasted here — is refused **without spending** one of its redemptions.
- Spends the one redemption via the store's atomic compare-and-swap (`UPDATE grants SET redeems = redeems + 1 … WHERE redeems < max_redeems AND expires_at > now AND revoked_at IS NULL … RETURNING`), so two concurrent loads can't both win.
- Revokes the grant immediately (the gate otherwise accepts an unexpired grant's raw token as a request-scoped `?key=`/Bearer credential regardless of redemptions — revoking ends that).
- Signs the webview in with `gate.signIn(email)`: an **ordinary email session**, identical to a Google/email-code sign-in (30-day cookie; scopes re-derived from the policy on every request, so a later delisting bites). Not a grant session — that would render as a "guest share link", carry a scope snapshot, and die with the grant.
- Success: `303 Location: <next>`, `Set-Cookie: oa_auth=…`, `cache-control: no-store`, `referrer-policy: no-referrer`. Audit: gate rows `redeem`, `revoke`, `signin`, plus `event = 'app-link'`, `reason = 'redeem'`.
- Failure: `303 Location: /signin?error=app-link:<reason>`, no cookie. `<reason>`: `bad-token` (unknown / not an app link / missing), `revoked` (already used — the second load), `expired`, `exhausted`, `not-allowed` (the address was delisted between mint and redeem). The sign-in page explains it ("click Open in disky again").

## Why 60 s

The link has to survive: the browser's "Open disky?" confirmation (a human click — the dominant term), the app's cold launch, and one webview navigation. A minute covers that with margin; a slower user just clicks the button again. Single-use + immediate revoke bound the exposure regardless.

## Which browsers show the button

`offerAppLink` (`site/src/appLink.ts`): `navigator.platform` starts with `Mac` (or the UA has `Macintosh`), `maxTouchPoints ≤ 1` (excludes iPadOS, which reports a Mac UA), and **not** inside the app itself — recognized by a `disky/<version>` token in the UA or Tauri's `__TAURI_INTERNALS__` / `__TAURI__` globals. **App side:** append `disky/<version>` to the webview's user agent so the button hides there. Guest (grant) sessions never see the button.

## Signing in from inside the app

Google's in-page button opens a popup, which WKWebView drops (a silent no-op), and Google refuses OAuth in embedded webviews anyway. So the wall, inside the app (`inApp`: the `disky/` UA token or Tauri globals), replaces Google with **"Continue with Google in your browser"**: a `target=_blank` link to the same page plus `?open-in-disky=1`. The app routes every new-window request to the default browser (`on_new_window` → `open`). There the person signs in as usual, and `UserMenu` sees the param once a non-guest session exists, strips it, and fires "Open in disky" unprompted. The emailed code still works in-app.

The app accepts a link from either known site (prod or dev), not just the configured one. A link from the other one switches the app's site setting (logged to `disky.log`). An explicit `DISKY_URL` pins the site. Before this, clicking "Open in disky" on dev with the app set to prod was refused silently: the app activated with no window.

## Residual risks

- Before it is redeemed, the token is a 60 s credential for the caller: anyone who reads it off the `disky://` URL first (another app registered for the scheme, a process watching URL opens) could redeem it. The app should be the sole `disky://` handler; the short TTL and single use bound this.
- Login CSRF, as with any magic link: someone could get a victim's browser to load *their own* link, signing the victim in as them. Same exposure as the existing `?key=` share links, narrowed by the 60 s TTL.

## Files

- `site/functions/_lib/appLink.ts` — mint / redeem / origin check / TTL; `site/functions/_lib/appLink.test.ts` — 12 specs over a sqlite D1 twin
- `site/functions/api/app-link.ts`, `site/functions/auth/app-link.ts` — the routes
- `site/src/appLink.ts` (+ `.test.ts`) — `offerAppLink`, `openInApp`; `site/src/SiteNav.tsx` — the menu item + fallback hint; `site/src/AuthGate.tsx` — the `app-link:*` sign-in errors
