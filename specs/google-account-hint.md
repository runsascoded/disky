# Skip Google's account chooser for returning visitors

From the auth session (`~/c/oa/auth`), 2026-10-07.

## Problem

With several Google accounts in one browser (e.g. an OA and a personal account), signing in to disky takes at least two clicks and page loads. Clicking "Continue as Ryan (OA)" on Google's rendered button still opens Google's account chooser. The comment above `LoginWall` in `site/src/AuthGate.tsx` claims the button "signs in with one click and no round-trip through the account chooser". That's only true for an account that already granted this client through the button or prompt, and only when the browser has just one Google session.

## What auth now does (dist `996f05d`, auth `b418767`)

- **Remembered account:** the Google callback and One Tap verify set a long-lived `oa_google_hint` cookie (400 days, HttpOnly) holding the address that signed in. It survives sign-out on purpose, and a denied sign-in clears it.
- **Hinted redirect:** `oidcStart` sends the remembered address to Google as `login_hint`. Google then skips its chooser. Verified on the auth demo with an OA and a personal account both signed in: after a one-time "signing back in" confirmation, the redirect returned signed in with no Google page at all.
- **Escape hatch:** `?account=choose` on the start URL drops the hint and sends `prompt=select_account`.
- **Nonce endpoint:** `googleOneTapNonce` returns the remembered address as `loginHint`, but only when the handler is called with `{ request }`.
- **`SignInPanel`:** once it learns the hint, it replaces Google's button with a "Continue as <address>" link to `googleUrl`, which is the hinted redirect. Google's button stays mounted but hidden, so its prompt still works. Below it, a "Not <address>? Use another Google account" link goes to `googleUrl` with `account=choose`. Both are overridable: `continueAs` and `switchAccount` take a function or `false`, and `classNames.switchAccount` styles the second link.
- **`GoogleOneTap`:** passes `hd` and `loginHint` through to GSI. disky admits viewer domains beyond `openathena.ai`, so `hd` doesn't fit here.

## Changes in disky

1. Bump `@open-athena/auth` to `996f05d` (`pds gh auth`, or the SHA directly). This is the same build m3's log-client-fields work needs, so migration `0002_access_log_location.sql` must be applied before deploying (see auth's `specs/done/log-client-fields.md`).
2. `site/functions/auth/google/onetap/nonce.ts`: pass the request, `googleOneTapNonce({ gate })({ request: ctx.request })`. Without it the panel never learns the hint, so the "Continue as" button and the "Not you?" link never appear. The redirect still hints.
3. `site/src/AuthGate.tsx`:
   - Add `switchAccount: '<class>'` to the `SignInPanel` `classNames`, and style it as a small muted, centered link under the button.
   - The "Continue as" link uses `classNames.googleButton` (`signin`), so it already looks like the redirect button.
4. Turn on Google's prompt with auto-select. In the `oneTap` props, add `prompt: { autoSelect: true }`. Once an account has granted this client through GSI, a returning visitor is signed in with no click at all. It's an overlay, which suits a wall that exists only to sign people in.
5. Correct the comment above `LoginWall`:
   - Google's button can still ask which account to use.
   - The one-click path for a returning visitor is the remembered-account "Continue as" redirect.
   - The prompt with auto-select gets them in with no click.
6. Check the macOS app path (`inApp`): there `oneTap` is undefined, so the panel never learns the hint and shows nothing new. The handoff page's redirect is hinted server-side regardless.

## Verify

- Sign in with Google once (either path), sign out, and reload the wall. It should show "Continue as <you>" and "Not <you>? …".
- Click "Continue as". You should land signed in with no Google page.
- Click "Not you?". Google's chooser should appear.
- Sign in as an account the policy refuses. On the next visit, the wall shows Google's button again, with no hint.
