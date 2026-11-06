/** `?error=` on `/signin` → what to tell the person. The Functions send the adapter's reason (`google:<why>`, `oa:<why>`)
 *  or `link` for a dead emailed link. `?sso=` is the silent sign-in's marker (specs/oa-login-rp.md phase 2), never an
 *  error: nobody asked for that attempt, and the wall's buttons are the answer. */
export function signInError(search: string): string | undefined {
  const code = new URLSearchParams(search).get('error')
  if (!code) return undefined
  if (code === 'link') return 'That sign-in link is no longer valid — it may have expired or already been used. Ask for a fresh code below.'
  if (code.startsWith('google:')) {
    const why = code.slice('google:'.length)
    const replay = /state|nonce/.test(why)
    return replay
      ? "Google sign-in didn't complete — the attempt expired or the page was reloaded mid-way. Try again."
      : `Google sign-in didn't complete (${why}). Try again, or use an emailed code.`
  }
  if (code.startsWith('oa:')) {
    const why = code.slice('oa:'.length)
    if (why === 'login_required') return 'Sign in with OA needs you to sign in at OA first. Click "Sign in with OA" to continue.'
    if (why === 'access_denied') return 'OA sign-in was cancelled or refused. It admits openathena.ai accounts only; others can use an emailed code.'
    return /state|nonce/.test(why)
      ? "OA sign-in didn't complete — the attempt expired or the page was reloaded mid-way. Try again."
      : `OA sign-in didn't complete (${why}). Try again, or use another way below.`
  }
  return 'Sign-in didn\'t complete. Try again.'
}
