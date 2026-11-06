import { describe, expect, it } from 'vitest'
import { signInError } from './signInError'

describe('what `/signin` tells the person', () => {
  it('the silent sign-in\'s marker is no error: `?sso=` says nothing, whatever the broker answered', () => {
    expect(['?sso=login_required', '?sso=interaction_required', '?sso=access_denied', '?sso=error', '?next=%2Fscans&sso=login_required', ''].map(signInError))
      .toEqual([undefined, undefined, undefined, undefined, undefined, undefined])
  })
  it('`?error=` still explains itself', () => {
    expect(['?error=oa:login_required', '?error=oa:access_denied', '?error=oa:bad%20state', '?error=link'].map(signInError)).toEqual([
      'Sign in with OA needs you to sign in at OA first. Click "Sign in with OA" to continue.',
      'OA sign-in was cancelled or refused. It admits openathena.ai accounts only; others can use an emailed code.',
      "OA sign-in didn't complete — the attempt expired or the page was reloaded mid-way. Try again.",
      'That sign-in link is no longer valid — it may have expired or already been used. Ask for a fresh code below.',
    ])
  })
})
