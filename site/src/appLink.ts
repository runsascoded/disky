// "Open in disky": hand this browser's sign-in to the macOS app
// (specs/app-link.md). Google refuses OAuth inside the app's WKWebView, so the
// app can't sign in on its own; the signed-in browser mints a single-use link
// (`POST /api/app-link`) and passes it over the app's URL scheme.

export const APP_SCHEME_URL = (link: string): string => `disky://open?link=${encodeURIComponent(link)}`

interface Nav { userAgent: string; platform?: string; maxTouchPoints?: number }

/**
 * Offer the button only in a desktop Mac browser — where the app can exist —
 * and not inside the app's own window. `platform` is deprecated but still
 * `MacIntel` on every Mac browser; the UA's `Macintosh` covers its absence.
 * iPadOS Safari reports a Mac UA too, so touch points rule it out. The app's
 * window is recognized by a `disky/<version>` UA token (the app appends one)
 * or the Tauri globals its webview injects.
 */
export function offerAppLink(nav: Nav, win: object = {}): boolean {
  const mac = (nav.platform ?? '').startsWith('Mac') || /Macintosh/.test(nav.userAgent)
  const touch = (nav.maxTouchPoints ?? 0) > 1
  const inApp = /\bdisky\//.test(nav.userAgent) || '__TAURI_INTERNALS__' in win || '__TAURI__' in win
  return mac && !touch && !inApp
}

/** Mint a link for the current page, then hand it to the app. Resolves once the
 *  navigation is attempted; rejects with the server's error. */
export async function openInApp(): Promise<void> {
  const res = await fetch('/api/app-link', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ next: window.location.pathname + window.location.search }),
  })
  const body = await res.json().catch(() => ({})) as { url?: string; error?: string }
  if (!res.ok || !body.url) throw new Error(body.error ?? `HTTP ${res.status}`)
  window.location.href = APP_SCHEME_URL(body.url)
}
