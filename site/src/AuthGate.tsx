import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { AuthGate as Gate, deniedEmail, RequestAccessForm, SignInPanel, useForgetWhoami } from '@open-athena/auth/react'
import { AUTH_MODE, devIdentity, WHOAMI_SOURCE } from './auth'
import { signInError } from './signInError'
import { DEFAULT_STORE, storeEmailAuth } from './stores'

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

/** Whether "Sign in with OA" (OA's broker, specs/oa-login-rp.md) is configured here: the wall then offers it first. */
function useOaLogin() {
  return useQuery({
    queryKey: ['oa-login'],
    queryFn: async (): Promise<boolean> => {
      const r = await fetch('/auth/oa/client', { credentials: 'include' })
      if (!r.ok) return false
      return ((await r.json()) as { enabled?: boolean }).enabled === true
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
// for the non-Google tail. A first visit gets Google's own in-page button
// (`oneTap`); with several Google accounts in the browser it can still open
// Google's account chooser. A returning visitor (the `oa_google_hint[_accounts]`
// cookies Google sign-ins leave, up to five, surviving sign-out) gets a row per
// remembered account instead (`RememberedAccount`): the redirect flow
// (`/auth/google?account=<it>`) with `login_hint`, which skips the chooser, plus
// "Use another Google account" for it. Google's prompt with auto-select signs a
// returning visitor in with no click at all, once their account has granted
// this client through the button or prompt. Where Google's script can't load,
// the button degrades to the plain redirect.
// With OA's broker configured, the middleware has already tried it silently by the time
// this shows (`functions/_lib/silentSignIn.ts`): the wall is what's left when that fails.
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
  const oa = useOaLogin()
  // Inside <Gate> (no `next`), back to the page the wall stands in for.
  const oaUrl = `/auth/oa?next=${encodeURIComponent(next ?? window.location.pathname + window.location.search)}`
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
        {oa.data && (
          <a className="signin signin-oa" href={oaUrl}>Sign in with OA</a>
        )}
        {google.isFetched && (
          <SignInPanel
            title={null}   // the card's own <h1> + `restrict` line already say what this is
            googleUrl={clientId ? googleUrl : undefined}
            withNext={!next}
            continueAs={email => <RememberedAccount email={email} />}
            oneTap={clientId ? {
              clientId,
              nonceEndpoint: '/auth/google/onetap/nonce',
              verifyEndpoint: '/auth/google/onetap',
              onDenied,
              prompt: { autoSelect: true },
              className: 'signin-onetap',
              // GSI takes a fixed pixel width (≤ 400): fill the card's inner
              // width (440 max − 2×32 padding), less on a narrow phone.
              buttonOptions: { theme: 'filled_blue', size: 'large', text: 'continue_with', shape: 'rectangular', width: Math.min(376, Math.max(240, window.innerWidth - 112)) },
            } : undefined}
            emailAuth={storeEmailAuth(DEFAULT_STORE)}
            onSignedIn={() => { forget(); if (next) navigate(next, { replace: true }) }}
            classNames={{ root: 'signin-panel', googleButton: 'signin', switchAccount: 'signin-switch', divider: 'signin-or' }}
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

/** One remembered Google account (the `oa_google_hint_accounts` cookie's, newest first): Google's mark, the address,
 *  "Last used" on the newest only (CSS), an arrow. A hint, never an identity: the click still signs in at Google. */
function RememberedAccount({ email }: { email: string }) {
  return (
    <span className="remembered-account">
      <svg viewBox="0 0 48 48" width="18" height="18" aria-label="Google" role="img">
        <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
        <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
        <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
        <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
      </svg>
      <span className="account-email">{email}</span>
      <span className="account-last-used">Last used</span>
      <span className="account-arrow" aria-hidden="true">→</span>
    </span>
  )
}

/** `/signin?next=<path>` — the wall as a page, for the inline "sign in" links
 *  (`signInUrl()`): a guest upgrading to a real identity, or a session that
 *  expired mid-page — and where the sign-in Functions land a failure
 *  (`?error=`). Same-origin paths only; anything else goes home. */
export function SignInPage() {
  const { search } = useLocation()
  const raw = new URLSearchParams(search).get('next') ?? '/'
  const next = raw.startsWith('/') && !raw.startsWith('//') ? raw : '/'
  return <LoginWall next={next} error={signInError(search)} />
}
