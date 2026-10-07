// Identity plumbing (@open-athena/auth): where whoami comes from, per host.
//
// In the app tier the host is public shell + app-gated data — identity is the
// app session (`/api/auth/whoami`), minted by our own Google OIDC client
// (`/auth/google`), an emailed code (`/auth/email/*`), or by redeeming a
// `?key=` share link (specs/done/oidc-cutover-cw.md).
import { displayName, useForgetWhoami, useWhoami, type Whoami, type WhoamiSource } from '@open-athena/auth/react'
import { DEFAULT_STORE } from './stores'

// Deployment seam (specs/done/denovo-factor.md): a build-time flag from
// `wrangler.toml` `[vars]` `AUTH_MODE` (vite.config.ts). `app` = the app
// session (`/api/auth/whoami`, minted at `/signin`); `public` = no gate
// (r2.rbw.sh, per-project embeds): the app renders for an anonymous viewer,
// no whoami fetch or login wall (specs/federated-scans.md).
export const AUTH_MODE: 'app' | 'public' = import.meta.env.VITE_AUTH_MODE === 'public' ? 'public' : 'app'
// The package's default whoami source (`/api/auth/whoami`, the app session).
export const WHOAMI_SOURCE: WhoamiSource | undefined = undefined

// `?wall` forces the wall in dev (which otherwise short-circuits to authed,
// since neither identity source exists locally). A local session disables the
// stub entirely — dev then exercises the real whoami/scopes path, including
// guest grants. The real `oa_auth` cookie is HttpOnly, so a sign-in minted by
// the local Functions (Google / email-code) is announced by the JS-visible
// `oa_dev_session` marker (functions/_lib/devsession.ts); a cookie forged
// against the local wrangler's SESSION_SECRET via document.cookie also counts.
// Evaluated per render (not once at load) so an in-page code sign-in flips
// the gate without a reload. The stub carries every scope, matching what the
// Functions grant a localhost request (`DEV_SCOPES` in functions/_lib/auth.ts).
const DEV_WHOAMI: Whoami = {
  kind: 'sso',
  email: import.meta.env.VITE_DEV_EMAIL ?? 'dev@example.test',
  admin: true,
  scopes: [DEFAULT_STORE.key, 'admin', 'requests'],
  subject: null,
}
const forceWall = new URLSearchParams(window.location.search).has('wall')
const hasLocalSession = (): boolean =>
  document.cookie.includes('oa_auth=') || document.cookie.includes('oa_dev_session=')
export const devIdentity = (): Whoami | null | undefined =>
  import.meta.env.DEV && !hasLocalSession() ? (forceWall ? null : DEV_WHOAMI) : undefined

/** Where the inline "sign in" links go: the `/signin` page (Google / emailed
 *  code), returning here after. */
export const signInUrl = (): string => `/signin?next=${encodeURIComponent(window.location.pathname + window.location.search)}`

export interface Ident {
  email: string
  name?: string
  /** A share-link (grant) session, not SSO — the chip shows the grant's own
   *  subject (name + avatar) rather than the owner-registry lookup. */
  guest?: boolean
  /** The grant subject's explicit avatar URL (Slack/GitHub/…), when set. */
  avatar?: string
}

/** Sign out of the app session (POST /api/auth/logout clears the cookie). */
export function useSignOut(): () => void {
  const forget = useForgetWhoami()
  return () => {
    void fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).then(() => {
      forget()
    })
  }
}

/** The header chip's identity: null until (unless) someone is signed in. */
export function useIdent(): Ident | null {
  const { whoami } = useWhoami(WHOAMI_SOURCE, { devIdentity: devIdentity() })
  if (!whoami) return null
  const name = displayName(whoami) ?? undefined
  const email = whoami.email ?? name ?? 'guest'
  return { email, name, guest: whoami.kind === 'grant', avatar: whoami.subject?.avatar ?? undefined }
}

/** Scopes on the current identity, or null when nobody is signed in. */
function useScopes(): string[] | null {
  const { whoami } = useWhoami(WHOAMI_SOURCE, { devIdentity: devIdentity() })
  return whoami?.scopes ?? null
}

const hasBase = (scopes: string[]): boolean => scopes.includes(DEFAULT_STORE.key) || scopes.includes('*')

/**
 * Who may assign owners: the assign scope (`gcs:assign`, every signed-in
 * viewer) or an admin; a share link can't. The server enforces the same
 * (`POST /api/actions` is `requireAssigner`).
 */
export function useCanAssign(): boolean {
  const scopes = useScopes()
  return scopes !== null && [`${DEFAULT_STORE.key}:assign`, 'admin', '*'].some(s => scopes.includes(s))
}

/**
 * Staging (the opt-in trash proposal) needs the full base scope; a read-only
 * guest link cannot. The server enforces the same via `requireStager`.
 */
export function useCanStage(): boolean {
  const scopes = useScopes()
  return scopes !== null && hasBase(scopes)
}
