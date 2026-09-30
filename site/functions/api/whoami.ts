// GET /api/whoami — the viewer's identity for FE gating and the plain-fetch
// callers (`StagedPage.tsx`), in the package's `SsoWhoami` shape ({ kind,
// email, admin, scopes, subject }): scopes + the admin flag are decided
// server-side (staff domain, viewer domains, the `admin_emails` /
// `allowed_emails` rows). Signed out = 401, which the package hook reads as
// "nobody"; the body still says so.
import { type Ctx, type Env as AuthEnv, identify, json } from "../_lib/auth.js"

export const onRequestGet = async (ctx: Ctx & { env: AuthEnv }): Promise<Response> => {
  const id = await identify(ctx)
  if (!id || !id.email) return json({ email: null, admin: false }, 401)
  return json({ kind: 'sso', email: id.email, admin: id.admin, scopes: id.scopes, subject: id.subject })
}
