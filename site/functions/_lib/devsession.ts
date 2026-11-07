/**
 * Local-dev sign-in marker. The app session cookie (`oa_auth`) is HttpOnly, so
 * the SPA's localhost identity stub (`devIdentity` in src/auth.ts) can't tell
 * a real sign-in happened and would keep stubbing — `?wall` would show the
 * wall again right after a successful Google / email-code sign-in. In local
 * dev, mirror a session-minting response with a JS-visible `oa_dev_session=1`
 * cookie; the stub yields to the real whoami probe when it sees it. Gated on
 * `DEV_EMAIL`, which only `.dev.vars` sets (never a Pages secret), rather than
 * the hostname: the dev stack is also reached over the tailnet (`m3:<port>`).
 */
export const DEV_SESSION_COOKIE = 'oa_dev_session'

/** If `res` sets the app session cookie and this is a dev stack, add the
 *  marker cookie alongside it. Otherwise `res` is returned untouched. */
export const markDevSession = (res: Response, env: { DEV_EMAIL?: string }): Response => {
  if (!env.DEV_EMAIL) return res
  const setsSession = res.headers.getSetCookie().some(c => c.startsWith('oa_auth='))
  if (!setsSession) return res
  const out = new Response(res.body, res)
  out.headers.append('set-cookie', `${DEV_SESSION_COOKIE}=1; Path=/; SameSite=Lax`)
  return out
}
