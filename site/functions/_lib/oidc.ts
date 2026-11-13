/**
 * Shared config for the OIDC sign-in Functions (`/auth/google` + its callback),
 * the ZT-free replacement for the CF Access edge gate. Both the start and
 * callback handlers must be given the *same* `redirectUri`, so it's derived
 * once here from the request origin — which makes prod (cw-s3.oa.dev), the
 * dev stack (the Pages preview), and localhost all work, provided each origin's
 * callback is registered on the Google client. See specs/done/oidc-cutover-cw.md.
 *
 * Cookie names come from `cookieNames` (`__Host-` under `AUTH_HOST_COOKIES`), the same on the start and the callback.
 */
import { type OaLoginCfg, oaLoginOptions, type OidcCfg } from '@disk-tree/pages-kit/oa'
import type { OidcOptions } from '@open-athena/auth/oidc'
import { cookieNames, type Env, gateFor, isPreviewHost } from './auth.js'

export type GoogleCfg = OidcCfg & Required<Pick<OidcOptions, 'nonceCookieName' | 'accountHintCookie'>>

/** The adapter config, or null if the deployment isn't OIDC-configured
 *  (no gate — DB/SESSION_SECRET — or no Google client secrets). */
export function oidcConfig(env: Env, request: Request): GoogleCfg | null {
  const gate = gateFor(env, request)
  if (!gate || !env.GOOGLE_CLIENT_ID || !env.GOOGLE_CLIENT_SECRET) return null
  const names = cookieNames(env, request)
  return {
    gate,
    clientId: env.GOOGLE_CLIENT_ID,
    clientSecret: env.GOOGLE_CLIENT_SECRET,
    redirectUri: `${new URL(request.url).origin}/auth/google/callback`,
    nonceCookieName: names.googleNonce,
    accountHintCookie: names.accountHint,
  }
}

/** The adapter config for "Sign in with OA" (`/auth/oa` + its callback, specs/oa-login-rp.md): OA's broker
 *  (`OA_LOGIN_ISSUER`, e.g. https://login.oa.dev) as the OIDC provider, or null where it isn't configured (no gate, or
 *  `OA_LOGIN_ISSUER` or the host's client unset). A preview host (`isPreviewHost`) uses the preview client
 *  (`OA_LOGIN_PREVIEW_CLIENT_ID` / `_SECRET`) and a redirect on its own origin, or nothing: never the deployment's own
 *  client. Shared with `fleet/` (`@disk-tree/pages-kit/oa`). */
export function oaLoginConfig(env: Env, request: Request): OaLoginCfg | null {
  const names = cookieNames(env, request)
  return oaLoginOptions(gateFor(env, request), env, request, {
    preview: isPreviewHost(env, request), nonceCookieName: names.oaNonce, accountHintCookie: names.oaHint,
  })
}
