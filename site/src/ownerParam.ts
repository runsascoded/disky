import type { Param } from 'use-prms'

/**
 * `?o=` (the owner axis), golfed: the pools are its shortest forms — bare `?o`
 * is the unowned pool ("owner: nobody"), `?o=*` the owned one ("anyone") —
 * while people (`me`, a user key, `!a,b`) stay as written. The app keeps the
 * long names (`'unowned'` / `'owned'`) as values; this maps them at the URL.
 * The long names and the pre-2026-09-07 `claimed` / `unclaimed` still decode,
 * and `legacyOwner` says how to rewrite them in place.
 */
export const ownerParam: Param<string | undefined> = {
  encode: v => v === 'unowned' ? '' : v === 'owned' ? '*' : v,
  decode: e => e === undefined ? undefined : normPool(e) ?? e,
}

const normPool = (e: string): 'owned' | 'unowned' | undefined =>
  e === '' || e === 'unowned' || e === 'unclaimed' ? 'unowned'
  : e === '*' || e === 'owned' || e === 'claimed' ? 'owned'
  : undefined

/** A pool spelled the long or the retired way → its canonical URL value
 *  (`''` / `'*'`); null when `raw` is absent or already canonical. */
export function legacyOwner(raw: string | null): '' | '*' | null {
  if (raw === null || raw === '' || raw === '*') return null
  const p = normPool(raw)
  return p === 'unowned' ? '' : p === 'owned' ? '*' : null
}

/** A query string with `key=` written as the bare `key` (what use-prms emits
 *  for an empty value), so a rewritten URL matches what the app writes. */
export const bareEmpty = (qs: string, key: string): string =>
  qs.split('&').map(kv => (kv === `${key}=` ? key : kv)).join('&')
