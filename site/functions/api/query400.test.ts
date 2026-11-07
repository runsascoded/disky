import { describe, expect, it } from 'vitest'
import type { Env } from '../_lib/auth'
import { onRequestGet as diff } from './diff'
import { onRequestGet as subtree } from './subtree'

// A query that doesn't parse is a 400 carrying the parser's message (the
// filter box shows it under itself), before auth or any read — never a silent
// "no matches".
const env = { GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
const get = async (h: typeof subtree, qs: string, e: Env = env) => {
  const r = await h({ request: new Request(`https://x/api?${qs}`), env: e })
  return [r.status, await r.text()]
}

describe('a bad `q=` / `qs=` is a 400 with its message', () => {
  it('/api/subtree', async () => {
    expect(await Promise.all([
      get(subtree, 'date=2026-10-01&q=/a(/'),
      get(subtree, 'date=2026-10-01&q=a(&qs=regex'),
      get(subtree, 'date=2026-10-01&q=a(', { ...env, QUERY_SYNTAX: 'regex' }),
      get(subtree, 'date=2026-10-01&q=a&qs=nope'),
    ])).toEqual([
      [400, 'bad query: invalid regex: Invalid regular expression: /a(/i: Unterminated group'],
      [400, 'bad query: invalid regex: Invalid regular expression: /a(/i: Unterminated group'],
      [400, 'bad query: invalid regex: Invalid regular expression: /a(/i: Unterminated group'],
      [400, "bad query: unknown query syntax 'nope' (want simple|regex)"],
    ])
  })
  it('/api/diff', async () => {
    expect(await get(diff, 'from=2026-09-01&to=2026-10-01&q=*x&qs=regex')).toEqual([400, 'bad query: invalid regex: Invalid regular expression: /*x/i: Nothing to repeat'])
  })
})
