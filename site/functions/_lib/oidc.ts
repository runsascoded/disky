/**
 * Shared config for the OIDC sign-in Functions (`/auth/google` + its callback),
 * the ZT-free replacement for the CF Access edge gate. Both the start and
 * callback handlers must be given the *same* `redirectUri`, so it's derived
 * once here from the request origin — which makes prod (cw-s3.oa.dev), the
 * dev stack (the Pages preview), and localhost all work, provided each origin's
 * callback is registered on the Google client. See specs/oidc-cutover-cw.md.
 */
import { type Env, gateFor } from './auth.js'

export interface OidcCfg {
  gate: NonNullable<ReturnType<typeof gateFor>>
  clientId: string
  clientSecret: string
  redirectUri: string
  /** First sign-in seeds the profile (name + Google `picture`, inlined) so the
   *  chip shows a face by default, not an initial. Never overrides a self-set one. */
  seedProfile: true
}

/** The adapter config, or null if the deployment isn't OIDC-configured
 *  (no gate — DB/SESSION_SECRET — or no Google client secrets). */
export function oidcConfig(env: Env, request: Request): OidcCfg | null {
  const gate = gateFor(env)
  if (!gate || !env.GOOGLE_CLIENT_ID || !env.GOOGLE_CLIENT_SECRET) return null
  return {
    gate,
    clientId: env.GOOGLE_CLIENT_ID,
    clientSecret: env.GOOGLE_CLIENT_SECRET,
    redirectUri: `${new URL(request.url).origin}/auth/google/callback`,
    seedProfile: true,
  }
}
