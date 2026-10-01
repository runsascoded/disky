import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { AuthGate as Gate, deniedEmail, RequestAccessForm, SignInPanel, useForgetWhoami } from '@open-athena/auth/react'
import { AUTH_MODE, devIdentity, WHOAMI_SOURCE } from './auth'
import { handoffUrl, inApp } from './appLink'
import { DEFAULT_STORE } from './stores'

/** The public Google client id, or null where Google sign-in isn't configured
 *  (the wall then offers only the emailed code). One fetch per page load. */
function useGoogleClient() {
  return useQuery({
    queryKey: ['google-client'],
    queryFn: async (): Promise<string | null> => {
      const r = await fetch('/auth/google/client', { credentials: 'include' })
      if (!r.ok) return null
      return ((await r.json()) as { clientId?: string | null }).clientId ?? null
    },
    staleTime: Infinity,
    retry: false,
    enabled: AUTH_MODE === 'app',
  })
}

// Gate the human-facing routes on an identity: the app session (our own Google
// OIDC or an emailed code, minted at `/auth/google` / `/auth/email/*`, or a
// `?key=` share link, which <Gate> redeems before probing). The static shell +
// og:image stay publicly crawlable for link unfurls either way — crawlers read
// the og: meta from <head> regardless of which body we render.
export function AuthGate({ children }: { children: ReactNode }) {
  // Public deploys (r2.rbw.sh, per-project embeds): no gate — render for an
  // anonymous viewer (server grants the base scope via PUBLIC_READ).
  if (AUTH_MODE === 'public') return <>{children}</>
  return (
    <Gate source={WHOAMI_SOURCE} devIdentity={devIdentity()} signIn={<LoginWall />}>
      {children}
    </Gate>
  )
}

// The wall: Google-first (one button, no typing), with an emailed-code fallback
// for the non-Google tail. The Google button is Google's own in-page one
// (`oneTap`): personalized to the account the browser is signed into, it signs
// in with one click and no round-trip through the account chooser; where
// Google's script can't load it degrades to the redirect flow (`/auth/google`).
// The request-access form + how-to prose fold behind a disclosure — they're the
// tail for people the policy doesn't yet admit (not staff, not a viewer
// domain, no `allowed_emails` row), not the wall itself — and unfold on a
// `?denied=<email>` bounce (the redirect flow's, or one the in-page button
// sets), which also pre-fills the form with the provider-verified address. All
// paths converge on the same app session. See specs/done/oidc-cutover-cw.md.
//
// Inside <Gate> the wall stands in for the page, so signing in just refetches
// whoami (`onSignedIn={forget}`) and Google returns to the current URL. On the
// standalone `/signin` page (`next` given — the inline "sign in" links from a
// guest / expired session land there) both paths return to `next` instead.
function LoginWall({ next, error }: { next?: string; error?: string }) {
  const forget = useForgetWhoami()
  const navigate = useNavigate()
  const location = useLocation()
  const denied = deniedEmail()
  const { wall } = DEFAULT_STORE
  const google = useGoogleClient()
  const clientId = google.data ?? null
  const googleUrl = next ? `/auth/google?next=${encodeURIComponent(next)}` : '/auth/google'
  // Inside the macOS app Google refuses OAuth (embedded webview), and its
  // in-page button's popup goes nowhere: send Google sign-in to the default
  // browser, which hands the session back via "Open in disky" (`?open-in-disky`,
  // fired unprompted once signed in there). The emailed code works in-app.
  const app = inApp(navigator, window)
  // A verified-but-not-allowed address from the in-page button: land on the
  // same `?denied=` the redirect flow bounces to, so the wall unfolds
  // request-access pre-filled with it.
  const onDenied = (email: string) => {
    const sp = new URLSearchParams(location.search)
    sp.set('denied', email)
    navigate({ search: `?${sp}` }, { replace: true })
  }
  return (
    <div className="authwall">
      <div className="card">
        <h1>{DEFAULT_STORE.title}</h1>
        <p>{DEFAULT_STORE.desc}</p>
        <p className="restrict">{wall.restrict}</p>
        {error && <p className="signin-error" role="alert">{error}</p>}
        {/* Wait for the client id (one small request) rather than flash the
            redirect button and swap it for Google's. */}
        {app && clientId && (
          <>
            <a className="signin signin-browser" href={handoffUrl(window.location)} target="_blank" rel="noopener">
              Continue with Google in your browser
            </a>
            <p className="signin-how">Signs you in there, then hands the session back to disky.</p>
          </>
        )}
        {google.isFetched && (
          <SignInPanel
            title={null}   // the card's own <h1> + `restrict` line already say what this is
            googleUrl={clientId && !app ? googleUrl : undefined}
            withNext={!next}
            oneTap={clientId && !app ? {
              clientId,
              nonceEndpoint: '/auth/google/onetap/nonce',
              verifyEndpoint: '/auth/google/onetap',
              onDenied,
              className: 'signin-onetap',
              // GSI takes a fixed pixel width (≤ 400): fill the card's inner
              // width (440 max − 2×32 padding), less on a narrow phone.
              buttonOptions: { theme: 'filled_blue', size: 'large', text: 'continue_with', shape: 'rectangular', width: Math.min(376, Math.max(240, window.innerWidth - 112)) },
            } : undefined}
            emailAuth={{ startEndpoint: '/auth/email/start', verifyEndpoint: '/auth/email/code' }}
            onSignedIn={() => { forget(); if (next) navigate(next, { replace: true }) }}
            classNames={{ root: 'signin-panel', googleButton: 'signin', divider: 'signin-or' }}
          />
        )}
        <details className="signin-more" open={Boolean(denied)}>
          <summary>{denied ? `${denied} isn't on the list yet — request access` : "Don't have access?"}</summary>
          <div className="signin-panel">
            <RequestAccessForm defaultEmail={denied} />
          </div>
          {wall.how && <p className="signin-how">{wall.how}</p>}
        </details>
      </div>
    </div>
  )
}

/** `?error=` on `/signin` → what to tell the person. The Functions send the
 *  adapter's reason (`google:<why>`) or `link` for a dead emailed link. */
function signInError(code: string | null): string | undefined {
  if (!code) return undefined
  if (code === 'link') return 'That sign-in link is no longer valid — it may have expired or already been used. Ask for a fresh code below.'
  if (code.startsWith('app-link:')) {
    return code === 'app-link:not-allowed'
      ? "This address isn't on the list for this site."
      : 'That disky sign-in link is no longer valid — it expires after a minute and works once. Click "Open in disky" again in your browser.'
  }
  if (code.startsWith('google:')) {
    const why = code.slice('google:'.length)
    const replay = /state|nonce/.test(why)
    return replay
      ? "Google sign-in didn't complete — the attempt expired or the page was reloaded mid-way. Try again."
      : `Google sign-in didn't complete (${why}). Try again, or use an emailed code.`
  }
  return 'Sign-in didn\'t complete. Try again.'
}

/** `/signin?next=<path>` — the wall as a page, for the inline "sign in" links
 *  (`signInUrl()`): a guest upgrading to a real identity, or a session that
 *  expired mid-page — and where the sign-in Functions land a failure
 *  (`?error=`). Same-origin paths only; anything else goes home. */
export function SignInPage() {
  const params = new URLSearchParams(useLocation().search)
  const raw = params.get('next') ?? '/'
  const next = raw.startsWith('/') && !raw.startsWith('//') ? raw : '/'
  return <LoginWall next={next} error={signInError(params.get('error'))} />
}
